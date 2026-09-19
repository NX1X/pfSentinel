"""E2E tests for strict SSH host key checking and `pfs device trust-key`.

Runs against the real fake SSH server, so the host key exchange, the pinned
known_hosts file and paramiko's RejectPolicy are all exercised for real.
"""

from __future__ import annotations

import shutil
import subprocess

import paramiko
import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services import host_keys

from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

BACKUP = ["backup", "run", "-d", "home-fw", "--config-only", "--no-notify"]


def _output(result) -> str:
    return (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def strict_config(seeded_config: AppConfig) -> AppConfig:
    """The seeded device, switched to strict checking (the 0.2.0 default)."""
    device = seeded_config.get_device("home-fw")
    assert device is not None
    device.strict_host_keys = True
    seeded_config.save()
    return seeded_config


def test_new_devices_are_strict_by_default() -> None:
    assert DeviceConfig(id="fw", label="fw", host="10.0.0.1").strict_host_keys is True


def test_untrusted_key_is_refused_with_a_fix(cli_runner: CliRunner, strict_config) -> None:
    result = cli_runner.invoke(app, BACKUP)
    assert result.exit_code != 0
    assert "pfs device trust-key home-fw" in _output(result)


def test_trust_key_then_backup_succeeds(
    cli_runner: CliRunner, strict_config, ssh_server: FakeSSHServer
) -> None:
    trusted = cli_runner.invoke(app, ["device", "trust-key", "home-fw", "--yes"])
    assert trusted.exit_code == 0, _output(trusted)
    assert "SHA256:" in trusted.stdout
    assert host_keys.known_hosts_path().is_file()

    result = cli_runner.invoke(app, BACKUP)
    assert result.exit_code == 0, _output(result)
    assert "Backup complete" in result.stdout


def test_trust_key_prompt_declined_changes_nothing(cli_runner: CliRunner, strict_config) -> None:
    result = cli_runner.invoke(app, ["device", "trust-key", "home-fw"], input="n\n")
    assert result.exit_code == 1
    assert not host_keys.known_hosts_path().exists()


def test_changed_key_is_refused_as_possible_mitm(
    cli_runner: CliRunner, strict_config, ssh_server: FakeSSHServer
) -> None:
    # Pin a different key for the same host:port, as if the server was swapped.
    host_keys.trust(ssh_server.host, ssh_server.port, paramiko.ECDSAKey.generate())

    result = cli_runner.invoke(app, BACKUP)
    assert result.exit_code != 0
    out = _output(result)
    assert "CHANGED" in out
    assert "trust-key home-fw" in out


def test_retrust_replaces_old_key(
    cli_runner: CliRunner, strict_config, ssh_server: FakeSSHServer
) -> None:
    host_keys.trust(ssh_server.host, ssh_server.port, paramiko.ECDSAKey.generate())
    result = cli_runner.invoke(app, ["device", "trust-key", "home-fw", "--yes"])
    assert result.exit_code == 0, _output(result)
    assert "REPLACES" in result.stdout

    real = host_keys.fetch_host_key(ssh_server.host, ssh_server.port)
    pinned = host_keys.trusted_key(ssh_server.host, ssh_server.port)
    assert pinned is not None
    assert host_keys.fingerprint(pinned) == host_keys.fingerprint(real)
    assert cli_runner.invoke(app, BACKUP).exit_code == 0


def test_trust_key_turns_on_strict_for_legacy_device(
    cli_runner: CliRunner, seeded_config: AppConfig
) -> None:
    """Configs saved before 0.2.0 carry strict_host_keys=false; trusting upgrades them."""
    result = cli_runner.invoke(app, ["device", "trust-key", "home-fw", "--yes"])
    assert result.exit_code == 0, _output(result)
    device = AppConfig.load().get_device("home-fw")
    assert device is not None and device.strict_host_keys is True


def test_unknown_device(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["device", "trust-key", "nope", "--yes"])
    assert result.exit_code == 1


def test_non_default_port_uses_bracket_entry() -> None:
    assert host_keys.host_entry("10.0.0.1", 22) == "10.0.0.1"
    assert host_keys.host_entry("10.0.0.1", 2222) == "[10.0.0.1]:2222"


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_fingerprint_matches_openssh(tmp_path) -> None:
    key = paramiko.ECDSAKey.generate()
    pub = tmp_path / "k.pub"
    pub.write_text(f"{key.get_name()} {key.get_base64()}\n")
    out = subprocess.run(
        ["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True, check=True
    ).stdout
    assert host_keys.fingerprint(key) in out
