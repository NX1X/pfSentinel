"""Tests for BackupType enum, extended BackupRecord, and BackupIndex migration."""

from __future__ import annotations

import json

from pfsentinel.models.backup import (
    BackupIndex,
    BackupRecord,
    BackupType,
    ChangeCategory,
)


class TestBackupType:
    def test_all_types_exist(self):
        assert BackupType.CONFIG == "config"
        assert BackupType.RRD == "rrd"
        assert BackupType.PACKAGE_CONFIGS == "pkg"
        assert BackupType.DHCP_LEASES == "dhcp"
        assert BackupType.ALIASES == "aliases"
        assert BackupType.CERTIFICATES == "certs"
        assert BackupType.LOGS == "logs"
        assert BackupType.ZFS_SNAPSHOT == "zfs"
        assert BackupType.FS_ARCHIVE == "archive"

    def test_type_is_str_enum(self):
        assert str(BackupType.CONFIG) == "config"
        assert f"{BackupType.ZFS_SNAPSHOT}" == "zfs"


class TestBackupRecordExtensions:
    def test_default_backup_type_is_config(self):
        record = BackupRecord(device_id="fw1", filename="test.xml", relative_path="test.xml")
        assert record.backup_type == BackupType.CONFIG

    def test_custom_backup_type(self):
        record = BackupRecord(
            device_id="fw1",
            filename="test.tar.gz",
            relative_path="rrd/2025/07/06/test.tar.gz",
            backup_type=BackupType.RRD,
        )
        assert record.backup_type == BackupType.RRD

    def test_source_paths_default_empty(self):
        record = BackupRecord(device_id="fw1", filename="test.xml", relative_path="test.xml")
        assert record.source_paths == []

    def test_source_paths_stored(self):
        record = BackupRecord(
            device_id="fw1",
            filename="test.tar.gz",
            relative_path="rrd/test.tar.gz",
            source_paths=["/var/db/rrd/wan.rrd", "/var/db/rrd/lan.rrd"],
        )
        assert len(record.source_paths) == 2
        assert "/var/db/rrd/wan.rrd" in record.source_paths

    def test_zfs_fields_default_none(self):
        record = BackupRecord(device_id="fw1", filename="test.xml", relative_path="test.xml")
        assert record.zfs_snapshot_name is None
        assert record.zfs_incremental is False
        assert record.zfs_base_snapshot is None

    def test_zfs_fields_stored(self):
        record = BackupRecord(
            device_id="fw1",
            filename="test.zfs.gz",
            relative_path="zfs/test.zfs.gz",
            backup_type=BackupType.ZFS_SNAPSHOT,
            zfs_snapshot_name="zroot/ROOT@pfsentinel-20250706",
            zfs_incremental=True,
            zfs_base_snapshot="zroot/ROOT@pfsentinel-20250705",
        )
        assert record.zfs_snapshot_name == "zroot/ROOT@pfsentinel-20250706"
        assert record.zfs_incremental is True
        assert record.zfs_base_snapshot == "zroot/ROOT@pfsentinel-20250705"

    def test_type_label_property(self):
        record = BackupRecord(
            device_id="fw1",
            filename="test.xml",
            relative_path="test.xml",
            backup_type=BackupType.ZFS_SNAPSHOT,
        )
        assert record.type_label == "zfs"

    def test_type_label_config(self):
        record = BackupRecord(device_id="fw1", filename="test.xml", relative_path="test.xml")
        assert record.type_label == "config"

    def test_backward_compatible_serialization(self):
        """Records without new fields should deserialize with defaults."""
        old_data = {
            "device_id": "fw1",
            "filename": "test.xml",
            "relative_path": "2025/07/06/test.xml",
        }
        record = BackupRecord.model_validate(old_data)
        assert record.backup_type == BackupType.CONFIG
        assert record.source_paths == []
        assert record.zfs_snapshot_name is None


