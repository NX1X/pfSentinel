"""Backup retention policy enforcement."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from pfsentinel.models.backup import BackupIndex, BackupRecord, BackupRun
from pfsentinel.models.config import BackupPolicy
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)


class RetentionService:
    """Enforces backup retention policy for a device."""

    def __init__(self, backup_root: Path, policy: BackupPolicy) -> None:
        self._backup_root = backup_root
        self._policy = policy

    def _index_path(self, device_id: str) -> Path:
        return self._backup_root / device_id / "backup_index.json"

    def load_index(self, device_id: str) -> BackupIndex:
        path = self._index_path(device_id)
        if not path.exists():
            return BackupIndex(device_id=device_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data = BackupIndex.migrate(data)
            return BackupIndex.model_validate(data)
        except Exception as e:
            logger.warning(f"Could not load backup index for {device_id}: {e}")
            return BackupIndex(device_id=device_id)

    def save_index(self, index: BackupIndex) -> None:
        path = self._index_path(index.device_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(index.model_dump_json(indent=2), encoding="utf-8")

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

            for record in to_delete:
                self._delete_record(device_id, record, index)
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

    def _delete_record(self, device_id: str, record: BackupRecord, index: BackupIndex) -> None:
        """Delete backup file and remove from index."""
        path = self._backup_root / device_id / record.relative_path
        if path.is_symlink():
            logger.error(f"Refusing to delete symlink (potential attack): {path}")
            index.remove(record.id)
            return
        # Verify resolved path stays within backup root
        try:
            path.resolve().relative_to(self._backup_root.resolve())
        except ValueError:
            logger.error(f"Path escapes backup root (potential traversal): {path}")
            index.remove(record.id)
            return
        if path.exists():
            try:
                path.unlink()
                logger.debug(f"Deleted backup file: {path}")
            except OSError as e:
                logger.error(f"Could not delete {path}: {e}")
        else:
            logger.warning(f"Backup file not found during cleanup: {path}")

        prune_empty_dirs(path.parent, self._backup_root / device_id)
        index.remove(record.id)

    def start_run(self, device_id: str) -> BackupRun:
        """Reserve the next run number for the device and return the run.

        The number is saved at once, so a run that fails half-way never
        hands its number to the next run.
        """
        index = self.load_index(device_id)
        index.last_run_number += 1
        self.save_index(index)
        return BackupRun(
            device_id=device_id, number=index.last_run_number, started_at=datetime.now()
        )

    def next_sequence(self, device_id: str) -> int:
        """Get the next sequence number for today's backups."""
        index = self.load_index(device_id)
        today = datetime.now().date()
        today_count = sum(
            1 for r in index.records if r.created_at.date() == today and r.device_id == device_id
        )
        return today_count + 1


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
