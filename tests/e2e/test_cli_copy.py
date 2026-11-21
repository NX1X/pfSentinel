"""`pfs copy` (setup, now, status, disable) through the CLI.

Folder targets use tmp_path. SFTP targets use a second FakeSSHServer with a
writable folder and key login, so the SSH transport is real.
"""

from __future__ import annotations

import secrets
import socket
from collections.abc import Iterator
from dataclasses import dataclass
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

from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

RUN = ["backup", "run", "-d", "home-fw", "--config-only", "--no-notify"]


def _out(result) -> str:
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


def _encrypt() -> None:
    config = AppConfig.load()
    BackupCrypto(config.backup_policy.encryption, CredentialService()).setup_keystore()
    config.save()


def _copy_to_folder(path: Path) -> None:
    config = AppConfig.load()
    policy = config.backup_policy.second_copy
    policy.enabled, policy.kind, policy.path = True, "folder", str(path)
    config.save()


@pytest.fixture
def encrypted(seeded_config: AppConfig) -> AppConfig:
    _encrypt()
    return AppConfig.load()


@pytest.fixture
def share(tmp_path: Path) -> Path:
    path = tmp_path / "share"
    path.mkdir()
    return path


@dataclass
class SftpTarget:
    server: FakeSSHServer
    store: Path
    key_file: Path

    def answers(self, *, passphrase: str = "", trust: str = "y", keep_days: int = 30) -> str:
        """Input for `pfs copy setup` choosing this SFTP server."""
        lines = [
            "sftp",
            self.server.host,
            str(self.server.port),
            "pfs-copy",
            str(self.key_file),
            "backups/pfsentinel",
            passphrase,
            trust,
            str(keep_days),
        ]
        return "\n".join(lines) + "\n"


@pytest.fixture
def sftp_target(tmp_path: Path) -> Iterator[SftpTarget]:
    store = tmp_path / "sftp-store"
    store.mkdir()
    client_key = paramiko.ECDSAKey.generate()
    key_file = tmp_path / "copy_key"
    client_key.write_private_key_file(str(key_file))
    server = FakeSSHServer.start(username="pfs-copy", sftp_root=store, authorized_key=client_key)
    try:
        yield SftpTarget(server=server, store=store, key_file=key_file)
    finally:
        server.stop()


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# copy setup: folder
# ---------------------------------------------------------------------------


def test_setup_folder_target_turns_the_copy_on(
    cli_runner: CliRunner, encrypted: AppConfig, share: Path
) -> None:
    result = cli_runner.invoke(app, ["copy", "setup"], input=f"folder\n{share}\n45\n")

    assert result.exit_code == 0, _out(result)
    policy = AppConfig.load().backup_policy.second_copy
    assert policy.enabled is True
    assert policy.kind == "folder"
    assert Path(policy.path) == share
    assert policy.keep_days == 45


def test_setup_folder_that_does_not_exist_is_refused(
    cli_runner: CliRunner, encrypted: AppConfig, tmp_path: Path
) -> None:
    missing = tmp_path / "nowhere"
    result = cli_runner.invoke(app, ["copy", "setup"], input=f"folder\n{missing}\n")

    assert result.exit_code == 1
    assert "does not exist" in _out(result)
    assert AppConfig.load().backup_policy.second_copy.enabled is False


@pytest.mark.parametrize("where", ["root", "inside", "parent"])
def test_setup_folder_that_overlaps_the_backup_folder_is_refused(
    cli_runner: CliRunner, encrypted: AppConfig, where: str
) -> None:
    """The copy's cleanup removes old day folders: here it would delete local backups."""
    root = encrypted.backup_policy.resolved_root
    root.mkdir(parents=True, exist_ok=True)
    folder = {"root": root, "inside": root / "copy", "parent": root.parent}[where]
    folder.mkdir(exist_ok=True)
    result = cli_runner.invoke(app, ["copy", "setup"], input=f"folder\n{folder}\n")

    assert result.exit_code == 1
    assert "overlaps the backup folder" in _out(result)
    assert AppConfig.load().backup_policy.second_copy.enabled is False


def test_setup_unknown_target_kind_is_refused(cli_runner: CliRunner, encrypted: AppConfig) -> None:
    result = cli_runner.invoke(app, ["copy", "setup"], input="dropbox\n")

    assert result.exit_code == 1
    assert "must be one of" in _out(result)
    assert AppConfig.load().backup_policy.second_copy.enabled is False


