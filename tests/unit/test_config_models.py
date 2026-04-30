"""Tests for new config models: ExtraBackupTargets, ZfsPolicy, ArchivePolicy."""

from __future__ import annotations

from pfsentinel.models.config import (
    ArchivePolicy,
    BackupPolicy,
    ExtraBackupTargets,
    ZfsPolicy,
)


class TestExtraBackupTargets:
    def test_all_disabled_by_default(self):
        targets = ExtraBackupTargets()
        assert targets.rrd is False
        assert targets.package_configs is False
        assert targets.dhcp_leases is False
        assert targets.aliases is False
        assert targets.certificates is False
        assert targets.logs is False

    def test_enabled_targets_empty_by_default(self):
        targets = ExtraBackupTargets()
        assert targets.enabled_targets() == []

    def test_enabled_targets_returns_enabled(self):
        targets = ExtraBackupTargets(rrd=True, logs=True)
        enabled = targets.enabled_targets()
        assert "rrd" in enabled
        assert "logs" in enabled
        assert len(enabled) == 2

    def test_enabled_targets_all_on(self):
        targets = ExtraBackupTargets(
            rrd=True,
            package_configs=True,
            dhcp_leases=True,
            aliases=True,
            certificates=True,
            logs=True,
        )
        enabled = targets.enabled_targets()
        assert len(enabled) == 6

    def test_default_log_files(self):
        targets = ExtraBackupTargets()
        assert "/var/log/filter.log" in targets.log_files
        assert "/var/log/system.log" in targets.log_files

    def test_custom_paths_default_empty(self):
        targets = ExtraBackupTargets()
        assert targets.custom_paths == []


class TestZfsPolicy:
    def test_defaults(self):
        policy = ZfsPolicy()
        assert policy.enabled is False
        assert policy.dataset == "zroot/ROOT"
        assert policy.incremental is True
        assert policy.cleanup_remote is True
        assert policy.max_snapshots_remote == 3

    def test_custom_values(self):
        policy = ZfsPolicy(
            enabled=True,
            dataset="zroot/DATA",
            incremental=False,
            max_snapshots_remote=10,
        )
        assert policy.enabled is True
        assert policy.dataset == "zroot/DATA"
        assert policy.incremental is False
        assert policy.max_snapshots_remote == 10


class TestArchivePolicy:
    def test_defaults(self):
        policy = ArchivePolicy()
        assert policy.enabled is False
        assert "/cf/conf" in policy.directories
        assert "/usr/local/etc" in policy.directories
        assert "*.core" in policy.exclude_patterns

    def test_custom_directories(self):
        policy = ArchivePolicy(directories=["/etc", "/var/db"])
        assert policy.directories == ["/etc", "/var/db"]


class TestBackupPolicyExtensions:
    def test_extras_default(self):
        policy = BackupPolicy()
        assert isinstance(policy.extras, ExtraBackupTargets)
        assert policy.extras.rrd is False

    def test_zfs_default(self):
        policy = BackupPolicy()
        assert isinstance(policy.zfs, ZfsPolicy)
        assert policy.zfs.enabled is False

    def test_archive_default(self):
        policy = BackupPolicy()
        assert isinstance(policy.archive, ArchivePolicy)
        assert policy.archive.enabled is False

    def test_max_backups_per_type_defaults(self):
        policy = BackupPolicy()
        assert policy.max_backups_per_type["config"] == 30
        assert policy.max_backups_per_type["rrd"] == 10
        assert policy.max_backups_per_type["zfs"] == 5
        assert policy.max_backups_per_type["archive"] == 5
        assert policy.max_backups_per_type["logs"] == 7

    def test_backward_compatible_serialization(self):
        """A BackupPolicy without extras/zfs/archive fields should deserialize with defaults."""
        old_data = {
            "backup_root": None,
            "max_backups_per_device": 30,
            "compress": True,
            "validate_after_backup": True,
            "keep_days": 30,
            "secure_delete": False,
        }
        policy = BackupPolicy.model_validate(old_data)
        assert isinstance(policy.extras, ExtraBackupTargets)
        assert isinstance(policy.zfs, ZfsPolicy)
        assert isinstance(policy.archive, ArchivePolicy)
