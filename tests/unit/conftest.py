"""Fixtures for the CLI tests under tests/unit.

The root tests/conftest.py already isolates the keyring, the encrypted
secret store and the release check. These fixtures add a hermetic config
location, so CLI commands never read or write the real ~/.pfsentinel.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.models.config import AppConfig


@pytest.fixture
def cli_runner() -> CliRunner:
    """Typer's CliRunner for invoking commands in-process."""
    return CliRunner()


@pytest.fixture
def cli_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect AppConfig.config_path() to a file under tmp_path.

    The file does not exist yet; tests that need a saved config call
    AppConfig().save() or use the ``saved_config`` fixture.
    """
    pfs_dir = tmp_path / "home" / ".pfsentinel"
    pfs_dir.mkdir(parents=True)
    path = pfs_dir / "config.json"
    monkeypatch.setattr(AppConfig, "config_path", staticmethod(lambda: path))
    return path


@pytest.fixture
def saved_config(cli_config_path: Path, tmp_path: Path) -> AppConfig:
    """A saved config with no devices and a backup root under tmp_path."""
    root = tmp_path / "backups"
    root.mkdir()
    config = AppConfig()
    config.backup_policy.backup_root = root
    config.save()
    return config
