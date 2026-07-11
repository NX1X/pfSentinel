"""E2E tests for `pfs device` commands against a real fake SSH server.

Mirrors the structure of tests/e2e/test_backup_via_ssh.py - same fixtures,
same shape, no mocking of the SSH transport.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig

pytestmark = pytest.mark.e2e


@pytest.fixture
def empty_config(e2e_home):
    """Persist an AppConfig with no devices.

    Deliberately does not depend on ssh_server or seeded_config so we get
    a clean, deviceless config on disk.
    """
    config = AppConfig()
    config.save()
    return config


def test_device_list_with_no_devices_reports_empty(
    cli_runner: CliRunner,
    empty_config: AppConfig,
) -> None:
    """`pfs device list` on an empty config prints a helpful 'no devices' notice."""
    result = cli_runner.invoke(app, ["device", "list"])

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    assert "No devices configured" in result.stdout


def test_device_list_with_seeded_device_shows_home_fw(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """`pfs device list` renders a table that includes the configured device."""
    result = cli_runner.invoke(app, ["device", "list"])

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    assert "home-fw" in result.stdout


def test_device_remove_existing_device_succeeds(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """`pfs device remove home-fw -y` removes the device and survives a list call."""
    result = cli_runner.invoke(app, ["device", "remove", "home-fw", "-y"])

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    assert "home-fw" in result.stdout
    assert "removed" in result.stdout.lower()

    # After removal the config file should report no devices.
    reloaded = AppConfig.load()
    assert reloaded.get_device("home-fw") is None

    follow_up = cli_runner.invoke(app, ["device", "list"])
    assert follow_up.exit_code == 0
    assert "No devices configured" in follow_up.stdout


def test_device_remove_nonexistent_device_fails(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """Removing an unknown device returns a non-zero exit code."""
    result = cli_runner.invoke(app, ["device", "remove", "does-not-exist", "-y"])

    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "not found" in combined or "does-not-exist" in combined


def test_device_test_reports_ssh_ok_against_fake_server(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """`pfs device test -d home-fw` should report SSH reachable.

    The fake server only speaks SSH on a single random port, so HTTPS and
    HTTP will be unreachable - that's expected. We only assert the SSH
    line is OK and the CLI exits cleanly.
    """
    result = cli_runner.invoke(app, ["device", "test", "-d", "home-fw"])

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    # The SSH line is rendered as "SSH  : ✓ OK" (with rich markup stripped).
    assert "SSH" in result.stdout
    assert "OK" in result.stdout


def test_device_add_via_https_with_crafted_input(
    cli_runner: CliRunner,
    empty_config: AppConfig,
) -> None:
    """`pfs device add` over HTTPS (skips the SSH-key prompt) persists a device.

    Flags fill in id, label, host, method, username and a non-default port
    (so the port-confirmation prompt is skipped). The remaining prompts are
    fed via input=: password (twice for confirmation), then 'n' to decline
    the post-add 'Test connection now?' check.
    """
    result = cli_runner.invoke(
        app,
        [
            "device",
            "add",
            "--id",
            "test-fw",
            "--label",
            "Test FW",
            "--host",
            "192.0.2.10",
            "--method",
            "https",
            "--username",
            "admin",
            "--https-port",
            "8443",
        ],
        input="hunter2\nhunter2\nn\n",
    )

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    assert "test-fw" in result.stdout
    assert "added" in result.stdout.lower()

    reloaded = AppConfig.load()
    device = reloaded.get_device("test-fw")
    assert device is not None
    assert device.label == "Test FW"
    assert device.host == "192.0.2.10"
    assert device.primary_method.value == "https"
    assert device.https_port == 8443
    assert device.username == "admin"


def test_device_edit_changes_label(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """`pfs device edit home-fw` updates the label and persists.

    Feed a new label for the first prompt, then accept all remaining defaults
    by sending bare newlines. Order of prompts in `device_edit`:
      1) Display name  (we change this)
      2) Host/IP       (accept default)
      3) Username      (accept default)
      4) Connection method (accept default 'ssh')
      5) Change password?  (no)
      6) Change SSH key?   (no - SSH path)
      7) Toggle SSL verification? (no)
    """
    original = AppConfig.load().get_device("home-fw")
    assert original is not None
    assert original.label == "Home pfSense"

    result = cli_runner.invoke(
        app,
        ["device", "edit", "home-fw"],
        input="Edited Label\n\n\n\n\n\n\n",
    )

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}"
    assert "updated" in result.stdout.lower()

    reloaded = AppConfig.load().get_device("home-fw")
    assert reloaded is not None
    assert reloaded.label == "Edited Label"
    # Other fields must be unchanged.
    assert reloaded.host == original.host
    assert reloaded.username == original.username
    assert reloaded.primary_method == original.primary_method
