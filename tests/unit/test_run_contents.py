"""BackupPolicy.run_contents: what a scheduled run backs up, for display."""

from __future__ import annotations

import pytest

from pfsentinel.models.config import EXTRA_TARGET_FIELDS, BackupPolicy, ExtraBackupTargets


def test_a_new_policy_backs_up_the_config_only() -> None:
    assert BackupPolicy().run_contents() == "config"


def test_zfs_with_archive_names_the_archive_as_fallback() -> None:
    policy = BackupPolicy()
    policy.zfs.enabled = True
    policy.archive.enabled = True
    assert policy.run_contents() == "config, ZFS snapshot, filesystem archive if the snapshot fails"


def test_archive_alone() -> None:
    policy = BackupPolicy()
    policy.archive.enabled = True
    assert policy.run_contents() == "config, filesystem archive"


def test_every_target_name_maps_to_a_field() -> None:
    extras = ExtraBackupTargets()
    extras.set_targets(list(EXTRA_TARGET_FIELDS))
    assert extras.enabled_targets() == list(EXTRA_TARGET_FIELDS)


def test_set_targets_refuses_an_unknown_name() -> None:
    extras = ExtraBackupTargets(rrd=True)
    with pytest.raises(ValueError, match="zfs"):
        extras.set_targets(["zfs"])
    assert extras.rrd  # unchanged
