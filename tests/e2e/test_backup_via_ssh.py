"""Canonical e2e test: backup run against a real local SSH server.

This is the template that the other e2e tests follow. It exercises the
full path:

    Typer CLI -> BackupOrchestrator -> ConnectionManager -> SSHConnector
        -> paramiko (real TCP socket) -> FakeSSHServer (SFTP)
        -> downloaded XML -> compression -> file on disk -> index update

No part of the SSH transport is mocked. The only thing not real is the
remote peer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.services.retention import RetentionService

pytestmark = pytest.mark.e2e


def test_backup_run_via_real_ssh(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
    tmp_path: Path,
) -> None:
    """`pfs backup run -d home-fw` produces a verified backup on disk.

    Drives the CLI in-process via Typer's CliRunner. The seeded_config
    fixture has already written the AppConfig and stored credentials,
    and ssh_server is running on 127.0.0.1 with /cf/conf/config.xml
    pre-loaded.
    """
    result = cli_runner.invoke(
        app,
        ["backup", "run", "-d", "home-fw", "--config-only", "--no-notify"],
    )

    assert result.exit_code == 0, (
        f"CLI exit={result.exit_code}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}\n"
    )
    assert "Backup complete" in result.stdout

    # The retention index should have exactly one record for home-fw.
    backup_root = seeded_config.backup_policy.backup_root
    assert backup_root is not None
    retention = RetentionService(backup_root, seeded_config.backup_policy)
    index = retention.load_index("home-fw")

    assert len(index.records) == 1, (
        f"Expected 1 record, got {len(index.records)}: {[r.filename for r in index.records]}"
    )

    record = index.records[0]
    assert record.device_id == "home-fw"
    assert record.connection_method == "ssh"
    assert record.verified is True
    assert record.sha256 != ""
    assert record.size_bytes > 0
    assert record.compressed is True

    # The actual file should be on disk where the record claims.
    file_path = backup_root / "home-fw" / record.relative_path
    assert file_path.exists(), f"Backup file missing: {file_path}"
    assert file_path.stat().st_size == record.size_bytes


def test_backup_run_against_invalid_credentials_fails(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """Auth failure surfaces a non-zero exit and a clear error message."""
    from pfsentinel.services.credentials import CredentialService

    # Overwrite the stored password with garbage.
    CredentialService().store("home-fw", "wrong-password")

    result = cli_runner.invoke(
        app,
        ["backup", "run", "-d", "home-fw", "--config-only", "--no-notify"],
    )

    assert result.exit_code != 0
    # Either "authentication" or "auth" should appear somewhere in the
    # output; pfSentinel surfaces SSH auth errors via BackupError.
    combined = (result.stdout + result.stderr).lower()
    assert "auth" in combined, f"Expected auth error, got:\n{combined}"
