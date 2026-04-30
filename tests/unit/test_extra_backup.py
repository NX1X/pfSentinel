"""Tests for extra backup service (RRD, DHCP, packages, etc.)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.backup import BackupType
from pfsentinel.models.config import AppConfig, BackupPolicy
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.extra_backup import ExtraBackupError, ExtraBackupService

TS = datetime(2025, 3, 5, 14, 30, 22)


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        devices=[DeviceConfig(id="fw1", label="FW", host="10.0.0.1")],
        backup_policy=BackupPolicy(backup_root=tmp_path / "backups"),
    )


def _make_device() -> DeviceConfig:
    return DeviceConfig(id="fw1", label="FW", host="10.0.0.1")


class TestBackupTargetDispatch:
    def test_unknown_target_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ExtraBackupService(config, CredentialService())
        with pytest.raises(ExtraBackupError, match="Unknown"):
            svc.backup_target("nosuch", _make_device(), TS, 1)

    @pytest.mark.parametrize("target", ["rrd", "pkg", "dhcp", "aliases", "certs", "logs"])
    def test_valid_targets_are_dispatched(self, target, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ExtraBackupService(config, CredentialService())
        # Patch the handler to verify dispatch without running
        with patch.object(
            svc,
            f"backup_{({'dhcp': 'dhcp_leases', 'certs': 'certificates', 'pkg': 'package_configs'}).get(target, target)}",
        ) as m:
            if target == "pkg":
                with patch.object(svc, "backup_package_configs") as m2:
                    m2.return_value = MagicMock()
                    svc.backup_target(target, _make_device(), TS, 1)
                    m2.assert_called_once()
            else:
                handler_name = {
                    "rrd": "backup_rrd",
                    "dhcp": "backup_dhcp_leases",
                    "aliases": "backup_aliases",
                    "certs": "backup_certificates",
                    "logs": "backup_logs",
                }.get(target, target)
                with patch.object(svc, handler_name) as m3:
                    m3.return_value = MagicMock()
                    svc.backup_target(target, _make_device(), TS, 1)
                    m3.assert_called_once()


class TestBackupRrd:
    @patch("pfsentinel.services.extra_backup.SSHConnector")
    def test_no_rrd_files_raises(self, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ExtraBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.list_remote_files.return_value = []

        with pytest.raises(ExtraBackupError, match="No RRD"):
            svc.backup_rrd(_make_device(), TS, 1)

    @patch("pfsentinel.services.extra_backup.SSHConnector")
    def test_no_downloaded_files_raises(self, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ExtraBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.list_remote_files.return_value = ["/var/db/rrd/cpu.rrd"]
        mock_conn.download_files.return_value = []

        with pytest.raises(ExtraBackupError, match="Failed to download"):
            svc.backup_rrd(_make_device(), TS, 1)

    @patch("pfsentinel.services.extra_backup.SSHConnector")
    def test_happy_path(self, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ExtraBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.list_remote_files.return_value = ["/var/db/rrd/cpu.rrd"]

        # Create a real temp file so tarfile can work
        fake_file = tmp_path / "cpu.rrd"
        fake_file.write_bytes(b"rrd data")
        mock_conn.download_files.return_value = [fake_file]

        record = svc.backup_rrd(_make_device(), TS, 1)
        assert record.backup_type == BackupType.RRD
        assert record.device_id == "fw1"


class TestBackupPackageConfigs:
    @patch("pfsentinel.services.extra_backup.SSHConnector")
    def test_zero_bytes_raises(self, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ExtraBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.stream_command_to_file.return_value = 0

        with pytest.raises(ExtraBackupError, match="No package config"):
            svc.backup_package_configs(_make_device(), TS, 1)

    @patch("pfsentinel.services.extra_backup.SSHConnector")
    @patch("pfsentinel.services.extra_backup.checksum")
    def test_happy_path(self, mock_checksum, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ExtraBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        def fake_stream(cmd, dest, timeout=120, warn_exit_codes=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"pkg tar data")
            return 12

        mock_conn.stream_command_to_file.side_effect = fake_stream
        mock_checksum.sha256_file.return_value = "hash"

        record = svc.backup_package_configs(_make_device(), TS, 1)
        assert record.backup_type == BackupType.PACKAGE_CONFIGS


class TestBackupDhcpLeases:
    @patch("pfsentinel.services.extra_backup.SSHConnector")
    def test_download_failure_raises(self, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ExtraBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.download_file.side_effect = Exception("SFTP error")

        with pytest.raises(ExtraBackupError, match="Failed to download DHCP"):
            svc.backup_dhcp_leases(_make_device(), TS, 1)


class TestBackupLogs:
    @patch("pfsentinel.services.extra_backup.SSHConnector")
    def test_no_downloaded_raises(self, mock_ssh_cls, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = ExtraBackupService(config, CredentialService())
        mock_conn = MagicMock()
        mock_ssh_cls.return_value = mock_conn
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.download_files.return_value = []

        with pytest.raises(ExtraBackupError, match="Failed to download"):
            svc.backup_logs(_make_device(), TS, 1)

    def test_no_log_files_configured_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        config.backup_policy.extras.log_files = []
        svc = ExtraBackupService(config, CredentialService())
        with pytest.raises(ExtraBackupError, match="No log files configured"):
            svc.backup_logs(_make_device(), TS, 1)
