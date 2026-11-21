"""Backup retention policy enforcement."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path

from pfsentinel.models.backup import (
    BackupIndex,
    BackupIndexError,
    BackupRecord,
    BackupRun,
    BackupType,
)
from pfsentinel.models.config import BackupPolicy
from pfsentinel.utils.locking import file_lock
from pfsentinel.utils.logging import get_logger
from pfsentinel.utils.permissions import restrict_to_owner

LOCK_FILE = ".pfsentinel.lock"


def _replace(src: str, dst: Path) -> None:
    """os.replace, retried on Windows while another process reads the index.

    Windows refuses to replace a file that another process has open, for
    example a `pfs backup list` in a second window.
    """
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 9:
                raise
            time.sleep(0.2)


logger = get_logger(__name__)

INDEX_FILE = "backup_index.json"


class RetentionService:
    """Enforces backup retention policy for a device."""

    def __init__(self, backup_root: Path, policy: BackupPolicy) -> None:
        self._backup_root = backup_root
        self._policy = policy

    def device_dir(self, device_id: str) -> Path:
        return self._backup_root / device_id

    def index_path(self, device_id: str) -> Path:
        return self.device_dir(device_id) / INDEX_FILE

    def load_index(self, device_id: str) -> BackupIndex:
        """The device's index. Empty only when backup_index.json does not exist.

        Any other problem raises BackupIndexError. An empty index here used
        to be saved over the real one by the next run, and the old records
        were lost. Path.exists() is not used: it also returns False when
        the file exists but cannot be read.
        """
        path = self.index_path(device_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return BackupIndex(device_id=device_id)
        except OSError as e:
            raise BackupIndexError(device_id, path, e.strerror or type(e).__name__) from e
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise TypeError(_NOT_AN_OBJECT)
            return BackupIndex.model_validate(BackupIndex.migrate(data))
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            raise BackupIndexError(device_id, path, _index_error_reason(e)) from e

    def save_index(self, index: BackupIndex) -> None:
        """Write the index atomically and keep the previous file as .bak.

        The new content goes to a temp file in the same folder, is flushed
        to disk, and then replaces the index in one step. A crash or a full
        disk leaves the old index in place, never a half-written one.
        """
        path = self.index_path(index.device_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = index.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{INDEX_FILE}.", suffix=".tmp")
        try:
            if sys.platform != "win32":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            _keep_previous(path)
            _replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        _sync_folder(path.parent)

    def device_lock(self, device_id: str, timeout: float = 2400.0) -> AbstractContextManager[None]:
        """Lock the device's backups against another pfSentinel process.

        Hold it while a run, reindex or verify changes backup_index.json.
        """
        folder = self._backup_root / device_id
        folder.mkdir(parents=True, exist_ok=True)
        return file_lock(folder / LOCK_FILE, timeout=timeout)

    def apply(self, device_id: str) -> list[BackupRecord]:
        """Apply retention policy per backup type. Returns list of deleted records."""
        index = self.load_index(device_id)
        deleted: list[BackupRecord] = []
        cutoff_date = datetime.now() - timedelta(days=self._policy.keep_days)

        # Group records by backup_type
        by_type: dict[str, list[BackupRecord]] = defaultdict(list)
        for r in index.records:
            by_type[r.backup_type.value].append(r)

        for type_name, records in by_type.items():
            records.sort(key=lambda r: r.created_at, reverse=True)
            max_count = self._policy.max_backups_per_type.get(
                type_name, self._policy.max_backups_per_device
            )
            # Long-term keepers sit outside the normal limits: they neither
            # count toward max_count nor expire with keep_days.
            protected = self.long_term_keep(type_name, records)
            normal = [r for r in records if r.id not in protected]
            to_delete: list[BackupRecord] = []

            # Apply max count per type
            if len(normal) > max_count:
                to_delete.extend(normal[max_count:])

            # Apply keep_days
            marked = {r.id for r in to_delete}
            for record in normal:
                if record.id not in marked and record.created_at < cutoff_date:
                    to_delete.append(record)

            if type_name == BackupType.ZFS_SNAPSHOT.value:
                to_delete = whole_zfs_chains(records, to_delete)

            for record in to_delete:
                if self._delete_record(device_id, record, index):
                    deleted.append(record)

        if deleted:
            self.save_index(index)
            logger.info(f"Retention: deleted {len(deleted)} backups for {device_id}")

        return deleted

    def long_term_keep(
        self, type_name: str, records: list[BackupRecord], now: datetime | None = None
    ) -> set[str]:
        """IDs of the weekly and monthly keepers among ``records`` (newest first)."""
        policy = self._policy.long_term
        if not policy.enabled or type_name not in policy.types:
            return set()
        now = now or datetime.now()
        week_cutoff = now - timedelta(weeks=policy.weekly_weeks)
        # The first day of the month that starts the monthly window.
        months_back = now.year * 12 + now.month - 1 - (policy.monthly_months - 1)
        month_cutoff = datetime(months_back // 12, months_back % 12 + 1, 1)

        keep: set[str] = set()
        weeks_seen: set[tuple[int, int]] = set()
        months_seen: set[tuple[int, int]] = set()
        for record in sorted(records, key=lambda r: r.created_at, reverse=True):
            when = record.created_at
            week = when.isocalendar()[:2]
            if policy.weekly_weeks and when >= week_cutoff and week not in weeks_seen:
                weeks_seen.add(week)
                keep.add(record.id)
            month = (when.year, when.month)
            if policy.monthly_months and when >= month_cutoff and month not in months_seen:
                months_seen.add(month)
                keep.add(record.id)
        return keep

    def _delete_record(self, device_id: str, record: BackupRecord, index: BackupIndex) -> bool:
        """Delete backup file and remove from index. False if the file could not be deleted."""
        path = self._backup_root / device_id / record.relative_path
        if path.is_symlink():
            logger.error(f"Refusing to delete symlink (potential attack): {path}")
            index.remove(record.id)
            return True
        # Verify resolved path stays within backup root
        try:
            path.resolve().relative_to(self._backup_root.resolve())
        except ValueError:
            logger.error(f"Path escapes backup root (potential traversal): {path}")
            index.remove(record.id)
            return True
        if path.exists():
            try:
                path.unlink()
                logger.debug(f"Deleted backup file: {path}")
            except OSError as e:
                # Keep the record, so the next run tries again. Without it
                # the file stays on disk, unlisted and never removed.
                logger.error(f"Could not delete {path}: {e}")
                return False
        else:
            logger.warning(f"Backup file not found during cleanup: {path}")

        prune_empty_dirs(path.parent, self._backup_root / device_id)
        index.remove(record.id)
        return True

    def start_run(self, device_id: str) -> BackupRun:
        """Reserve the next run number for the device and return the run.

        The number is saved at once, so a run that fails half-way never
        hands its number to the next run.
        """
        index = self.load_index(device_id)
        if not index.records:
            index = self._rebuild_if_files_exist(device_id, index)
        index.last_run_number += 1
        self.save_index(index)
        # Windows: the device folder holds the backups; owner-only like Linux.
        restrict_to_owner(self._backup_root / device_id)
        return BackupRun(
            device_id=device_id, number=index.last_run_number, started_at=datetime.now()
        )

    def _rebuild_if_files_exist(self, device_id: str, index: BackupIndex) -> BackupIndex:
        """An empty index next to backup files: rebuild it before the run saves.

        Field case (2026-11-20): backup_index.json was deleted by hand with
        old backups, the next run started again at #001 and the newer
        backups vanished from the list. Reindex only adds records and never
        touches backup files, so running it here is safe.
        """
        from pfsentinel.services.reindex import reindex_device  # avoids an import cycle

        result = reindex_device(self, device_id, strict=True)
        if result.unreadable:
            # Saving now would start the run numbers again over files that
            # exist but cannot be read. Stop; the run alerts as failed.
            raise BackupIndexError(
                device_id,
                self.index_path(device_id),
                f"the index is missing and {len(result.unreadable)} backup file(s) or "
                f"folder(s) next to it cannot be read, for example {result.unreadable[0]}",
            )
        if not result.added:
            return index
        logger.warning(
            f"The backup index of {device_id} was missing or empty; rebuilt it from "
            f"{len(result.added)} backup file(s) on disk. Run numbers continue after "
            f"#{result.last_run_number}."
        )
        return self.load_index(device_id)

    def next_sequence(self, device_id: str) -> int:
        """Get the next sequence number for today's backups."""
        index = self.load_index(device_id)
        today = datetime.now().date()
        today_count = sum(
            1 for r in index.records if r.created_at.date() == today and r.device_id == device_id
        )
        return today_count + 1


