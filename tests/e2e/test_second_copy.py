"""Second copy of the encrypted backups: a folder target and an SFTP target."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import paramiko
import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.services import host_keys
from pfsentinel.services.backup_crypto import BackupCrypto
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.notifications import AlertKind, NotificationService
from pfsentinel.services.retention import RetentionService
from pfsentinel.services.second_copy import SecondCopyService
from tests.unit.test_config_checks import BASE

from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

RUN = ["backup", "run", "-d", "home-fw", "--include", "dhcp", "--no-notify"]


def _output(result) -> str:
    return (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def with_leases(ssh_server: FakeSSHServer) -> FakeSSHServer:
    # A realistic config, so the restore check passes and only copy alerts show.
    ssh_server.set_file("/cf/conf/config.xml", BASE)
    ssh_server.set_file("/var/dhcpd/var/db/dhcpd.leases", b"lease 192.0.2.50 { }\n")
    return ssh_server


@pytest.fixture
def alerts():
    sent: list[tuple[AlertKind, str]] = []

    def capture(self, kind, title, message):
        sent.append((kind, message))
        return {}

    with patch.object(NotificationService, "alert", capture):
        yield sent


def _encrypt_and_copy_to(config: AppConfig, **copy) -> AppConfig:
    config = AppConfig.load()
    BackupCrypto(config.backup_policy.encryption, CredentialService()).setup_keystore()
    policy = config.backup_policy.second_copy
    for key, value in {"enabled": True, **copy}.items():
        setattr(policy, key, value)
    config.save()
    return config


def _local_files(config: AppConfig) -> dict[str, bytes]:
    root = config.backup_policy.resolved_root
    return {"/".join(p.relative_to(root).parts): p.read_bytes() for p in root.rglob("*.enc")}


def test_folder_target_gets_every_encrypted_file(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, tmp_path: Path, alerts
) -> None:
    target = tmp_path / "nextcloud"
    target.mkdir()
    config = _encrypt_and_copy_to(seeded_config, kind="folder", path=str(target))

    result = cli_runner.invoke(app, RUN)
    assert result.exit_code == 0, _output(result)

    local = _local_files(config)
    assert len(local) == 2
    copied = {"/".join(p.relative_to(target).parts): p.read_bytes() for p in target.rglob("*.enc")}
    assert copied == local
    assert not list(target.rglob("*.part"))
    assert alerts == []


def test_plain_files_never_leave_the_pc(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, tmp_path: Path
) -> None:
    target = tmp_path / "share"
    target.mkdir()
    assert cli_runner.invoke(app, RUN).exit_code == 0  # plain backups
    config = AppConfig.load()
    config.backup_policy.second_copy.enabled = True
    config.backup_policy.second_copy.path = str(target)
    result = SecondCopyService(config, CredentialService()).sync("home-fw")
    assert len(result.skipped_plain) == 2
    assert not [p for p in target.rglob("*") if p.is_file()]


def test_a_missing_target_alerts_and_the_next_run_catches_up(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, tmp_path: Path, alerts
) -> None:
    target = tmp_path / "offline-share"
    _encrypt_and_copy_to(seeded_config, kind="folder", path=str(target))

    first = cli_runner.invoke(app, RUN)
    assert first.exit_code == 0, "a failed copy never fails the backup"
    assert [k for k, _ in alerts] == [AlertKind.SECOND_COPY_FAILED]
    assert "does not exist" in alerts[0][1]

    target.mkdir()  # the share is back
    alerts.clear()
    assert cli_runner.invoke(app, RUN).exit_code == 0
    assert alerts == []
    assert len(list(target.rglob("*.enc"))) == 4, "both runs are on the target now"


def test_target_retention_removes_old_day_folders(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, tmp_path: Path
) -> None:
    target = tmp_path / "share"
    old_day = target / "home-fw" / "2020" / "01" / "02" / "020000_#001"
    old_day.mkdir(parents=True)
    (old_day / "old.xml.gz.enc").write_bytes(b"x")
    unrelated = target / "home-fw" / "notes.txt"
    unrelated.write_text("keep me")
    _encrypt_and_copy_to(seeded_config, kind="folder", path=str(target), keep_days=90)

    assert cli_runner.invoke(app, RUN).exit_code == 0
    assert not (target / "home-fw" / "2020" / "01" / "02").exists()
    assert unrelated.read_text() == "keep me"
    today = datetime.now()
    assert (target / "home-fw" / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}").is_dir()


def test_sftp_target_with_a_pinned_host_key(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, tmp_path: Path, alerts
) -> None:
    store = tmp_path / "sftp-store"
    store.mkdir()
    client_key = paramiko.ECDSAKey.generate()
    key_file = tmp_path / "copy_key"
    client_key.write_private_key_file(str(key_file))
    server = FakeSSHServer.start(username="pfs-copy", sftp_root=store, authorized_key=client_key)
    try:
        config = _encrypt_and_copy_to(
            seeded_config,
            kind="sftp",
            host=server.host,
            port=server.port,
            username="pfs-copy",
            key_path=str(key_file),
            path="backups/pfsentinel",
        )

        # Not pinned yet: refused, and the backup still succeeds.
        assert cli_runner.invoke(app, RUN).exit_code == 0
        assert [k for k, _ in alerts] == [AlertKind.SECOND_COPY_FAILED]
        assert "not trusted" in alerts[0][1]

        host_keys.trust(
            server.host, server.port, host_keys.fetch_host_key(server.host, server.port)
        )
        alerts.clear()
        assert cli_runner.invoke(app, RUN).exit_code == 0
        assert alerts == []
        local = _local_files(config)
        remote = {
            "/".join(p.relative_to(store / "backups" / "pfsentinel").parts): p.read_bytes()
            for p in store.rglob("*.enc")
        }
        assert remote == local and len(remote) == 4
    finally:
        server.stop()


def test_copy_setup_refuses_without_encryption(
    cli_runner: CliRunner, seeded_config: AppConfig
) -> None:
    result = cli_runner.invoke(app, ["copy", "setup"])
    assert result.exit_code == 1
    assert "encryption" in _output(result).lower()


def test_old_records_outside_keep_days_are_not_copied(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, tmp_path: Path
) -> None:
    target = tmp_path / "share"
    target.mkdir()
    config = _encrypt_and_copy_to(seeded_config, kind="folder", path=str(target), keep_days=30)
    assert cli_runner.invoke(app, RUN).exit_code == 0
    for p in list(target.rglob("*.enc")):
        p.unlink()
    retention = RetentionService(config.backup_policy.resolved_root, config.backup_policy)
    index = retention.load_index("home-fw")
    for record in index.records:
        record.created_at = datetime.now() - timedelta(days=40)
    retention.save_index(index)

    result = SecondCopyService(AppConfig.load(), CredentialService()).sync("home-fw")
    assert result.copied == []


def test_older_plain_files_raise_no_alert_once_encryption_is_on(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, tmp_path: Path, alerts
) -> None:
    """Before rc.3 every run alerted 'turn on encryption' with encryption on."""
    target = tmp_path / "share"
    target.mkdir()
    assert cli_runner.invoke(app, RUN).exit_code == 0  # plain backups first
    _encrypt_and_copy_to(seeded_config, kind="folder", path=str(target))

    assert cli_runner.invoke(app, RUN).exit_code == 0
    assert alerts == []
    assert len(list(target.rglob("*.enc"))) == 2


def test_a_folder_target_inside_the_backup_folder_is_never_cleaned(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, alerts
) -> None:
    root = seeded_config.backup_policy.resolved_root
    old_day = root / "home-fw" / "2020" / "01" / "02"
    old_day.mkdir(parents=True)
    keeper = old_day / "keep.xml.gz.enc"
    keeper.write_bytes(b"x")
    _encrypt_and_copy_to(seeded_config, kind="folder", path=str(root), keep_days=30)

    assert cli_runner.invoke(app, RUN).exit_code == 0
    assert keeper.exists()
    assert [k for k, _ in alerts] == [AlertKind.SECOND_COPY_FAILED]
    assert "overlaps the backup folder" in alerts[0][1]


def test_a_failed_folder_copy_leaves_no_part_file(tmp_path: Path) -> None:
    from pfsentinel.services.second_copy import FolderTarget

    local = tmp_path / "a.xml.gz.enc"
    local.write_bytes(b"data")
    share = tmp_path / "share"
    share.mkdir()

    def half_copy(src, dst):
        Path(dst).write_bytes(b"da")
        raise OSError("disk full")

    with patch("pfsentinel.services.second_copy.shutil.copyfile", half_copy):
        with pytest.raises(OSError, match="disk full"):
            FolderTarget(share).put(local, "fw/2026/11/21/a.xml.gz.enc")
    assert [p.name for p in share.rglob("*") if p.is_file()] == []
