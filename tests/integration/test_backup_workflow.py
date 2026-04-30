"""Integration tests for the full backup workflow with mocked SSH."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pfsentinel.models.config import AppConfig, BackupPolicy
from pfsentinel.models.device import ConnectionMethod, DeviceConfig
from pfsentinel.services.backup import BackupService
from pfsentinel.services.credentials import CredentialService


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    policy = BackupPolicy(backup_root=tmp_path, max_backups_per_device=10, compress=True)
    config = AppConfig(backup_policy=policy)
    device = DeviceConfig(
        id="home-fw",
        label="Home pfSense",
        host="192.168.1.1",
        primary_method=ConnectionMethod.SSH,
    )
    config.add_device(device)
    return config


@pytest.fixture
def creds(config: AppConfig) -> CredentialService:
    creds = CredentialService()
    creds.store("home-fw", "test_password")
    return creds


@pytest.fixture
def backup_service(config: AppConfig, creds: CredentialService) -> BackupService:
    return BackupService(config, creds)


class TestBackupWorkflow:
    def test_full_backup_with_mocked_ssh(
        self, backup_service: BackupService, sample_xml: str, tmp_path: Path
    ):
        """Full backup workflow with mocked SSH connection."""
        with (
            patch("pfsentinel.services.connection.SSHConnector.connect"),
            patch(
                "pfsentinel.services.connection.SSHConnector.download_config",
                return_value=sample_xml,
            ),
            patch("pfsentinel.services.connection.SSHConnector.disconnect"),
        ):
            progress_calls = []

            def track_progress(msg: str, pct: int) -> None:
                progress_calls.append((msg, pct))

            record = backup_service.run_backup("home-fw", progress=track_progress)

        # Verify record properties
        assert record.device_id == "home-fw"
        assert record.connection_method == "ssh"
        assert record.device_hostname == "home-fw"
        assert record.compressed is True
        assert record.sha256 != ""
        assert record.size_bytes > 0
        assert record.verified is True

        # Verify file was created
        backup_root = backup_service._backup_root
        file_path = backup_root / "home-fw" / record.relative_path
        assert file_path.exists()

        # Verify index was updated
        from pfsentinel.services.retention import RetentionService

        ret_svc = RetentionService(backup_root, backup_service._config.backup_policy)
        index = ret_svc.load_index("home-fw")
        assert len(index.records) == 1
        assert index.records[0].id == record.id

        # Verify progress was reported
        assert any(pct == 100 for _, pct in progress_calls)

    def test_backup_creates_initial_category(self, backup_service: BackupService, sample_xml: str):
        """First backup should be marked as INITIAL."""
        from pfsentinel.models.backup import ChangeCategory

        with (
            patch("pfsentinel.services.connection.SSHConnector.connect"),
            patch(
                "pfsentinel.services.connection.SSHConnector.download_config",
                return_value=sample_xml,
            ),
            patch("pfsentinel.services.connection.SSHConnector.disconnect"),
        ):
            record = backup_service.run_backup("home-fw")

        assert ChangeCategory.INITIAL in record.changes

    def test_verify_backup(self, backup_service: BackupService, sample_xml: str):
        """Verify should return True for a just-created backup."""
        with (
            patch("pfsentinel.services.connection.SSHConnector.connect"),
            patch(
                "pfsentinel.services.connection.SSHConnector.download_config",
                return_value=sample_xml,
            ),
            patch("pfsentinel.services.connection.SSHConnector.disconnect"),
        ):
            record = backup_service.run_backup("home-fw")

        ok = backup_service.verify_backup(record)
        assert ok is True

    def test_list_backups(self, backup_service: BackupService, sample_xml: str):
        """Listed backups should include created backup."""
        with (
            patch("pfsentinel.services.connection.SSHConnector.connect"),
            patch(
                "pfsentinel.services.connection.SSHConnector.download_config",
                return_value=sample_xml,
            ),
            patch("pfsentinel.services.connection.SSHConnector.disconnect"),
        ):
            record = backup_service.run_backup("home-fw")

        records = backup_service.list_backups("home-fw")
        assert len(records) == 1
        assert records[0].id == record.id

    def test_delete_backup(self, backup_service: BackupService, sample_xml: str):
        """Deleted backup should no longer appear in list."""
        with (
            patch("pfsentinel.services.connection.SSHConnector.connect"),
            patch(
                "pfsentinel.services.connection.SSHConnector.download_config",
                return_value=sample_xml,
            ),
            patch("pfsentinel.services.connection.SSHConnector.disconnect"),
        ):
            record = backup_service.run_backup("home-fw")

        backup_service.delete_backup(record)
        records = backup_service.list_backups("home-fw")
        assert len(records) == 0

    def test_restore_backup(self, backup_service: BackupService, sample_xml: str, tmp_path: Path):
        """Restore should create decompressed XML file at target."""
        with (
            patch("pfsentinel.services.connection.SSHConnector.connect"),
            patch(
                "pfsentinel.services.connection.SSHConnector.download_config",
                return_value=sample_xml,
            ),
            patch("pfsentinel.services.connection.SSHConnector.disconnect"),
        ):
            record = backup_service.run_backup("home-fw")

        restore_dir = tmp_path / "restore"
        restore_dir.mkdir()
        dest = backup_service.restore_backup(record, restore_dir)

        assert dest.exists()
        content = dest.read_text()
        assert "<pfsense" in content

    def test_backup_unknown_device_raises(self, backup_service: BackupService):
        from pfsentinel.services.backup import BackupError

        with pytest.raises(BackupError, match="not found"):
            backup_service.run_backup("nonexistent-device")

    def test_backup_invalid_xml_raises(self, backup_service: BackupService):
        from pfsentinel.services.backup import BackupError

        with (
            patch("pfsentinel.services.connection.SSHConnector.connect"),
            patch(
                "pfsentinel.services.connection.SSHConnector.download_config",
                return_value="NOT XML AT ALL",
            ),
            patch("pfsentinel.services.connection.SSHConnector.disconnect"),
        ):
            with pytest.raises(BackupError, match="not valid"):
                backup_service.run_backup("home-fw")
