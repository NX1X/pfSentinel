"""CLI tests for `pfs config` (show, init, validate, set, path)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.credentials import CredentialService


def _out(result) -> str:
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


def _add_device(device_id: str, password: str | None) -> None:
    config = AppConfig.load()
    config.add_device(DeviceConfig(id=device_id, label=device_id, host="192.0.2.1"))
    config.save()
    if password:
        CredentialService().store(device_id, password)


@pytest.fixture
def wide_console():
    """Widen the shared Rich console so the JSON panel is not cut at 80 columns."""
    from pfsentinel.cli.formatters import console

    saved = console._width
    console.width = 400
    yield
    console._width = saved


@pytest.mark.usefixtures("wide_console")
class TestShow:
    def test_prints_the_config_as_json(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        result = cli_runner.invoke(app, ["config", "show"])

        assert result.exit_code == 0, _out(result)
        assert '"backup_policy"' in result.stdout
        assert '"notifications"' in result.stdout

    def test_marks_an_unset_backup_root_as_default(
        self, cli_runner: CliRunner, cli_config_path: Path
    ) -> None:
        AppConfig().save()
        result = cli_runner.invoke(app, ["config", "show"])

        assert result.exit_code == 0, _out(result)
        assert "not yet configured" in _out(result)

    def test_never_prints_a_stored_password(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _add_device("edge-fw", "s3cret-pass-value")
        result = cli_runner.invoke(app, ["config", "show"])

        assert "edge-fw" in result.stdout
        assert "s3cret-pass-value" not in result.stdout


class TestInit:
    def test_creates_the_config_with_the_chosen_backup_dir(
        self, cli_runner: CliRunner, cli_config_path: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "my-backups"
        result = cli_runner.invoke(app, ["config", "init"], input=f"{target}\n")

        assert result.exit_code == 0, _out(result)
        assert cli_config_path.exists()
        assert AppConfig.load().backup_policy.backup_root == target.resolve()

    def test_leaves_an_existing_config_alone(
        self, cli_runner: CliRunner, saved_config: AppConfig, cli_config_path: Path
    ) -> None:
        before = cli_config_path.read_text(encoding="utf-8")
        result = cli_runner.invoke(app, ["config", "init"])

        assert result.exit_code == 0
        assert "--force" in _out(result)
        assert cli_config_path.read_text(encoding="utf-8") == before

    def test_force_overwrites_an_existing_config(
        self, cli_runner: CliRunner, saved_config: AppConfig, tmp_path: Path
    ) -> None:
        _add_device("edge-fw", None)
        target = tmp_path / "fresh"
        result = cli_runner.invoke(app, ["config", "init", "--force"], input=f"{target}\n")

        assert result.exit_code == 0, _out(result)
        config = AppConfig.load()
        assert config.devices == []
        assert config.backup_policy.backup_root == target.resolve()


class TestValidate:
    def test_missing_config_file_fails(self, cli_runner: CliRunner, cli_config_path: Path) -> None:
        result = cli_runner.invoke(app, ["config", "validate"])

        assert result.exit_code == 1
        assert "not found" in _out(result)
        assert "pfs config init" in _out(result)

    def test_corrupt_config_file_is_reported_invalid(
        self, cli_runner: CliRunner, cli_config_path: Path
    ) -> None:
        cli_config_path.write_text("{ this is not json", encoding="utf-8")
        result = cli_runner.invoke(app, ["config", "validate"])

        assert result.exit_code == 1
        assert "invalid" in _out(result).lower()

    def test_a_corrupt_config_is_never_overwritten(
        self, cli_runner: CliRunner, cli_config_path: Path
    ) -> None:
        """Before 1.0, load() returned defaults and the next save erased the devices."""
        broken = '{"devices": [{"id": "fw", "host": "192.0.2.1"}], oops'
        cli_config_path.write_text(broken, encoding="utf-8")
        cli_runner.invoke(app, ["config", "set", "backup-dir", "/tmp/x"])
        assert cli_config_path.read_text(encoding="utf-8") == broken

    def test_valid_config_without_devices(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        result = cli_runner.invoke(app, ["config", "validate"])

        assert result.exit_code == 0, _out(result)
        assert "valid" in _out(result)
        assert "No devices configured" in _out(result)

    def test_flags_a_device_without_a_password(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _add_device("with-pw", "pw")
        _add_device("no-pw", None)
        result = cli_runner.invoke(app, ["config", "validate"])

        output = _out(result)
        assert result.exit_code == 0, output
        assert "no-pw" in output
        assert "missing password" in output
        assert "set passwords" in output

    def test_all_devices_with_passwords_need_no_hint(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _add_device("with-pw", "pw")
        result = cli_runner.invoke(app, ["config", "validate"])

        assert "missing password" not in _out(result)


class TestSet:
    def test_backup_dir_is_saved(
        self, cli_runner: CliRunner, saved_config: AppConfig, tmp_path: Path
    ) -> None:
        new_dir = tmp_path / "elsewhere"
        new_dir.mkdir()
        result = cli_runner.invoke(app, ["config", "set", "backup-dir", str(new_dir)])

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().backup_policy.backup_root == new_dir.resolve()
        assert "will be created" not in _out(result)

    def test_backup_dir_that_does_not_exist_yet_is_accepted(
        self, cli_runner: CliRunner, saved_config: AppConfig, tmp_path: Path
    ) -> None:
        new_dir = tmp_path / "not-yet"
        result = cli_runner.invoke(app, ["config", "set", "backup-dir", str(new_dir)])

        assert result.exit_code == 0, _out(result)
        assert "will be created" in _out(result)
        assert not new_dir.exists()

    def test_unknown_key_is_refused(
        self, cli_runner: CliRunner, saved_config: AppConfig, cli_config_path: Path
    ) -> None:
        before = json.loads(cli_config_path.read_text(encoding="utf-8"))
        result = cli_runner.invoke(app, ["config", "set", "log-level", "DEBUG"])

        assert result.exit_code == 1
        assert "Unknown setting" in _out(result)
        assert json.loads(cli_config_path.read_text(encoding="utf-8")) == before


def test_path_prints_the_config_file_location(cli_runner: CliRunner, cli_config_path: Path) -> None:
    result = cli_runner.invoke(app, ["config", "path"])

    assert result.exit_code == 0
    assert "".join(result.stdout.split()) == str(cli_config_path)
