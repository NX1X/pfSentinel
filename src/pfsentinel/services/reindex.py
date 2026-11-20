"""Rebuild a device's backup index from the backup files on disk (recovery).

Used by ``pfs backup reindex``. Backup files are only read, never changed,
moved or deleted. A readable index keeps its records and gets the files it
is missing. An unreadable index is moved aside as
``backup_index.json.broken-<time>``, never deleted.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from pfsentinel.models.backup import (
    BackupIndex,
    BackupIndexError,
    BackupRecord,
    BackupType,
    ChangeCategory,
)
from pfsentinel.services import backup_crypto
from pfsentinel.services.retention import RetentionService
from pfsentinel.utils import checksum
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

# <device>_<YYYY-MM-DD>_<HHMMSS>_#<NNN>_<label><ext>[.gz][.enc], as written by
# naming.generate_filename (config) and naming.generate_backup_filename.
_BACKUP_NAME_RE = re.compile(
    r"^(?P<device>[a-z0-9][a-z0-9\-]*)_"
    r"(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{6})_#(?P<seq>\d{3,})_"
    r"(?P<label>[a-z0-9+\-]+)"
    r"(?P<ext>\.(?:xml|tar|txt|zfs|dat))(?P<gz>\.gz)?(?P<enc>\.enc)?$"
)
# Run folder of the 0.3.0 layout: YYYY/MM/DD/<HHMMSS>_#NNN/.
_RUN_FOLDER_RE = re.compile(r"^\d{6}_#(?P<run>\d{3,})$")

_TYPE_BY_LABEL = {
    "rrd": BackupType.RRD,
    "pkg": BackupType.PACKAGE_CONFIGS,
    "dhcp": BackupType.DHCP_LEASES,
    "aliases": BackupType.ALIASES,
    "certs": BackupType.CERTIFICATES,
    "logs": BackupType.LOGS,
    "archive": BackupType.FS_ARCHIVE,
    "zfs-full": BackupType.ZFS_SNAPSHOT,
    "zfs-incr": BackupType.ZFS_SNAPSHOT,
}
_CHANGE_VALUES = {c.value for c in ChangeCategory}

REINDEX_DESCRIPTION = "Recovered by pfs backup reindex"


@dataclass
class ReindexResult:
    device_id: str
    dry_run: bool
    added: list[BackupRecord] = field(default_factory=list)
    kept: int = 0
    last_run_number: int = 0
    # Set when the old index could not be read.
    broken_reason: str | None = None
    moved_to: Path | None = None
    # Paths relative to the device folder.
    skipped_unsafe: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    saved: bool = False
    # Highest run (or old daily sequence) number in a backup file name, also
    # of files that could not be read, so no new run reuses their number.
    highest_seen: int = 0


class ReindexError(OSError):
    """Reindex moved the broken index aside, then failed and could not move it back."""


def reindex_device(
    retention: RetentionService, device_id: str, dry_run: bool = False, strict: bool = False
) -> ReindexResult:
    """Rebuild ``device_id``'s index from its backup files. See the module docstring.

    The broken index is only moved aside after the scan, just before the
    new index is saved, so a failed scan leaves everything as it was. If the
    save then fails, the broken index is moved back.

    ``strict`` (the automatic rebuild before a run): when any backup file or
    folder cannot be read, save nothing, so a partial index is never kept.
    The caller sees ``result.unreadable``.

    pfSentinel has no cross-process lock. So the index is read again just
    before the save, and the found records are merged into it: a backup run
    that finished during the scan keeps its record and its run number.
    """
    result = ReindexResult(device_id=device_id, dry_run=dry_run)
    index_path = retention.index_path(device_id)
    try:
        index = retention.load_index(device_id)
    except BackupIndexError as e:
        result.broken_reason = e.reason
        result.moved_to = _broken_name(index_path)
        index = BackupIndex(device_id=device_id)
    result.kept = len(index.records)

    known = {_normalize(r.relative_path) for r in index.records}
    for path in _scan(retention.device_dir(device_id), result):
        rel = path.relative_to(retention.device_dir(device_id)).as_posix()
        if rel in known:
            continue
        record = _record_for(device_id, path, rel, result)
        if record is not None:
            result.added.append(record)
            known.add(rel)

    result.added.sort(key=lambda r: (r.created_at, r.relative_path))
    old_last = index.last_run_number
    result.last_run_number = max(_last_run_number(index, result.added), result.highest_seen)

    changed = (
        bool(result.added) or result.moved_to is not None or result.last_run_number != old_last
    )
    if dry_run or not changed or (strict and result.unreadable):
        return result

    if result.moved_to is not None:
        # A rename keeps the file. _broken_name picked a name that is not taken.
        index_path.rename(result.moved_to)
        logger.warning(f"Moved unreadable backup index of {device_id} to {result.moved_to}")
    try:
        _merge_and_save(retention, device_id, result)
    except BaseException:
        if result.moved_to is not None:
            _move_back(result.moved_to, index_path, device_id)
        raise
    result.saved = True
    logger.info(f"Reindex of {device_id}: {len(result.added)} record(s) added")
    return result


def _merge_and_save(retention: RetentionService, device_id: str, result: ReindexResult) -> None:
    """Read the index again, add the found records it lacks, keep the higher run number."""
    current = retention.load_index(device_id)
    known = {_normalize(r.relative_path) for r in current.records}
    for record in result.added:
        if _normalize(record.relative_path) not in known:
            current.add(record)
    current.last_run_number = max(current.last_run_number, result.last_run_number)
    result.last_run_number = current.last_run_number
    retention.save_index(current)


def _move_back(moved_to: Path, index_path: Path, device_id: str) -> None:
    try:
        moved_to.rename(index_path)
    except OSError as e:
        raise ReindexError(
            f"reindex of {device_id} failed after it moved the unreadable index aside, "
            f"and could not move it back ({e.strerror or e}). The old index is at {moved_to}"
        ) from e


def _normalize(relative_path: str) -> str:
    return relative_path.replace("\\", "/")


def _broken_name(index_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = index_path.with_name(f"{index_path.name}.broken-{stamp}")
    n = 1
    while candidate.exists() or candidate.is_symlink():
        n += 1
        candidate = index_path.with_name(f"{index_path.name}.broken-{stamp}-{n}")
    return candidate


def _inside(path: Path, root: Path) -> bool:
    """False for links and junctions, and for anything that resolves outside ``root``."""
    if path.is_symlink() or path.is_junction():
        return False
    try:
        path.resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    return True


def _scan(device_dir: Path, result: ReindexResult) -> list[Path]:
    """Backup-named files inside ``device_dir``. Never follows a link out of it."""
    if not device_dir.is_dir():
        return []
    root = device_dir.resolve()

    def on_error(error: OSError) -> None:
        where = Path(error.filename) if error.filename else device_dir
        result.unreadable.append(_relative(where, device_dir))
        logger.warning(f"Reindex could not read {where}: {error.strerror or error}")

    found: list[Path] = []
    for folder, dirs, files in os.walk(device_dir, onerror=on_error, followlinks=False):
        here = Path(folder)
        safe_dirs = []
        for name in sorted(dirs):
            if _inside(here / name, root):
                safe_dirs.append(name)
            else:
                _refuse(here / name, device_dir, result)
        dirs[:] = safe_dirs
        for name in sorted(files):
            if not _BACKUP_NAME_RE.match(name):
                continue
            path = here / name
            if _inside(path, root):
                found.append(path)
            else:
                _refuse(path, device_dir, result)
    return found


def _relative(path: Path, device_dir: Path) -> str:
    try:
        return path.relative_to(device_dir).as_posix()
    except ValueError:
        return str(path)


def _refuse(path: Path, device_dir: Path, result: ReindexResult) -> None:
    rel = _relative(path, device_dir)
    logger.error(f"Reindex skipped a link or a path outside the device folder: {path}")
    result.skipped_unsafe.append(rel)


def _record_for(device_id: str, path: Path, rel: str, result: ReindexResult) -> BackupRecord | None:
    match = _BACKUP_NAME_RE.match(path.name)
    if match is None or match["device"] != device_id:
        return None
    if match["ext"] == ".xml":
        backup_type = BackupType.CONFIG
        changes = [ChangeCategory(c) for c in match["label"].split("+") if c in _CHANGE_VALUES]
    elif match["label"] in _TYPE_BY_LABEL:
        backup_type = _TYPE_BY_LABEL[match["label"]]
        changes = []
    else:
        return None
    try:
        created = datetime.strptime(f"{match['date']} {match['time']}", "%Y-%m-%d %H%M%S")
    except ValueError:
        return None

    sequence = int(match["seq"])
    folder = _RUN_FOLDER_RE.match(path.parent.name)
    run_number = sequence if folder and int(folder["run"]) == sequence else None
    # Before the checksum: a file that cannot be read still holds its number.
    result.highest_seen = max(result.highest_seen, sequence)
    try:
        size = path.stat().st_size
        sha256 = checksum.sha256_file(path)
    except OSError as e:
        result.unreadable.append(rel)
        logger.warning(f"Reindex could not read {path}: {e.strerror or e}")
        return None
    try:
        return BackupRecord(
            device_id=device_id,
            filename=path.name,
            relative_path=rel,
            created_at=created,
            size_bytes=size,
            sha256=sha256,
            connection_method="unknown" if backup_type == BackupType.CONFIG else "ssh",
            changes=changes,
            compressed=bool(match["gz"]),
            sequence=sequence,
            verified=False,
            description=REINDEX_DESCRIPTION,
            backup_type=backup_type,
            zfs_incremental=match["label"] == "zfs-incr",
            run_number=run_number,
            encrypted=backup_crypto.is_encrypted(path),
        )
    except ValidationError:
        return None


def _last_run_number(index: BackupIndex, added: list[BackupRecord]) -> int:
    """At least the highest run number found, so new runs never reuse one.

    Files from before 0.3.0 carry a daily sequence instead of a run number;
    like BackupIndex.migrate, each (day, sequence) of those counts as a run.
    """
    records = [*index.records, *added]
    highest = max((r.run_number or 0 for r in records), default=0)
    highest_sequence = max((r.sequence for r in added), default=0)
    old_runs = {(r.created_at.date(), r.sequence) for r in records if r.run_number is None}
    return max(index.last_run_number, highest, highest_sequence, len(old_runs))
