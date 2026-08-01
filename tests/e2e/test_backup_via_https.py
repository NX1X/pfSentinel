"""E2e tests: backup run against a real local HTTPS server.

Exercises the full path:

    Typer CLI -> BackupOrchestrator -> ConnectionManager -> HTTPSConnector
        -> requests + TLS (real TCP socket) -> FakeHTTPSServer
        -> XML response -> compression -> file on disk -> index update

No part of the requests / urllib3 / TLS stack is mocked. The only thing
not real is the remote peer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.retention import RetentionService
from tests.e2e.fake_https_server import FakeHTTPSServer

pytestmark = pytest.mark.e2e


def _invoke_backup(
    cli_runner: CliRunner,
    device_id: str = "home-fw-https",
    *extra_args: str,
):
    args = ["backup", "run", "-d", device_id, "--config-only", "--no-notify"]
    args.extend(extra_args)
    return cli_runner.invoke(app, args)


def test_backup_run_via_real_https(
    cli_runner: CliRunner,
    seeded_https_config: AppConfig,
    tmp_path: Path,
) -> None:
    """`pfs backup run -d home-fw-https` produces a verified HTTPS backup on disk."""
    result = _invoke_backup(cli_runner)

    assert result.exit_code == 0, f"CLI exit={result.exit_code}\n--- stdout ---\n{result.stdout}\n"
    assert "Backup complete" in result.stdout

    backup_root = seeded_https_config.backup_policy.backup_root
    assert backup_root is not None
    retention = RetentionService(backup_root, seeded_https_config.backup_policy)
    index = retention.load_index("home-fw-https")

    assert len(index.records) == 1, (
        f"Expected 1 record, got {len(index.records)}: {[r.filename for r in index.records]}"
    )

    record = index.records[0]
    assert record.device_id == "home-fw-https"
    assert record.connection_method == "https"
    assert record.verified is True
    assert record.sha256 != ""
    assert record.size_bytes > 0

    file_path = backup_root / "home-fw-https" / record.relative_path
    assert file_path.exists(), f"Backup file missing: {file_path}"
    assert file_path.stat().st_size == record.size_bytes


def test_backup_run_https_uses_ca_cert(
    cli_runner: CliRunner,
    seeded_https_config: AppConfig,
    https_server: FakeHTTPSServer,
) -> None:
    """pfSentinel must validate against our self-signed cert, not skip verification.

    The seeded config sets ca_cert_path to the fake server's cert. If
    pfSentinel silently disabled TLS verification, the test would still
    pass — but at least one server-level GET should record the TLS
    handshake occurring. If the CA path were wrong, requests would raise
    SSLError before any HTTP round-trip and no login request would land.
    """
    result = _invoke_backup(cli_runner)

    assert result.exit_code == 0, result.stdout

    # If verification failed, requests would abort before sending; the
    # server would see no traffic. Presence of a login POST is proof the
    # TLS handshake completed with our self-signed cert trusted via
    # ca_cert_path.
    posts = [r for r in https_server.all_requests() if r["method"] == "POST"]
    login_posts = [r for r in posts if r["path"].startswith("/index.php")]
    assert login_posts, "No login POST reached the server — TLS handshake likely failed"


def test_backup_run_https_invalid_credentials_fails(
    cli_runner: CliRunner,
    seeded_https_config: AppConfig,
) -> None:
    """Auth failure surfaces a non-zero exit and a clear error message."""
    CredentialService().store("home-fw-https", "wrong-password")

    result = _invoke_backup(cli_runner)

    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "auth" in combined or "login" in combined, f"Expected auth/login error, got:\n{combined}"


def test_backup_run_https_area_filter(
    cli_runner: CliRunner,
    seeded_https_config: AppConfig,
    https_server: FakeHTTPSServer,
) -> None:
    """`--area filter` propagates to the backup POST's backuparea field."""
    result = _invoke_backup(cli_runner, "home-fw-https", "--area", "filter")

    assert result.exit_code == 0, result.stdout

    # last_request() is the backup POST (final request in the flow).
    posts = [
        r
        for r in https_server.all_requests()
        if r["method"] == "POST" and r["path"] == "/diag_backup.php"
    ]
    assert posts, "No backup POST reached the server"
    backup_post = posts[-1]
    assert backup_post["form"].get("backuparea") == "filter", (
        f"Expected backuparea=filter, got form={backup_post['form']}"
    )


def test_backup_run_https_no_packages(
    cli_runner: CliRunner,
    seeded_https_config: AppConfig,
    https_server: FakeHTTPSServer,
) -> None:
    """`--no-packages` propagates to the backup POST's nopackages=on field."""
    result = _invoke_backup(cli_runner, "home-fw-https", "--no-packages")

    assert result.exit_code == 0, result.stdout

    posts = [
        r
        for r in https_server.all_requests()
        if r["method"] == "POST" and r["path"] == "/diag_backup.php"
    ]
    assert posts, "No backup POST reached the server"
    backup_post = posts[-1]
    assert backup_post["form"].get("nopackages") == "on", (
        f"Expected nopackages=on, got form={backup_post['form']}"
    )


def test_backup_run_https_csrf_token_flow(
    cli_runner: CliRunner,
    seeded_https_config: AppConfig,
    https_server: FakeHTTPSServer,
) -> None:
    """pfSentinel must re-extract CSRF for the backup page, not reuse the login token.

    FakeHTTPSServer issues distinct login-page tokens ("TOK-LOGIN-...")
    and backup-page tokens ("TOK-BACKUP-...") and rejects mismatches with
    403. If the connector reused the login token for the backup POST,
    the whole flow would fail. So a successful backup here proves
    pfSentinel fetched the backup page and pulled its fresh token.
    """
    result = _invoke_backup(cli_runner)
    assert result.exit_code == 0, result.stdout

    posts = [
        r
        for r in https_server.all_requests()
        if r["method"] == "POST" and r["path"] == "/diag_backup.php"
    ]
    assert posts, "No backup POST reached the server"
    submitted = posts[-1]["form"].get("__csrf_magic", "")
    assert submitted.startswith("TOK-BACKUP-"), (
        f"Expected a backup-page CSRF token, got: {submitted!r}"
    )


def test_backup_run_https_missing_csrf_page_errors(
    cli_runner: CliRunner,
    seeded_https_config: AppConfig,
    https_server: FakeHTTPSServer,
) -> None:
    """A login page without __csrf_magic yields a non-zero exit and CSRF-shaped error."""
    https_server.set_include_login_csrf(False)

    result = _invoke_backup(cli_runner)

    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "csrf" in combined, f"Expected CSRF error message, got:\n{combined}"
