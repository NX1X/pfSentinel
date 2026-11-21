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

    @pytest.mark.parametrize("config_only", [False, True])
    @patch.object(BackupOrchestrator, "_resolve_targets", return_value=[])
    def test_second_copy_runs_without_extras(self, mock_resolve, tmp_path: Path, config_only):
        """Before 1.0 a run with no extras (the default) returned before the second copy."""
        config = _make_config(tmp_path)
        orch = BackupOrchestrator(config, CredentialService())
        with (
            patch.object(orch._backup_svc, "run_backup", return_value=_make_record()),
            patch.object(orch._retention, "start_run"),
            patch.object(orch._retention, "apply") as apply,
            patch.object(orch, "_second_copy") as second_copy,
        ):
            orch.run("fw1", config_only=config_only)
        apply.assert_called_once_with("fw1")
        second_copy.assert_called_once_with("fw1")

    @patch.object(BackupOrchestrator, "_resolve_targets", return_value=[])
    def test_zfs_snapshot_runs_without_extras(self, mock_resolve, tmp_path: Path):
        config = _make_config(tmp_path)
        config.backup_policy.zfs.enabled = True
        orch = BackupOrchestrator(config, CredentialService())
        zfs = _make_record(backup_type=BackupType.ZFS_SNAPSHOT)
        with (
            patch.object(orch._backup_svc, "run_backup", return_value=_make_record()),
            patch.object(orch._retention, "start_run"),
            patch.object(orch._retention, "apply"),
            patch.object(orch, "_run_zfs_snapshot", return_value=zfs) as run_zfs,
        ):
            records = orch.run("fw1")
        run_zfs.assert_called_once()
        assert records[-1] is zfs

    def test_config_only_skips_zfs_and_archive(self, tmp_path: Path):
        config = _make_config(tmp_path)
        config.backup_policy.zfs.enabled = True
        config.backup_policy.archive.enabled = True
        orch = BackupOrchestrator(config, CredentialService())
        with (
            patch.object(orch._backup_svc, "run_backup", return_value=_make_record()),
            patch.object(orch._retention, "start_run"),
            patch.object(orch._retention, "apply"),
            patch.object(orch, "_run_zfs_snapshot") as run_zfs,
            patch.object(orch, "_run_archive") as run_archive,
        ):
            records = orch.run("fw1", config_only=True)
        assert len(records) == 1
        run_zfs.assert_not_called()
        run_archive.assert_not_called()

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


class TestRunSafety:
    """Found in the bug hunt before rc.3."""

    @patch.object(BackupOrchestrator, "_resolve_targets", return_value=["rrd", "pkg"])
    def test_failed_parts_send_one_partial_alert(self, mock_resolve, tmp_path: Path):
        from pfsentinel.services.notifications import AlertKind

        orch = BackupOrchestrator(_make_config(tmp_path), CredentialService())
        with (
            patch.object(orch._backup_svc, "run_backup", return_value=_make_record()),
            patch.object(orch._extra_svc, "backup_target", side_effect=Exception("timeout")),
            patch.object(orch._retention, "apply"),
            patch.object(orch._backup_svc.notifications, "alert") as alert,
        ):
            orch.run("fw1")
        alert.assert_called_once()
        kind, title, message = alert.call_args.args
        assert kind is AlertKind.BACKUP_PARTIAL
        assert "Extra backup 'rrd' failed: timeout" in message
        assert "Extra backup 'pkg' failed: timeout" in message

    def test_a_failed_archive_is_reported(self, tmp_path: Path):
        config = _make_config(tmp_path)
        config.backup_policy.archive.enabled = True
        orch = BackupOrchestrator(config, CredentialService())
        warn_cb = MagicMock()
        with (
            patch.object(orch._backup_svc, "run_backup", return_value=_make_record()),
            patch.object(orch, "_resolve_targets", return_value=[]),
            patch.object(orch, "_run_archive", side_effect=RuntimeError("tar failed")),
            patch.object(orch._retention, "apply"),
            patch.object(orch._backup_svc.notifications, "alert") as alert,
        ):
            orch.run("fw1", on_warning=warn_cb)
        assert "Filesystem archive failed: tar failed" in warn_cb.call_args.args[0]
        alert.assert_called_once()

    def test_a_clean_run_sends_no_partial_alert(self, tmp_path: Path):
        orch = BackupOrchestrator(_make_config(tmp_path), CredentialService())
        with (
            patch.object(orch._backup_svc, "run_backup", return_value=_make_record()),
            patch.object(orch, "_resolve_targets", return_value=[]),
            patch.object(orch._retention, "apply"),
            patch.object(orch._backup_svc.notifications, "alert") as alert,
        ):
            orch.run("fw1")
        alert.assert_not_called()

    def test_an_unusable_backup_folder_alerts(self, tmp_path: Path):
        """F: unplugged at 02:00: before rc.3 the run failed with no alert."""
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("x")
        config = _make_config(tmp_path)
        config.backup_policy.backup_root = blocker / "backups"
        orch = BackupOrchestrator(config, CredentialService())
        with patch.object(orch._backup_svc.notifications, "notify_failure") as notify:
            with pytest.raises(BackupError, match="Cannot use the backup folder"):
                orch.run("fw1")
        notify.assert_called_once()
        assert "Cannot use the backup folder" in notify.call_args.args[1]

    def test_a_held_lock_fails_the_run_with_an_alert(self, tmp_path: Path):
        from pfsentinel.utils.locking import LockTimeoutError

        orch = BackupOrchestrator(_make_config(tmp_path), CredentialService())
        with (
            patch.object(
                orch._retention, "device_lock", side_effect=LockTimeoutError("held too long")
            ),
            patch.object(orch._backup_svc.notifications, "notify_failure") as notify,
            patch.object(orch._backup_svc, "run_backup") as run_backup,
        ):
            with pytest.raises(BackupError, match="held too long"):
                orch.run("fw1")
        notify.assert_called_once()
        run_backup.assert_not_called()


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
