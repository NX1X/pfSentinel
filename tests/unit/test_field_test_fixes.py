"""Fixes from the homelab field test of 2026-11-20 (v0.2.6, Windows 11).

The issues are listed in the field-test notes: device add took the device ID
as the host, a non-root backup user showed root-only files as errors, an
SSH-key-only device showed HTTPS and HTTP as failed, the passphrase prompt
appeared for an unencrypted key, and progress and log lines ran together.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pfsentinel.cli.commands import device as device_cmd
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import ConnectionMethod, DeviceConfig
from pfsentinel.services import connection
from pfsentinel.services.extra_backup import ExtraBackupService, NeedsRootError


def _write_key(path: Path, passphrase: bytes | None) -> Path:
    encryption = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    path.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, encryption
        )
    )
    return path


class TestKeyPassphraseDetection:
    def test_unencrypted_key_needs_no_passphrase(self, tmp_path: Path) -> None:
        key = _write_key(tmp_path / "plain", None)
        assert device_cmd._key_needs_passphrase(key) is False

    def test_encrypted_key_needs_a_passphrase(self, tmp_path: Path) -> None:
        key = _write_key(tmp_path / "locked", b"correct horse")
        assert device_cmd._key_needs_passphrase(key) is True

    def test_unreadable_key_is_unknown(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad"
        bad.write_text("not a key")
        assert device_cmd._key_needs_passphrase(bad) is None


class TestHostPrompt:
    def test_ip_address_resolves(self) -> None:
        assert device_cmd._resolves("127.0.0.1") is True

    def test_unresolvable_host_is_asked_again(self) -> None:
        """The operator typed the device ID as the host: ask again before saving."""
        answers = iter(["192.0.2.10"])
        with (
            patch.object(device_cmd, "_resolves", side_effect=lambda h: h == "192.0.2.10"),
            patch.object(device_cmd.typer, "confirm", return_value=False),
            patch.object(device_cmd.typer, "prompt", side_effect=lambda *_a, **_k: next(answers)),
        ):
            assert device_cmd._prompt_host("firewall-homelab-local") == "192.0.2.10"

    def test_unresolvable_host_can_be_kept_on_purpose(self) -> None:
        with (
            patch.object(device_cmd, "_resolves", return_value=False),
            patch.object(device_cmd.typer, "confirm", return_value=True),
        ):
            assert device_cmd._prompt_host("vpn-only.lan") == "vpn-only.lan"


class TestSshKeyOnlyDevice:
    def test_https_and_http_are_skipped_not_failed(self) -> None:
        device = DeviceConfig(
            id="fw", label="fw", host="192.0.2.1", ssh_key_path=Path("/keys/backup")
        )
        creds = MagicMock()
        creds.get.return_value = None  # no password on purpose
        cm = connection.ConnectionManager(device, creds)
        ssh = MagicMock(spec=connection.SSHConnector)
        with patch.object(cm, "_make_connector", return_value=ssh) as make:
            status = cm.test_all()

        assert [c.args[0] for c in make.call_args_list] == [ConnectionMethod.SSH]
        assert status.ssh_reachable
        assert status.skipped_methods == ["https", "http"]
        assert status.https_error is None and status.http_error is None


class TestNonRootUser:
    def _service(self, tmp_path: Path) -> ExtraBackupService:
        config = AppConfig()
        config.backup_policy.backup_root = tmp_path
        return ExtraBackupService(config, MagicMock())

    def test_root_only_logs_raise_needs_root(self, tmp_path: Path) -> None:
        svc = self._service(tmp_path)
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.download_files.return_value = []
        conn.permission_denied = ["/var/log/filter.log", "/var/log/system.log"]
        device = DeviceConfig(id="fw", label="fw", host="192.0.2.1", username="backup-user")
        with (
            patch.object(svc, "_get_ssh_connector", return_value=conn),
            pytest.raises(NeedsRootError, match="logs need root.*backup-user"),
        ):
            svc.backup_logs(device, timestamp=MagicMock(), sequence=1)

    def test_tar_permission_denied_is_one_info_line(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger="pfsentinel")
        connection._log_partial_read(1, "tar: Can't open 'sasldb2.db': Permission denied\n")
        records = [r for r in caplog.records if r.name.startswith("pfsentinel")]
        assert [r.levelno for r in records] == [logging.INFO]
        assert "sasldb2.db" in records[0].getMessage()
        assert "root only" in records[0].getMessage()

    def test_bsdtar_wording_also_gives_the_name(self) -> None:
        line = "tar: usr/local/etc/sasldb2.db: Couldn't open file: Permission denied"
        assert connection._denied_name(line) == "usr/local/etc/sasldb2.db"

    def test_other_tar_errors_stay_warnings(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger="pfsentinel")
        connection._log_partial_read(1, "tar: file changed as we read it\n")
        assert [r.levelno for r in caplog.records if r.name.startswith("pfsentinel")] == [
            logging.WARNING
        ]


class TestProgressLine:
    def test_a_log_line_never_starts_inside_the_progress_bar(self, capsys) -> None:
        import io

        from pfsentinel.cli import formatters
        from pfsentinel.utils import logging as pfs_logging

        # A handler of its own: the global one must not keep capsys's stream.
        handler = pfs_logging._ConsoleHandler(io.StringIO())
        formatters.print_progress("Downloading RRD data...", 10)
        assert not capsys.readouterr().out.endswith("\n")

        handler.handle(logging.makeLogRecord({"msg": "something happened", "levelno": 30}))
        assert capsys.readouterr().out == "\n", "the open bar line must end first"
        assert "something happened" in handler.stream.getvalue()

    def test_messages_after_an_unfinished_bar_start_on_a_new_line(self, capsys) -> None:
        from pfsentinel.cli import formatters

        formatters.print_progress("Package configs downloaded", 80)
        formatters.print_success("done")
        out = capsys.readouterr().out
        assert "downloaded\n" in out


@pytest.mark.parametrize(
    ("key_name", "file_type"),
    [("ssh-ed25519", "ed25519"), ("ecdsa-sha2-nistp256", "ecdsa"), ("ssh-rsa", "rsa")],
)
def test_trust_key_hint_names_the_matching_host_key_file(key_name: str, file_type: str) -> None:
    assert device_cmd._host_key_file_type(key_name) == file_type
