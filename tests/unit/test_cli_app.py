"""CLI tests for the top-level `pfs` commands: setup, docs, status, entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel import DOCS_URL
from pfsentinel.cli import app as app_module
from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import ConnectionMethod, DeviceConfig
from pfsentinel.services.credentials import CredentialService


def _out(result) -> str:
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


@pytest.fixture
def no_persistent_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OS keyring and no writable encrypted file store on this machine."""

    class _UnavailableStore:
        is_available = False

    monkeypatch.setattr("pfsentinel.services.credentials._KEYRING_AVAILABLE", False)
    monkeypatch.setattr("pfsentinel.services.credentials.EncryptedFileStore", _UnavailableStore)


# ---------------------------------------------------------------------------
# pfs setup
# ---------------------------------------------------------------------------


class TestSetup:
    def test_existing_devices_only_show_next_steps(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        saved_config.add_device(DeviceConfig(id="edge-fw", label="Edge", host="192.0.2.1"))
        saved_config.save()
        result = cli_runner.invoke(app, ["setup"])

        assert result.exit_code == 0, _out(result)
        assert "already exists with 1 device" in _out(result)
        assert len(AppConfig.load().devices) == 1

    def test_ssh_with_password_saves_device_and_password(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        # host, id (default), label (default), user (default), method,
        # key path (blank), password, ssh port
        answers = "192.0.2.10\n\n\n\nssh\n\nhunter2\n2222\n"
        result = cli_runner.invoke(app, ["setup"], input=answers)

        assert result.exit_code == 0, _out(result)
        device = AppConfig.load().get_device("192-0-2-10")
        assert device is not None
        assert device.primary_method == ConnectionMethod.SSH
        assert device.ssh_port == 2222
        assert device.username == "admin"
        assert CredentialService().get("192-0-2-10") == "hunter2"
        assert "pfs backup run -d 192-0-2-10" in _out(result)

    def test_https_asks_for_the_https_port(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        answers = "fw.example.test\nedge\nEdge FW\nbackup\nhttps\nhunter2\n8443\n"
        result = cli_runner.invoke(app, ["setup"], input=answers)

        assert result.exit_code == 0, _out(result)
        device = AppConfig.load().get_device("edge")
        assert device is not None
        assert device.primary_method == ConnectionMethod.HTTPS
        assert device.https_port == 8443
        assert device.label == "Edge FW"
        assert device.username == "backup"

    def test_ssh_key_stores_the_passphrase_and_skips_the_password(
        self, cli_runner: CliRunner, saved_config: AppConfig, tmp_path: Path
    ) -> None:
        key = tmp_path / "id_ed25519"
        key.write_text("not a real key", encoding="utf-8")
        # host, id, label, user, method, key path, password (skip), port, passphrase
        answers = f"192.0.2.11\nkeyed\n\n\nssh\n{key}\n\n\nkeypass\n"
        result = cli_runner.invoke(app, ["setup"], input=answers)

        assert result.exit_code == 0, _out(result)
        device = AppConfig.load().get_device("keyed")
        assert device is not None
        assert device.ssh_key_path == key
        creds = CredentialService()
        assert creds.get("keyed") is None
        assert creds.get_ssh_key_passphrase("keyed") == "keypass"

    def test_invalid_method_saves_nothing(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        result = cli_runner.invoke(app, ["setup"], input="192.0.2.10\n\n\n\ntelnet\n")

        assert result.exit_code == 1
        assert "Invalid method" in _out(result)
        assert AppConfig.load().devices == []

    def test_invalid_device_id_saves_nothing(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        answers = "192.0.2.10\nNot A Slug!\n\n\nhttp\nhunter2\n"
        result = cli_runner.invoke(app, ["setup"], input=answers)

        assert result.exit_code == 1
        assert "Error" in _out(result)
        assert AppConfig.load().devices == []

    def test_warns_when_the_password_cannot_be_kept(
        self, cli_runner: CliRunner, saved_config: AppConfig, no_persistent_store: None
    ) -> None:
        answers = "192.0.2.10\n\n\n\nhttp\nhunter2\n"
        result = cli_runner.invoke(app, ["setup"], input=answers)

        assert result.exit_code == 0, _out(result)
        assert "No persistent keyring" in _out(result)


# ---------------------------------------------------------------------------
# pfs docs
# ---------------------------------------------------------------------------


class TestDocs:
    def test_no_browser_only_prints_the_links(
        self, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", opened.append)
        result = cli_runner.invoke(app, ["docs", "--no-browser"])

        assert result.exit_code == 0
        assert DOCS_URL in result.stdout
        assert "llms-full.txt" in result.stdout
        assert opened == []

    def test_opens_the_manual_in_the_browser(
        self, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", opened.append)
        result = cli_runner.invoke(app, ["docs"])

        assert result.exit_code == 0
        assert opened == [DOCS_URL]

    def test_a_headless_machine_still_gets_the_link(
        self, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_browser(url: str) -> None:
            raise RuntimeError("no display")

        monkeypatch.setattr("webbrowser.open", no_browser)
        result = cli_runner.invoke(app, ["docs"])

        assert result.exit_code == 0
        assert DOCS_URL in result.stdout


# ---------------------------------------------------------------------------
# pfs status
# ---------------------------------------------------------------------------


class TestStatus:
    def _with_device(self, config: AppConfig) -> None:
        config.add_device(DeviceConfig(id="edge-fw", label="Edge", host="192.0.2.1"))
        config.save()

    def test_counts_backups_on_disk(self, cli_runner: CliRunner, saved_config: AppConfig) -> None:
        self._with_device(saved_config)
        root = saved_config.backup_policy.backup_root
        assert root is not None
        (root / "edge-fw").mkdir()
        (root / "edge-fw" / "a.xml.gz").write_bytes(b"x")
        (root / "edge-fw" / "b.xml.gz").write_bytes(b"x")
        result = cli_runner.invoke(app, ["status"])

        assert result.exit_code == 0, _out(result)
        assert "Total backups on disk: 2" in _out(result)

    def test_reports_a_missing_backup_dir(
        self, cli_runner: CliRunner, saved_config: AppConfig, tmp_path: Path
    ) -> None:
        saved_config.backup_policy.backup_root = tmp_path / "gone"
        self._with_device(saved_config)
        result = cli_runner.invoke(app, ["status"])

        assert result.exit_code == 0, _out(result)
        assert "Backup directory not found" in _out(result)

    def test_an_unexpected_error_is_shown_not_raised(
        self, cli_runner: CliRunner, saved_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> AppConfig:
            raise OSError("disk unplugged")

        monkeypatch.setattr(AppConfig, "load", staticmethod(broken))
        result = cli_runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "disk unplugged" in _out(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_pfsentinel_logger():
    """main_entry() configures the 'pfsentinel' logger; put it back afterwards."""
    logger = logging.getLogger("pfsentinel")
    saved = (logger.level, logger.propagate, list(logger.handlers))
    yield logger
    logger.setLevel(saved[0])
    logger.propagate = saved[1]
    logger.handlers[:] = saved[2]


class TestMainEntry:
    def test_uses_the_log_level_from_the_config(
        self,
        saved_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        restore_pfsentinel_logger: logging.Logger,
    ) -> None:
        saved_config.log_level = "DEBUG"
        saved_config.save()
        monkeypatch.setattr(sys, "argv", ["pfs", "--version"])

        with pytest.raises(SystemExit) as exc:
            app_module.main_entry()

        assert exc.value.code == 0
        assert restore_pfsentinel_logger.level == logging.DEBUG

    def test_falls_back_to_info_when_the_config_cannot_load(
        self,
        cli_config_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_pfsentinel_logger: logging.Logger,
    ) -> None:
        def broken() -> AppConfig:
            raise OSError("unreadable")

        monkeypatch.setattr(AppConfig, "load", staticmethod(broken))
        monkeypatch.setattr(sys, "argv", ["pfs", "--version"])

        with pytest.raises(SystemExit):
            app_module.main_entry()

        assert restore_pfsentinel_logger.level == logging.INFO
