"""A short history of the problem alerts sent, for the daily digest.

Stored as ``alert_history.json`` next to config.json. Only kept while the
digest is on, pruned to the last 7 days, and owner-only on POSIX: the
entries can hold error text.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pfsentinel.models.config import AppConfig
from pfsentinel.utils.logging import get_logger, redact_secrets

logger = get_logger(__name__)

HISTORY_FILE = "alert_history.json"
KEEP_FOR = timedelta(days=7)
MAX_ENTRIES = 1000
_SUMMARY_LIMIT = 200


@dataclass(frozen=True)
class AlertEntry:
    at: datetime
    kind: int
    device: str | None
    summary: str


def state_dir() -> Path:
    """The folder of config.json, where small state files live."""
    return AppConfig.config_path().parent


def write_private_json(path: Path, data: Any) -> None:
    """Atomic write, owner-only (0600) on POSIX before any content is written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> Any:
    """The parsed file, or None when it is missing or not valid JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def device_from_message(message: str) -> str | None:
    """Alert messages name their device on a "Device: <id>" line."""
    for line in message.splitlines():
        if line.startswith("Device:"):
            return line.partition(":")[2].strip() or None
    return None


def summarize(title: str, message: str) -> str:
    """The first line of the message that is not the device line, else the title."""
    line = next(
        (ln.strip() for ln in message.splitlines() if ln.strip() and not ln.startswith("Device:")),
        title,
    )
    if len(line) > _SUMMARY_LIMIT:
        line = line[: _SUMMARY_LIMIT - 3] + "..."
    return redact_secrets(line)


def _parse(raw: Any) -> list[AlertEntry]:
    if not isinstance(raw, list):
        return []
    entries: list[AlertEntry] = []
    for item in raw:
        try:
            entries.append(
                AlertEntry(
                    at=datetime.fromisoformat(str(item["at"])),
                    kind=int(item["kind"]),
                    device=str(item["device"]) if item.get("device") else None,
                    summary=str(item.get("summary", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return entries


def _history_path() -> Path:
    return state_dir() / HISTORY_FILE


def load_alerts(since: datetime) -> list[AlertEntry]:
    """Alerts recorded at or after ``since``, oldest first."""
    entries = _parse(read_json(_history_path()))
    return sorted((e for e in entries if e.at >= since), key=lambda e: e.at)


def record_alert(kind: int, title: str, message: str, now: datetime | None = None) -> None:
    """Add one alert to the history. A failure here never stops the alert itself."""
    when = now or datetime.now()
    entries = [e for e in _parse(read_json(_history_path())) if e.at >= when - KEEP_FOR]
    entries.append(
        AlertEntry(when, int(kind), device_from_message(message), summarize(title, message))
    )
    data = [
        {"at": e.at.isoformat(), "kind": e.kind, "device": e.device, "summary": e.summary}
        for e in entries[-MAX_ENTRIES:]
    ]
    try:
        write_private_json(_history_path(), data)
    except OSError as e:
        logger.warning(f"Could not record the alert for the daily digest: {e}")