class TestBackupIndexMigration:
    def test_migrate_v1_to_v2_adds_backup_type(self):
        v1_data = {
            "device_id": "fw1",
            "schema_version": 1,
            "records": [
                {
                    "id": "abc-123",
                    "device_id": "fw1",
                    "filename": "test.xml",
                    "relative_path": "2025/07/06/test.xml",
                }
            ],
        }
        migrated = BackupIndex.migrate(v1_data)
        assert migrated["schema_version"] == 2
        assert migrated["records"][0]["backup_type"] == "config"
        assert migrated["records"][0]["source_paths"] == []

    def test_migrate_v2_is_noop(self):
        v2_data = {
            "device_id": "fw1",
            "schema_version": 2,
            "records": [
                {
                    "id": "abc-123",
                    "device_id": "fw1",
                    "filename": "test.xml",
                    "relative_path": "2025/07/06/test.xml",
                    "backup_type": "rrd",
                    "source_paths": ["/var/db/rrd/wan.rrd"],
                }
            ],
        }
        migrated = BackupIndex.migrate(v2_data)
        assert migrated["records"][0]["backup_type"] == "rrd"

    def test_migrate_preserves_existing_fields(self):
        v1_data = {
            "device_id": "fw1",
            "schema_version": 1,
            "records": [
                {
                    "id": "abc-123",
                    "device_id": "fw1",
                    "filename": "test.xml.gz",
                    "relative_path": "2025/07/06/test.xml.gz",
                    "sha256": "deadbeef",
                    "compressed": True,
                    "verified": True,
                }
            ],
        }
        migrated = BackupIndex.migrate(v1_data)
        rec = migrated["records"][0]
        assert rec["sha256"] == "deadbeef"
        assert rec["compressed"] is True
        assert rec["verified"] is True
        assert rec["backup_type"] == "config"

    def test_migrate_multiple_records(self):
        v1_data = {
            "device_id": "fw1",
            "schema_version": 1,
            "records": [
                {
                    "device_id": "fw1",
                    "filename": "a.xml",
                    "relative_path": "a.xml",
                },
                {
                    "device_id": "fw1",
                    "filename": "b.xml",
                    "relative_path": "b.xml",
                },
            ],
        }
        migrated = BackupIndex.migrate(v1_data)
        assert all(r["backup_type"] == "config" for r in migrated["records"])

    def test_full_roundtrip_v1_load(self):
        """Simulate loading a v1 index file and getting a valid BackupIndex."""
        v1_json = json.dumps(
            {
                "device_id": "fw1",
                "schema_version": 1,
                "records": [
                    {
                        "id": "test-id",
                        "device_id": "fw1",
                        "filename": "fw1_2025-07-06_143022_#001_initial.xml",
                        "relative_path": "2025/07/06/fw1_2025-07-06_143022_#001_initial.xml",
                        "changes": ["initial"],
                    }
                ],
            }
        )
        data = json.loads(v1_json)
        data = BackupIndex.migrate(data)
        index = BackupIndex.model_validate(data)
        assert index.schema_version == 2
        assert index.records[0].backup_type == BackupType.CONFIG
        assert index.records[0].changes == [ChangeCategory.INITIAL]


class TestBackupIndexSortedByType:
    def _make_record(self, backup_type: BackupType, name: str) -> BackupRecord:
        return BackupRecord(
            device_id="fw1",
            filename=name,
            relative_path=name,
            backup_type=backup_type,
        )

    def test_sorted_by_type_filters(self):
        index = BackupIndex(device_id="fw1")
        index.add(self._make_record(BackupType.CONFIG, "a.xml"))
        index.add(self._make_record(BackupType.RRD, "b.tar.gz"))
        index.add(self._make_record(BackupType.CONFIG, "c.xml"))

        config_records = index.sorted_by_type(BackupType.CONFIG)
        assert len(config_records) == 2

        rrd_records = index.sorted_by_type(BackupType.RRD)
        assert len(rrd_records) == 1

    def test_latest_with_type(self):
        index = BackupIndex(device_id="fw1")
        r1 = self._make_record(BackupType.CONFIG, "a.xml")
        r2 = self._make_record(BackupType.RRD, "b.tar.gz")
        index.add(r1)
        index.add(r2)

        assert index.latest(BackupType.CONFIG) is not None
        assert index.latest(BackupType.CONFIG).filename == "a.xml"
        assert index.latest(BackupType.RRD).filename == "b.tar.gz"
        assert index.latest(BackupType.ZFS_SNAPSHOT) is None

    def test_latest_without_type(self):
        index = BackupIndex(device_id="fw1")
        r1 = self._make_record(BackupType.CONFIG, "a.xml")
        index.add(r1)

        latest = index.latest()
        assert latest is not None
