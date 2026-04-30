"""Backup retention policy enforcement."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from pfsentinel.models.backup import BackupIndex, BackupRecord
from pfsentinel.models.config import BackupPolicy


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
            to_delete: list[BackupRecord] = []

            # Apply max count per type
            if len(records) > max_count:
                to_delete.extend(records[max_count:])

            # Apply keep_days
            marked = {r.id for r in to_delete}
            for record in records:
                if record.id not in marked and record.created_at < cutoff_date:
                    to_delete.append(record)

            for record in to_delete:
                self._delete_record(device_id, record, index)
                deleted.append(record)

        if deleted:
            self.save_index(index)
            logger.info(f"Retention: deleted {len(deleted)} backups for {device_id}")

        return deleted

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

        index.remove(record.id)

    def next_sequence(self, device_id: str) -> int:
        """Get the next sequence number for today's backups."""
        index = self.load_index(device_id)
        today = datetime.now().date()
        today_count = sum(
            1 for r in index.records if r.created_at.date() == today and r.device_id == device_id
        )
        return today_count + 1
