"""Tests for ZFS snapshot backup service."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.config import AppConfig, BackupPolicy, ZfsPolicy
from pfsentinel.models.device import DeviceConfig
from pfsentinel.models.zfs import ZfsSnapshot, ZfsSnapshotIndex
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.zfs_backup import ZfsBackupService, ZfsError

TS = datetime(2025, 3, 5, 14, 30, 22)


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        devices=[DeviceConfig(id="fw1", label="FW", host="10.0.0.1")],
        backup_policy=BackupPolicy(
            backup_root=tmp_path / "backups",
            zfs=ZfsPolicy(enabled=True, dataset="zroot/ROOT"),
        ),
    )


class TestSnapshotIndex:
    def test_load_missing_returns_empty(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ZfsBackupService(config, CredentialService())
        idx = svc.load_snapshot_index("fw1")
        assert idx.device_id == "fw1"
        assert len(idx.snapshots) == 0

    def test_load_corrupt_returns_empty(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ZfsBackupService(config, CredentialService())
        path = tmp_path / "backups" / "fw1" / "zfs_snapshots.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{{{bad json", encoding="utf-8")
        idx = svc.load_snapshot_index("fw1")
        assert len(idx.snapshots) == 0

    def test_save_and_reload(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ZfsBackupService(config, CredentialService())
        idx = ZfsSnapshotIndex(device_id="fw1")
        snap = ZfsSnapshot(name="zroot/ROOT@test", dataset="zroot/ROOT", tag="test")
        idx.add(snap)
        svc.save_snapshot_index(idx)

        loaded = svc.load_snapshot_index("fw1")
        assert len(loaded.snapshots) == 1
        assert loaded.snapshots[0].name == "zroot/ROOT@test"


class TestDetectZfs:
    def test_available(self):
        config = _make_config(Path("/tmp"))
        svc = ZfsBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_conn.exec_command.return_value = ("zroot/ROOT\n", "", 0)
        assert svc.detect_zfs(mock_conn) is True

    def test_unavailable(self):
        config = _make_config(Path("/tmp"))
        svc = ZfsBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_conn.exec_command.return_value = ("", "", 1)
        assert svc.detect_zfs(mock_conn) is False

    def test_empty_output(self):
        config = _make_config(Path("/tmp"))
        svc = ZfsBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_conn.exec_command.return_value = ("", "", 0)
        assert svc.detect_zfs(mock_conn) is False


class TestCreateSnapshot:
    def test_success(self):
        config = _make_config(Path("/tmp"))
        svc = ZfsBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_conn.exec_command.return_value = ("", "", 0)
        snap = svc.create_snapshot(mock_conn, "zroot/ROOT", "test-tag")
        assert snap.name == "zroot/ROOT@test-tag"
        assert snap.dataset == "zroot/ROOT"

    def test_failure_raises(self):
        config = _make_config(Path("/tmp"))
        svc = ZfsBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_conn.exec_command.return_value = ("", "dataset busy", 1)
        with pytest.raises(ZfsError, match="Failed to create"):
            svc.create_snapshot(mock_conn, "zroot/ROOT", "tag")


class TestTransferFull:
    @patch("pfsentinel.services.zfs_backup.checksum")
    def test_returns_record(self, mock_checksum, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ZfsBackupService(config, CredentialService())
        mock_conn = MagicMock()
        snap = ZfsSnapshot(name="zroot/ROOT@snap1", dataset="zroot/ROOT", tag="snap1")
        device = config.devices[0]

        def fake_stream(cmd, dest, timeout=1800):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"zfs stream data")
            return 15

        mock_conn.stream_command_to_file.side_effect = fake_stream
        mock_checksum.sha256_file.return_value = "hash123"

        record = svc.transfer_full(mock_conn, snap, device, TS, 1)
        assert record.zfs_snapshot_name == "zroot/ROOT@snap1"
        assert record.zfs_incremental is False


class TestTransferIncremental:
    @patch("pfsentinel.services.zfs_backup.checksum")
    def test_returns_incremental_record(self, mock_checksum, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ZfsBackupService(config, CredentialService())
        mock_conn = MagicMock()
        base = ZfsSnapshot(name="zroot/ROOT@base", dataset="zroot/ROOT", tag="base")
        current = ZfsSnapshot(name="zroot/ROOT@current", dataset="zroot/ROOT", tag="current")
        device = config.devices[0]

        def fake_stream(cmd, dest, timeout=1800):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"incr data")
            return 9

        mock_conn.stream_command_to_file.side_effect = fake_stream
        mock_checksum.sha256_file.return_value = "hash456"

        record = svc.transfer_incremental(mock_conn, base, current, device, TS, 1)
        assert record.zfs_incremental is True
        assert record.zfs_base_snapshot == "zroot/ROOT@base"


class TestCleanupRemote:
    def test_destroys_snapshots(self):
        config = _make_config(Path("/tmp"))
        svc = ZfsBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_conn.exec_command.return_value = ("", "", 0)
        snaps = [
            ZfsSnapshot(name="zroot/ROOT@old1", dataset="zroot/ROOT", tag="old1"),
            ZfsSnapshot(name="zroot/ROOT@old2", dataset="zroot/ROOT", tag="old2"),
        ]
        svc.cleanup_remote(mock_conn, snaps)
        assert mock_conn.exec_command.call_count == 2


class TestRunSnapshotBackup:
    def test_device_not_found_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ZfsBackupService(config, CredentialService())
        with pytest.raises(ZfsError, match="not found"):
            svc.run_snapshot_backup("nonexistent")

    @patch("pfsentinel.services.zfs_backup.SSHConnector")
    def test_zfs_not_available_raises(self, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ZfsBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.exec_command.return_value = ("", "", 1)

        with pytest.raises(ZfsError, match="not available"):
            svc.run_snapshot_backup("fw1")