_NOT_AN_OBJECT = "not a JSON object"


def _index_error_reason(error: Exception) -> str:
    """A short reason that never echoes the file content."""
    if isinstance(error, json.JSONDecodeError):
        return f"not valid JSON at line {error.lineno}"
    if isinstance(error, UnicodeDecodeError):
        return "not UTF-8 text"
    errors = getattr(error, "errors", None)
    if callable(errors):  # pydantic ValidationError
        first = errors()[0] if errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ())) or "file"
        # A custom validator's message quotes the value; keep it out.
        msg = "invalid value" if first.get("type") == "value_error" else first.get("msg")
        return f"{msg or 'invalid'} at {where}"
    if str(error) == _NOT_AN_OBJECT:
        return _NOT_AN_OBJECT
    return f"unexpected structure ({type(error).__name__})"


def _sync_folder(folder: Path) -> None:
    """Flush the folder entry after os.replace, so the swap survives a crash (POSIX).

    Windows has no folder fsync; NTFS logs the rename itself. A file system
    that cannot sync a folder (EINVAL, ENOTSUP) is skipped; other errors raise.
    """
    if sys.platform == "win32":
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            raise
    finally:
        os.close(fd)


def _keep_previous(path: Path) -> None:
    """Copy the current index to backup_index.json.bak, itself replaced atomically.

    A failed copy only logs a warning: the .bak is a spare, and the new
    index must still be saved.
    """
    spare = path.with_name(path.name + ".bak")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{spare.name}.", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copyfile(path, tmp)
        os.replace(tmp, spare)
    except FileNotFoundError:
        pass  # first save: nothing to keep
    except OSError as e:
        logger.warning(f"Could not keep the previous backup index as {spare.name}: {e}")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def whole_zfs_chains(
    records: list[BackupRecord], to_delete: list[BackupRecord]
) -> list[BackupRecord]:
    """Delete a ZFS chain (a full send and the incrementals on it) whole, or not at all.

    An incremental restores only on top of the sends before it. Deleting the
    full send left incrementals that could not be restored.
    """
    marked = {r.id for r in to_delete}
    chains: list[list[BackupRecord]] = []
    for record in sorted(records, key=lambda r: r.created_at):
        if not record.zfs_incremental or not chains:
            chains.append([])
        chains[-1].append(record)
    keep: set[str] = set()
    for chain in chains:
        if any(r.id not in marked for r in chain):
            keep.update(r.id for r in chain)
    return [r for r in to_delete if r.id not in keep]


def prune_empty_dirs(folder: Path, stop: Path) -> None:
    """Remove ``folder`` and its parents while empty, up to (not including) ``stop``.

    Each run has its own folder, so deleting a run's last file would
    otherwise leave empty run, day and month folders behind.
    """
    try:
        folder.resolve().relative_to(stop.resolve())
    except (ValueError, OSError):
        return
    current = folder
    while current != stop and current.is_dir() and not current.is_symlink():
        try:
            current.rmdir()  # only succeeds when empty
        except OSError:
            return
        current = current.parent
