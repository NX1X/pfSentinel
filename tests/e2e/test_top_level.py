"""E2E tests for the top-level pfSentinel CLI surface (version/status/list)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from pfsentinel import __version__
from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig

pytestmark = pytest.mark.e2e


@pytest.fixture
def empty_config(e2e_home):
    """Persist an AppConfig with no devices.

    Deliberately does not depend on ssh_server or seeded_config so we get
    a clean, deviceless config on disk for the 'no devices' status path.
    """
    config = AppConfig()
    config.save()
    return config


def test_version_flag_prints_pfsentinel_version(cli_runner: CliRunner) -> None:
    """`pfs --version` exits 0 and emits the version string via typer.echo."""
    result = cli_runner.invoke(app, ["--version"])

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    assert "pfSentinel v" in result.stdout
    assert __version__ in result.stdout


def test_status_with_no_devices_reports_empty_state(
    cli_runner: CliRunner,
    empty_config: AppConfig,
) -> None:
    """`pfs status` on a fresh config tells the user no devices are configured."""
    result = cli_runner.invoke(app, ["status"])

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    assert "No devices configured" in result.stdout


def test_status_with_seeded_device_lists_home_fw(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """`pfs status` renders the devices table including the seeded device."""
    result = cli_runner.invoke(app, ["status"])

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    assert "home-fw" in result.stdout


def test_list_root_shortcut_with_no_backups(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """`pfs list` (the root shortcut for backup list) reports no backups when empty."""
    result = cli_runner.invoke(app, ["list"])

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    assert "No backups" in result.stdout
