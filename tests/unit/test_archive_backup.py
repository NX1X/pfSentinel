"""Tests for filesystem tar archive backup service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.config import AppConfig, ArchivePolicy, BackupPolicy
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.archive_backup import ArchiveBackupError, ArchiveBackupService
from pfsentinel.services.credentials import CredentialService


def _make_config(tmp_path: Path) -> AppConfig:
    device = DeviceConfig(id="fw1", label="FW", host="10.0.0.1")
    return AppConfig(
        devices=[device],
        backup_policy=BackupPolicy(
            backup_root=tmp_path / "backups",
            archive=ArchivePolicy(enabled=True),
        ),
    )


class TestGetSshConnector:
    def test_device_not_found_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        creds = CredentialService()
        svc = ArchiveBackupService(config, creds)
        with pytest.raises(ArchiveBackupError, match="not found"):
            svc._get_ssh_connector("nonexistent")

    def test_creates_connector(self, tmp_path: Path):
        config = _make_config(tmp_path)
        creds = CredentialService()
        creds.store("fw1", "pass123")
        svc = ArchiveBackupService(config, creds)
        connector = svc._get_ssh_connector("fw1")
        assert connector.device.id == "fw1"


class TestRunArchiveBackup:
    def test_device_not_found_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        creds = CredentialService()
        svc = ArchiveBackupService(config, creds)
        with pytest.raises(ArchiveBackupError, match="not found"):
            svc.run_archive_backup("nonexistent")

    def test_no_directories_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        config.backup_policy.archive.directories = []
        creds = CredentialService()
        svc = ArchiveBackupService(config, creds)
        with pytest.raises(ArchiveBackupError, match="No directories"):
            svc.run_archive_backup("fw1", directories=[])

    @patch("pfsentinel.services.archive_backup.SSHConnector")
    @patch("pfsentinel.services.archive_backup.checksum")
    def test_happy_path(self, mock_checksum, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        creds = CredentialService()
        svc = ArchiveBackupService(config, creds)

        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        def fake_stream(cmd, dest, timeout=600, warn_exit_codes=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake tar data here")
            return 18

        mock_conn.stream_command_to_file.side_effect = fake_stream
        mock_checksum.sha256_file.return_value = "abc123"

        record = svc.run_archive_backup("fw1")
        assert record.device_id == "fw1"
        assert record.backup_type.value == "archive"
        assert record.compressed is True

    @patch("pfsentinel.services.archive_backup.SSHConnector")
    def test_zero_bytes_raises(self, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        creds = CredentialService()
        svc = ArchiveBackupService(config, creds)

        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        def fake_stream(cmd, dest, timeout=600, warn_exit_codes=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"")
            return 0

        mock_conn.stream_command_to_file.side_effect = fake_stream

        with pytest.raises(ArchiveBackupError, match="No archive data"):
            svc.run_archive_backup("fw1")

    @patch("pfsentinel.services.archive_backup.SSHConnector")
    @patch("pfsentinel.services.archive_backup.checksum")
    def test_progress_callback(self, mock_checksum, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        creds = CredentialService()
        svc = ArchiveBackupService(config, creds)

        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        def fake_stream(cmd, dest, timeout=600, warn_exit_codes=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"data")
            return 4

        mock_conn.stream_command_to_file.side_effect = fake_stream
        mock_checksum.sha256_file.return_value = "abc"

        progress = MagicMock()
        svc.run_archive_backup("fw1", progress=progress)
        assert progress.call_count >= 3