# ---------------------------------------------------------------------------
# copy setup: sftp
# ---------------------------------------------------------------------------


def test_setup_sftp_pins_the_host_key_and_turns_the_copy_on(
    cli_runner: CliRunner, encrypted: AppConfig, sftp_target: SftpTarget
) -> None:
    result = cli_runner.invoke(app, ["copy", "setup"], input=sftp_target.answers())

    assert result.exit_code == 0, _out(result)
    assert "Fingerprint" in _out(result)
    server = sftp_target.server
    assert host_keys.trusted_key(server.host, server.port) is not None
    policy = AppConfig.load().backup_policy.second_copy
    assert policy.enabled is True
    assert policy.kind == "sftp"
    assert policy.username == "pfs-copy"


def test_setup_sftp_a_second_time_reuses_the_pinned_key(
    cli_runner: CliRunner, encrypted: AppConfig, sftp_target: SftpTarget
) -> None:
    assert cli_runner.invoke(app, ["copy", "setup"], input=sftp_target.answers()).exit_code == 0
    # No trust answer this time: the key is already pinned.
    answers = sftp_target.answers().replace("\ny\n", "\n", 1)
    result = cli_runner.invoke(app, ["copy", "setup"], input=answers)

    assert result.exit_code == 0, _out(result)
    assert "already trusted" in _out(result)


def test_setup_sftp_declining_the_host_key_changes_nothing(
    cli_runner: CliRunner, encrypted: AppConfig, sftp_target: SftpTarget
) -> None:
    result = cli_runner.invoke(app, ["copy", "setup"], input=sftp_target.answers(trust="n"))

    assert result.exit_code == 1
    assert "not trusted" in _out(result)
    server = sftp_target.server
    assert host_keys.trusted_key(server.host, server.port) is None
    assert AppConfig.load().backup_policy.second_copy.enabled is False


def test_setup_sftp_stores_the_key_passphrase(
    cli_runner: CliRunner, encrypted: AppConfig, sftp_target: SftpTarget
) -> None:
    generated = secrets.token_urlsafe(12)
    result = cli_runner.invoke(
        app, ["copy", "setup"], input=sftp_target.answers(passphrase=generated)
    )

    assert result.exit_code == 0, _out(result)
    assert CredentialService().get_secret("__second_copy_keypass__") == generated


def test_a_failed_setup_keeps_the_old_secret(
    cli_runner: CliRunner, encrypted: AppConfig, sftp_target: SftpTarget
) -> None:
    """Before rc.3 a failed setup kept the old target but stored the new secret."""
    CredentialService().store_secret("__second_copy_keypass__", "old-value")
    result = cli_runner.invoke(
        app, ["copy", "setup"], input=sftp_target.answers(passphrase="new-value", trust="n")
    )

    assert result.exit_code == 1
    assert CredentialService().get_secret("__second_copy_keypass__") == "old-value"


def test_a_failed_setup_removes_a_secret_that_was_not_there(
    cli_runner: CliRunner, encrypted: AppConfig, sftp_target: SftpTarget
) -> None:
    cli_runner.invoke(
        app, ["copy", "setup"], input=sftp_target.answers(passphrase="new-value", trust="n")
    )
    assert CredentialService().get_secret("__second_copy_keypass__") is None


def test_setup_sftp_without_the_sftp_subsystem_fails_cleanly(
    cli_runner: CliRunner, encrypted: AppConfig, sftp_target: SftpTarget
) -> None:
    with patch.object(
        paramiko.SSHClient, "open_sftp", side_effect=paramiko.SSHException("subsystem refused")
    ):
        result = cli_runner.invoke(app, ["copy", "setup"], input=sftp_target.answers())

    assert result.exit_code == 1
    assert "no SFTP for this account" in _out(result)
    assert AppConfig.load().backup_policy.second_copy.enabled is False


def test_setup_sftp_unreachable_host_is_refused(
    cli_runner: CliRunner, encrypted: AppConfig, tmp_path: Path
) -> None:
    answers = f"sftp\n127.0.0.1\n{_closed_port()}\npfs-copy\n{tmp_path / 'k'}\nremote\n\n"
    result = cli_runner.invoke(app, ["copy", "setup"], input=answers)

    assert result.exit_code == 1
    assert "Cannot reach" in _out(result)
    assert AppConfig.load().backup_policy.second_copy.enabled is False


