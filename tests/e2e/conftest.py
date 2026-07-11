"""Shared fixtures for e2e tests.

The e2e suite drives the real Typer CLI via CliRunner against a paramiko
fake SSH server running on 127.0.0.1. AppConfig is redirected to a tmp
directory so the user's real ~/.pfsentinel is never touched. The root
tests/conftest.py already replaces the system keyring with an in-memory
dict for every test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.models.config import AppConfig, BackupPolicy, NotificationConfig
from pfsentinel.models.device import ConnectionMethod, DeviceConfig
from pfsentinel.services.credentials import CredentialService
from tests.e2e.fake_ssh_server import FakeSSHServer


@pytest.fixture
def cli_runner() -> CliRunner:
    """Typer's CliRunner for invoking commands in-process."""
    return CliRunner()


@pytest.fixture
def e2e_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect AppConfig.config_path() into a tmp directory.

    CLI commands call AppConfig.load() / save() which target
    ~/.pfsentinel/config.json by default. We override that so each test
    gets a clean config that never touches the real home directory.
    """
    home = tmp_path / "home"
    pfs_dir = home / ".pfsentinel"
    pfs_dir.mkdir(parents=True)
    config_path = pfs_dir / "config.json"
    monkeypatch.setattr(AppConfig, "config_path", staticmethod(lambda: config_path))
    return home


@pytest.fixture
def ssh_server(sample_xml: str) -> Iterator[FakeSSHServer]:
    """Spin up a paramiko SSH+SFTP server on 127.0.0.1:<random port>.

    Pre-seeds the pfSense config path with SAMPLE_XML from the root
    conftest. Tests can overwrite via server.set_file(...) before
    invoking the CLI.
    """
    server = FakeSSHServer.start(
        username="admin",
        password="hunter2",
        files={"/cf/conf/config.xml": sample_xml.encode("utf-8")},
    )
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def seeded_config(
    e2e_home: Path,
    ssh_server: FakeSSHServer,
    tmp_path: Path,
) -> AppConfig:
    """Persist a config with one device ('home-fw') pointed at the fake server.

    The credential for 'home-fw' is stored in the in-memory keyring (set
    up by the root conftest) so CLI commands can authenticate.
    """
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    config = AppConfig(
        backup_policy=BackupPolicy(
            backup_root=backup_root,
            max_backups_per_device=10,
            compress=True,
        ),
        notifications=NotificationConfig(
            notify_on_success=False,
            notify_on_failure=False,
        ),
    )
    device = DeviceConfig(
        id="home-fw",
        label="Home pfSense",
        host=ssh_server.host,
        primary_method=ConnectionMethod.SSH,
        fallback_method=None,
        ssh_port=ssh_server.port,
        username=ssh_server.username,
        strict_host_keys=False,
        timeout=10,
    )
    config.add_device(device)
    config.save()

    creds = CredentialService()
    creds.store("home-fw", ssh_server.password)

    return config
