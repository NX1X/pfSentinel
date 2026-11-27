"""A history of backup runs, failed runs included.

The backup index lists only the files that exist. A run that fails makes no
file, so it left no trace: on Windows only the last task result remained.

Each run of ``pfs backup run`` (scheduled or by hand) writes two lines to
``run_history.jsonl`` next to config.json: one when it starts, one when it
ends. A run with a start line only was stopped before its end (the PC shut
down, the task hit its time limit) or is still going. ``pfs backup history``
shows the runs.

Nothing here may stop a backup: every write error is logged and ignored.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pfsentinel import __version__
from pfsentinel.services import alert_history
from pfsentinel.utils.locking import LockTimeoutError, file_lock
from pfsentinel.utils.logging import get_logger, redact_secrets

logger = get_logger(__name__)

HISTORY_FILE = "run_history.jsonl"
LOCK_FILE = "run_history.lock"
# The file is cut back to KEEP_RUNS runs when it holds more than MAX_RUNS.
MAX_RUNS = 1500
KEEP_RUNS = 1000
LOCK_TIMEOUT = 10.0
_TEXT_LIMIT = 300
_MAX_PROBLEMS = 10

OK = "ok"
PARTIAL = "partial"
FAILED = "failed"
UNFINISHED = "unfinished"


@dataclass
class RunEntry:
    """One backup run of one device."""

    id: str
    device: str
    # Both times carry their UTC offset (see _aware).
    started: datetime
    ended: datetime | None = None
    result: str = UNFINISHED
    run: int | None = None
    files: int = 0
    size_bytes: int = 0
    changes: str = ""
    error: str = ""
    problems: list[str] = field(default_factory=list)
    version: str = ""

    @property
    def seconds(self) -> float | None:
        """How long the run took, or None while it has no end."""
        if self.ended is None:
            return None
        return max(0.0, (self.ended - self.started).total_seconds())

    @property
    def detail(self) -> str:
        """One line that says what went wrong, empty for a clean run."""
        if self.result == UNFINISHED:
            return "No end recorded: still running, or stopped before the end"
        parts = [self.error] if self.error else []
        parts.extend(self.problems)
        return "; ".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device": self.device,
            "started": self.started.isoformat(timespec="seconds"),
            "ended": self.ended.isoformat(timespec="seconds") if self.ended else None,
            "result": self.result,
            "run": self.run,
            "files": self.files,
            "size_bytes": self.size_bytes,
            "changes": self.changes,
            "error": self.error,
            "problems": list(self.problems),
            "version": self.version,
        }


def _aware(when: datetime | None = None) -> datetime:
    """A time with its UTC offset. A time without one is taken as local time.

    With the offset, the length and the order of runs stay right when the
    clock goes back an hour in autumn.
    """
    return (when or datetime.now()).astimezone()


def history_path() -> Path:
    return alert_history.state_dir() / HISTORY_FILE


def _clean(text: str) -> str:
    """One line, no secrets, not longer than the limit."""
    line = " ".join(redact_secrets(str(text)).split())
    if len(line) > _TEXT_LIMIT:
        line = line[: _TEXT_LIMIT - 3] + "..."
    return line


def _from_dict(raw: Any) -> RunEntry | None:
    if not isinstance(raw, dict):
        return None
    try:
        ended = raw.get("ended")
        problems = raw.get("problems") or []
        return RunEntry(
            id=str(raw["id"]),
            device=str(raw["device"]),
            started=_aware(datetime.fromisoformat(str(raw["started"]))),
            ended=_aware(datetime.fromisoformat(str(ended))) if ended else None,
            result=str(raw.get("result") or UNFINISHED),
            run=int(raw["run"]) if raw.get("run") is not None else None,
            files=int(raw.get("files") or 0),
            size_bytes=int(raw.get("size_bytes") or 0),
            changes=str(raw.get("changes") or ""),
            error=str(raw.get("error") or ""),
            problems=[str(p) for p in problems] if isinstance(problems, list) else [],
            version=str(raw.get("version") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _read(path: Path) -> list[RunEntry]:
    """Every run in the file, oldest first. The end line of a run replaces its start line."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    runs: dict[str, RunEntry] = {}
    # Line by line, also the decoding: a crash can cut a line short, even
    # inside a UTF-8 character. That one line is skipped, the rest stays.
    for raw in data.split(b"\n"):
        try:
            entry = _from_dict(json.loads(raw.decode("utf-8")))
        except ValueError:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
            continue
        if entry is not None:
            runs[entry.id] = entry
    return sorted(runs.values(), key=lambda e: e.started)


def _append(path: Path, entry: RunEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(entry.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _trim(path: Path) -> None:
    """Keep the newest KEEP_RUNS runs once the file holds more than MAX_RUNS."""
    runs = _read(path)
    if len(runs) <= MAX_RUNS:
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for entry in runs[-KEEP_RUNS:]:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write(entry: RunEntry, trim: bool) -> None:
    # Any error: the history is a record of the backup, never a reason to
    # fail it. So this catches everything, not only OSError.
    try:
        path = history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.ExitStack() as stack:
            # Without the lock (another run holds it too long) the line is
            # still added: one append is one write.
            with contextlib.suppress(LockTimeoutError):
                stack.enter_context(file_lock(path.with_name(LOCK_FILE), timeout=LOCK_TIMEOUT))
            _append(path, entry)
            if trim:
                _trim(path)
    except Exception as e:
        logger.warning(f"Could not write the run history ({HISTORY_FILE}): {e}")


def begin(device: str, now: datetime | None = None) -> RunEntry:
    """Record that a run started. Returns the entry to pass to ``finish``."""
    entry = RunEntry(
        id=uuid.uuid4().hex[:12],
        device=device,
        started=_aware(now),
        version=__version__,
    )
    _write(entry, trim=False)
    return entry


def finish(
    entry: RunEntry,
    *,
    run: int | None = None,
    files: int = 0,
    size_bytes: int = 0,
    changes: str = "",
    error: str = "",
    problems: list[str] | None = None,
    now: datetime | None = None,
) -> RunEntry:
    """Record how a run ended: failed with an error, partial with problems, else ok."""
    entry.ended = _aware(now)
    entry.run = run if isinstance(run, int) else None
    entry.files = int(files)
    entry.size_bytes = int(size_bytes)
    entry.changes = str(changes)
    entry.error = _clean(error) if error else ""
    entry.problems = [_clean(p) for p in (problems or [])[:_MAX_PROBLEMS]]
    if entry.error:
        entry.result = FAILED
    elif entry.problems:
        entry.result = PARTIAL
    else:
        entry.result = OK
    _write(entry, trim=True)
    return entry


def load(
    device: str | None = None,
    limit: int | None = None,
    problems_only: bool = False,
) -> list[RunEntry]:
    """Runs, newest first. ``problems_only`` leaves out the clean runs."""
    runs = _read(history_path())
    if device:
        runs = [e for e in runs if e.device == device]
    if problems_only:
        runs = [e for e in runs if e.result != OK]
    runs.reverse()
    return runs[:limit] if limit else runs
