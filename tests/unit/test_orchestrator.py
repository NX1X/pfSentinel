"""Tests for backup orchestrator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.backup import BackupRecord, BackupType, ChangeCategory
from pfsentinel.models.config import AppConfig, BackupPolicy
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.backup import BackupError
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.orchestrator import BackupOrchestrator


def _make_config(tmp_path: Path, devices=None) -> AppConfig:
    if devices is None:
        devices = [DeviceConfig(id="fw1", label="FW", host="10.0.0.1")]
    return AppConfig(
        devices=devices,
        backup_policy=BackupPolicy(backup_root=tmp_path / "backups"),
    )


def _make_record(device_id="fw1", backup_type=BackupType.CONFIG):
    return BackupRecord(
        device_id=device_id,
        filename="backup.xml.gz",
        relative_path="2025/03/05/backup.xml.gz",
        backup_type=backup_type,
        changes=[ChangeCategory.MINOR],
    )


class TestResolveTargets:
    def test_all_extras(self, tmp_path: Path):
        config = _make_config(tmp_path)
        orch = BackupOrchestrator(config, CredentialService())
        targets = orch._resolve_targets(None, all_extras=True)
        assert targets == ["rrd", "pkg", "dhcp", "aliases", "certs", "logs"]

    def test_explicit_include(self, tmp_path: Path):
        config = _make_config(tmp_path)
        orch = BackupOrchestrator(config, CredentialService())
        targets = orch._resolve_targets(["rrd", "dhcp"], all_extras=False)
        assert targets == ["rrd", "dhcp"]

    def test_configured_defaults_empty(self, tmp_path: Path):
        config = _make_config(tmp_path)
        orch = BackupOrchestrator(config, CredentialService())
        targets = orch._resolve_targets(None, all_extras=False)
        assert targets == []

    def test_configured_defaults_with_enabled(self, tmp_path: Path):
        config = _make_config(tmp_path)
        config.backup_policy.extras.rrd = True
        config.backup_policy.extras.dhcp_leases = True
        orch = BackupOrchestrator(config, CredentialService())
        targets = orch._resolve_targets(None, all_extras=False)
        assert "rrd" in targets
        assert "dhcp" in targets


class TestRun:
    @patch.object(BackupOrchestrator, "_resolve_targets", return_value=[])
    def test_config_only(self, mock_resolve, tmp_path: Path):
        config = _make_config(tmp_path)
        orch = BackupOrchestrator(config, CredentialService())
        with patch.object(orch._backup_svc, "run_backup", return_value=_make_record()):
            records = orch.run("fw1", config_only=True)
        assert len(records) == 1
        assert records[0].backup_type == BackupType.CONFIG

    def test_device_not_found_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        orch = BackupOrchestrator(config, CredentialService())
        with pytest.raises(BackupError, match="not found"):
            orch.run("nonexistent")

    @patch.object(BackupOrchestrator, "_resolve_targets", return_value=["rrd"])
    def test_extra_failure_continues(self, mock_resolve, tmp_path: Path):
        config = _make_config(tmp_path)
        orch = BackupOrchestrator(config, CredentialService())

        with (
            patch.object(orch._backup_svc, "run_backup", return_value=_make_record()),
            patch.object(orch._extra_svc, "backup_target", side_effect=Exception("RRD fail")),
            patch.object(orch._retention, "load_index"),
            patch.object(orch._retention, "save_index"),
            patch.object(orch._retention, "next_sequence", return_value=1),
            patch.object(orch._retention, "apply"),
        ):
            records = orch.run("fw1")
        # Config succeeded, RRD failed — should still return config record
        assert len(records) == 1

    @patch.object(BackupOrchestrator, "_resolve_targets", return_value=["rrd"])
    def test_extra_failure_calls_on_warning(self, mock_resolve, tmp_path: Path):
        config = _make_config(tmp_path)
        orch = BackupOrchestrator(config, CredentialService())
        warn_cb = MagicMock()

        with (
            patch.object(orch._backup_svc, "run_backup", return_value=_make_record()),
            patch.object(orch._extra_svc, "backup_target", side_effect=Exception("RRD fail")),
            patch.object(orch._retention, "load_index"),
            patch.object(orch._retention, "save_index"),
            patch.object(orch._retention, "next_sequence", return_value=1),
            patch.object(orch._retention, "apply"),
        ):
            records = orch.run("fw1", on_warning=warn_cb)
        assert len(records) == 1
        warn_cb.assert_called_once()
        assert "rrd" in warn_cb.call_args[0][0].lower()
        assert "RRD fail" in warn_cb.call_args[0][0]


class TestRunAll:
    def test_no_enabled_devices_raises(self, tmp_path: Path):
        config = _make_config(tmp_path, devices=[])
        orch = BackupOrchestrator(config, CredentialService())
        with pytest.raises(BackupError, match="No enabled"):
            orch.run_all()

    def test_multiple_devices(self, tmp_path: Path):
        devices = [
            DeviceConfig(id="fw1", label="FW1", host="10.0.0.1"),
            DeviceConfig(id="fw2", label="FW2", host="10.0.0.2"),
        ]
        config = _make_config(tmp_path, devices=devices)
        orch = BackupOrchestrator(config, CredentialService())
        with (
            patch.object(
                orch,
                "run",
                side_effect=[
                    [_make_record("fw1")],
                    [_make_record("fw2")],
                ],
            ),
        ):
            records = orch.run_all()
        assert len(records) == 2

    def test_one_failure_continues(self, tmp_path: Path):
        devices = [
            DeviceConfig(id="fw1", label="FW1", host="10.0.0.1"),
            DeviceConfig(id="fw2", label="FW2", host="10.0.0.2"),
        ]
        config = _make_config(tmp_path, devices=devices)
        orch = BackupOrchestrator(config, CredentialService())
        with (
            patch.object(
                orch,
                "run",
                side_effect=[
                    Exception("fw1 failed"),
                    [_make_record("fw2")],
                ],
            ),
        ):
            records = orch.run_all()
        assert len(records) == 1
        assert records[0].device_id == "fw2"