def test_setup_sftp_with_a_key_the_server_rejects_fails(
    cli_runner: CliRunner, encrypted: AppConfig, sftp_target: SftpTarget
) -> None:
    paramiko.ECDSAKey.generate().write_private_key_file(str(sftp_target.key_file))
    result = cli_runner.invoke(app, ["copy", "setup"], input=sftp_target.answers())

    assert result.exit_code == 1
    assert "Cannot connect" in _out(result)
    assert AppConfig.load().backup_policy.second_copy.enabled is False


# ---------------------------------------------------------------------------
# copy now
# ---------------------------------------------------------------------------


def test_now_refuses_when_the_copy_is_off(cli_runner: CliRunner, encrypted: AppConfig) -> None:
    result = cli_runner.invoke(app, ["copy", "now"])

    assert result.exit_code == 1
    assert "pfs copy setup" in _out(result)


def test_now_copies_encrypted_backups_the_target_lacks(
    cli_runner: CliRunner, encrypted: AppConfig, share: Path
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    _copy_to_folder(share)
    result = cli_runner.invoke(app, ["copy", "now", "-d", "home-fw"])

    assert result.exit_code == 0, _out(result)
    assert "home-fw: 1 copied" in _out(result)
    assert len(list(share.rglob("*.enc"))) == 1


def test_now_for_all_devices_copies_nothing_twice(
    cli_runner: CliRunner, encrypted: AppConfig, share: Path
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    _copy_to_folder(share)
    assert cli_runner.invoke(app, ["copy", "now"]).exit_code == 0
    result = cli_runner.invoke(app, ["copy", "now"])

    assert result.exit_code == 0, _out(result)
    assert "home-fw: 0 copied" in _out(result)


def test_now_warns_about_plain_backups_it_will_not_copy(
    cli_runner: CliRunner, seeded_config: AppConfig, share: Path
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0  # plain: encryption is off
    _encrypt()
    _copy_to_folder(share)
    result = cli_runner.invoke(app, ["copy", "now"])

    assert result.exit_code == 0, _out(result)
    assert "plain file(s) not copied" in _out(result)
    assert "encrypt-existing" in _out(result)
    assert list(share.rglob("*")) == []


def test_now_fails_when_the_target_is_gone(
    cli_runner: CliRunner, encrypted: AppConfig, share: Path
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    _copy_to_folder(share)
    share.rmdir()
    result = cli_runner.invoke(app, ["copy", "now"])

    assert result.exit_code == 1
    assert "does not exist" in _out(result)


# ---------------------------------------------------------------------------
# copy status / disable
# ---------------------------------------------------------------------------


def test_status_when_off(cli_runner: CliRunner, encrypted: AppConfig) -> None:
    result = cli_runner.invoke(app, ["copy", "status"])

    assert result.exit_code == 0
    assert "OFF" in _out(result)


def test_status_shows_the_folder_and_retention(
    cli_runner: CliRunner, encrypted: AppConfig, share: Path
) -> None:
    _copy_to_folder(share)
    result = cli_runner.invoke(app, ["copy", "status"])

    output = _out(result)
    assert "ON" in output
    assert share.name in output
    assert "90 days" in output


def test_status_shows_the_sftp_location(cli_runner: CliRunner, encrypted: AppConfig) -> None:
    config = AppConfig.load()
    policy = config.backup_policy.second_copy
    policy.enabled, policy.kind = True, "sftp"
    policy.host, policy.port, policy.username, policy.path = "nas.test", 2222, "copy", "pfs"
    config.save()
    result = cli_runner.invoke(app, ["copy", "status"])

    assert "sftp://copy@nas.test:2222/pfs" in _out(result)


def test_disable_turns_the_copy_off_and_keeps_copied_files(
    cli_runner: CliRunner, encrypted: AppConfig, share: Path
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    _copy_to_folder(share)
    assert cli_runner.invoke(app, ["copy", "now"]).exit_code == 0
    result = cli_runner.invoke(app, ["copy", "disable"])

    assert result.exit_code == 0, _out(result)
    assert AppConfig.load().backup_policy.second_copy.enabled is False
    assert len(list(share.rglob("*.enc"))) == 1
