"""Tests for type-aware retention policy."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from pfsentinel.models.backup import BackupIndex, BackupRecord, BackupType
from pfsentinel.models.config import BackupPolicy, LongTermRetention
from pfsentinel.services.retention import RetentionService


def _make_record(
    device_id: str,
    backup_type: BackupType,
    filename: str,
    age_days: int = 0,
    relative_path: str | None = None,
) -> BackupRecord:
    ts = datetime.now() - timedelta(days=age_days)
    rp = relative_path or filename
    return BackupRecord(
        device_id=device_id,
        filename=filename,
        relative_path=rp,
        backup_type=backup_type,
        created_at=ts,
    )


class TestTypedRetention:
    def test_retention_per_type_independent(self, tmp_path: Path):
        """Config and RRD records have separate max counts."""
        policy = BackupPolicy(
            backup_root=tmp_path,
            max_backups_per_type={"config": 2, "rrd": 2},
            keep_days=365,
            long_term=LongTermRetention(enabled=False),
        )
        retention = RetentionService(tmp_path, policy)

        device_id = "fw1"
        device_dir = tmp_path / device_id
        device_dir.mkdir(parents=True)

        # Create 4 config + 4 rrd records
        index = BackupIndex(device_id=device_id)
        for i in range(4):
            cfg = _make_record(device_id, BackupType.CONFIG, f"cfg-{i}.xml", age_days=i)
            rrd = _make_record(device_id, BackupType.RRD, f"rrd-{i}.tar.gz", age_days=i)
            index.add(cfg)
            index.add(rrd)

        retention.save_index(index)

        deleted = retention.apply(device_id)
        # Should delete 2 config + 2 rrd (keep newest 2 of each)
        assert len(deleted) == 4

        reloaded = retention.load_index(device_id)
        config_records = [r for r in reloaded.records if r.backup_type == BackupType.CONFIG]
        rrd_records = [r for r in reloaded.records if r.backup_type == BackupType.RRD]
        assert len(config_records) == 2
        assert len(rrd_records) == 2

    def test_retention_uses_fallback_for_unknown_type(self, tmp_path: Path):
        """Types not in max_backups_per_type fall back to max_backups_per_device."""
        policy = BackupPolicy(
            backup_root=tmp_path,
            max_backups_per_device=3,
            max_backups_per_type={},
            keep_days=365,
        )
        retention = RetentionService(tmp_path, policy)

        device_id = "fw1"
        device_dir = tmp_path / device_id
        device_dir.mkdir(parents=True)

        index = BackupIndex(device_id=device_id)
        for i in range(5):
            rec = _make_record(device_id, BackupType.LOGS, f"logs-{i}.tar.gz", age_days=i)
            index.add(rec)

        retention.save_index(index)
        deleted = retention.apply(device_id)

        assert len(deleted) == 2  # 5 - 3 = 2 deleted
        reloaded = retention.load_index(device_id)
        assert len(reloaded.records) == 3

    def test_retention_keep_days_applies_per_type(self, tmp_path: Path):
        """Old records are deleted even if under max count."""
        policy = BackupPolicy(
            backup_root=tmp_path,
            max_backups_per_type={"config": 100},
            keep_days=7,
            long_term=LongTermRetention(enabled=False),
        )
        retention = RetentionService(tmp_path, policy)

        device_id = "fw1"
        (tmp_path / device_id).mkdir(parents=True)

        index = BackupIndex(device_id=device_id)
        index.add(_make_record(device_id, BackupType.CONFIG, "new.xml", age_days=1))
        index.add(_make_record(device_id, BackupType.CONFIG, "old.xml", age_days=30))

        retention.save_index(index)
        deleted = retention.apply(device_id)

        assert len(deleted) == 1
        reloaded = retention.load_index(device_id)
        assert len(reloaded.records) == 1
        assert reloaded.records[0].filename == "new.xml"

    def test_load_migrates_v1_index(self, tmp_path: Path):
        """Loading a v1 index file auto-migrates to v2."""
        policy = BackupPolicy(backup_root=tmp_path)
        retention = RetentionService(tmp_path, policy)

        device_id = "fw1"
        index_dir = tmp_path / device_id
        index_dir.mkdir(parents=True)
        index_path = index_dir / "backup_index.json"

        v1_data = {
            "device_id": device_id,
            "schema_version": 1,
            "records": [
                {
                    "id": "test-1",
                    "device_id": device_id,
                    "filename": "test.xml",
                    "relative_path": "2025/07/06/test.xml",
                }
            ],
        }
        index_path.write_text(json.dumps(v1_data), encoding="utf-8")

        loaded = retention.load_index(device_id)
        assert loaded.schema_version == 3
        assert loaded.records[0].backup_type == BackupType.CONFIG

    def test_no_deletion_when_under_limits(self, tmp_path: Path):
        """No records deleted when counts are under limits."""
        policy = BackupPolicy(
            backup_root=tmp_path,
            max_backups_per_type={"config": 10},
            keep_days=365,
        )
        retention = RetentionService(tmp_path, policy)

        device_id = "fw1"
        (tmp_path / device_id).mkdir(parents=True)

        index = BackupIndex(device_id=device_id)
        for i in range(3):
            index.add(_make_record(device_id, BackupType.CONFIG, f"cfg-{i}.xml", age_days=i))

        retention.save_index(index)
        deleted = retention.apply(device_id)
        assert len(deleted) == 0


class TestLongTermRetention:
    """Besides the last N: 1 per week for 3 months, 1 per month for a year."""

    def _daily_for(self, days: int) -> BackupIndex:
        index = BackupIndex(device_id="fw1")
        for age in range(days):
            index.add(_make_record("fw1", BackupType.CONFIG, f"cfg-{age:03d}.xml", age_days=age))
        return index

    def test_daily_backups_for_two_years_keep_the_expected_set(self, tmp_path: Path) -> None:
        policy = BackupPolicy(backup_root=tmp_path, max_backups_per_type={"config": 30})
        retention = RetentionService(tmp_path, policy)
        (tmp_path / "fw1").mkdir()
        retention.save_index(self._daily_for(730))

        retention.apply("fw1")
        kept = retention.load_index("fw1").records
        ages = sorted((datetime.now() - r.created_at).days for r in kept)
        assert ages[:30] == list(range(30)), "the last 30 days stay"
        older = [a for a in ages if a >= 30]
        assert older, "weekly and monthly keepers survive keep_days"
        assert max(older) <= 366, "nothing older than about a year"
        # About 12 weeks plus about 12 months, overlapping: a few dozen at most.
        assert 8 <= len(older) <= 24

    def test_the_newest_backup_of_each_week_is_kept(self, tmp_path: Path) -> None:
        policy = BackupPolicy(backup_root=tmp_path)
        retention = RetentionService(tmp_path, policy)
        records = self._daily_for(120).records
        keep = retention.long_term_keep("config", records)
        weeks = {}
        for r in records:
            weeks.setdefault(r.created_at.isocalendar()[:2], []).append(r)
        recent = [
            w
            for w, rs in weeks.items()
            if max(x.created_at for x in rs) >= datetime.now() - timedelta(weeks=12)
        ]
        for week in recent:
            newest = max(weeks[week], key=lambda r: r.created_at)
            assert newest.id in keep

    def test_only_listed_types_get_long_term_keepers(self, tmp_path: Path) -> None:
        retention = RetentionService(tmp_path, BackupPolicy(backup_root=tmp_path))
        rrd = [_make_record("fw1", BackupType.RRD, "r.tar", age_days=40)]
        assert retention.long_term_keep("rrd", rrd) == set()
