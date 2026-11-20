"""`pfs notify email|digest|desktop` and the digest end to end, with a real SMTP inbox."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.email_sender import SMTP_KEYSTORE_NAME

from .fake_smtp_server import FakeSMTPServer

pytestmark = pytest.mark.e2e

PASSWORD = "Mail-Pa55word-xyz"


def _output(result) -> str:
    return (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def mailpit() -> Iterator[FakeSMTPServer]:
    """Like Mailpit: plain SMTP, no login."""
    server = FakeSMTPServer.start()
    yield server
    server.stop()


@pytest.fixture
def provider() -> Iterator[FakeSMTPServer]:
    """Like a mail provider: STARTTLS and a login."""
    server = FakeSMTPServer.start(starttls=True, login=("me@example.com", PASSWORD))
    yield server
    server.stop()


def _subjects(server: FakeSMTPServer) -> list[str]:
    return [str(m.message["Subject"]) for m in server.messages]


def test_email_setup_for_a_local_server(
    cli_runner: CliRunner, e2e_home: Path, mailpit: FakeSMTPServer
) -> None:
    result = cli_runner.invoke(
        app,
        [
            "notify",
            "email",
            "setup",
            "--host",
            mailpit.host,
            "--port",
            str(mailpit.port),
            "--security",
            "none",
            "--from",
            "fw@example.com",
            "--to",
            "a@example.com, b@example.com",
        ],
    )
    assert result.exit_code == 0, _output(result)
    assert "Test email sent to a@example.com, b@example.com" in result.stdout
    assert _subjects(mailpit) == ["pfSentinel - Test email"]

    n = AppConfig.load().notifications
    assert n.email_enabled and n.email_security == "none" and n.email_username is None
    assert n.email_to == ["a@example.com", "b@example.com"]


def test_email_setup_with_login_keeps_the_password_out_of_config(
    cli_runner: CliRunner, e2e_home: Path, provider: FakeSMTPServer
) -> None:
    result = cli_runner.invoke(
        app,
        [
            "notify",
            "email",
            "setup",
            "--host",
            provider.host,
            "--port",
            str(provider.port),
            "--security",
            "starttls",
            "--username",
            "me@example.com",
            "--to",
            "ops@example.com",
            "--skip-tls-verify",
        ],
        input=f"{PASSWORD}\n\n",  # password, then accept the default From (the username)
    )
    assert result.exit_code == 0, _output(result)
    assert PASSWORD not in _output(result)
    assert provider.logins == [("me@example.com", PASSWORD)]
    assert _subjects(provider) == ["pfSentinel - Test email"]

    config_text = AppConfig.config_path().read_text()
    assert PASSWORD not in config_text
    assert CredentialService().get_secret(SMTP_KEYSTORE_NAME) == PASSWORD
    n = AppConfig.load().notifications
    assert n.email_from == "me@example.com" and n.email_skip_tls_verify

    status = cli_runner.invoke(app, ["notify", "email", "status"])
    assert "stored" in status.stdout and PASSWORD not in status.stdout
    assert "Email is fully configured" in status.stdout


def test_email_setup_refuses_a_bad_address(cli_runner: CliRunner, e2e_home: Path) -> None:
    result = cli_runner.invoke(
        app,
        [
            "notify",
            "email",
            "setup",
            "--host",
            "mail.example.com",
            "--security",
            "tls",
            "--no-login",
            "--from",
            "x@example.com",
            "--to",
            "not-an-address",
            "--port",
            "465",
        ],
    )
    assert result.exit_code == 1
    assert "Nothing was saved" in _output(result)
    assert not AppConfig.load().notifications.email_enabled


def test_email_setup_refuses_a_login_without_encryption(
    cli_runner: CliRunner, e2e_home: Path
) -> None:
    result = cli_runner.invoke(
        app,
        [
            "notify",
            "email",
            "setup",
            "--host",
            "mail.example.com",
            "--security",
            "none",
            "--username",
            "me",
            "--port",
            "25",
        ],
    )
    assert result.exit_code == 1
    assert "encrypted connection" in _output(result)


def test_email_enable_needs_a_complete_setup(cli_runner: CliRunner, e2e_home: Path) -> None:
    result = cli_runner.invoke(app, ["notify", "email", "enable"])
    assert result.exit_code == 1
    assert "pfs notify email setup" in " ".join(_output(result).split())


def test_test_and_status_include_email(
    cli_runner: CliRunner, e2e_home: Path, mailpit: FakeSMTPServer
) -> None:
    config = AppConfig()
    n = config.notifications
    n.email_enabled, n.email_host, n.email_port = True, mailpit.host, mailpit.port
    n.email_security, n.email_from, n.email_to = "none", "fw@example.com", ["a@example.com"]
    config.save()

    result = cli_runner.invoke(app, ["notify", "test"])
    assert result.exit_code == 0, _output(result)
    assert "Email: sent" in result.stdout
    assert _subjects(mailpit) == ["pfSentinel - Test"]

    status = cli_runner.invoke(app, ["notify", "status"])
    assert "Email:" in status.stdout and "Daily digest: No" in status.stdout

    cli_runner.invoke(app, ["notify", "email", "disable"])
    assert not AppConfig.load().notifications.email_enabled
    assert cli_runner.invoke(app, ["notify", "email", "enable"]).exit_code == 0


def test_digest_enable_disable_status(cli_runner: CliRunner, e2e_home: Path) -> None:
    bad = cli_runner.invoke(app, ["notify", "digest", "enable", "--time", "25:00"])
    assert bad.exit_code == 2

    result = cli_runner.invoke(app, ["notify", "digest", "enable", "--time", "7:30"])
    assert result.exit_code == 0, _output(result)
    assert "Daily digest enabled at 07:30" in result.stdout
    assert "pfs schedule enable" in result.stdout, "the check task is what sends it"
    n = AppConfig.load().notifications
    assert n.digest_enabled and n.digest_time == "07:30"

    status = cli_runner.invoke(app, ["notify", "digest", "status"])
    assert "07:30" in status.stdout and "never" in status.stdout

    assert cli_runner.invoke(app, ["notify", "digest", "disable"]).exit_code == 0
    assert not AppConfig.load().notifications.digest_enabled


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux desktop flag")
def test_desktop_enable_disable(cli_runner: CliRunner, e2e_home: Path) -> None:
    assert cli_runner.invoke(app, ["notify", "desktop", "disable"]).exit_code == 0
    assert not AppConfig.load().notifications.linux_desktop_enabled
    assert cli_runner.invoke(app, ["notify", "desktop", "enable"]).exit_code == 0
    assert AppConfig.load().notifications.linux_desktop_enabled
    status = cli_runner.invoke(app, ["notify", "desktop", "status"])
    assert "notify-send:" in status.stdout


def test_digest_replaces_backup_ok_and_is_sent_once_a_day(
    cli_runner: CliRunner, seeded_config: AppConfig, mailpit: FakeSMTPServer
) -> None:
    n = seeded_config.notifications
    n.notify_on_success = n.notify_on_failure = True
    n.email_enabled, n.email_host, n.email_port = True, mailpit.host, mailpit.port
    n.email_security, n.email_from, n.email_to = "none", "fw@example.com", ["a@example.com"]
    n.digest_enabled, n.digest_time = True, "00:00"
    seeded_config.save()

    run = cli_runner.invoke(app, ["backup", "run", "-d", "home-fw", "--config-only"])
    assert run.exit_code == 0, _output(run)
    # The sample config has no users: that problem is sent at once, the
    # successful backup itself is not.
    assert _subjects(mailpit) == ["pfSentinel - Restore check failed on home-fw"]

    check = cli_runner.invoke(app, ["backup", "check-recent", "--non-interactive"])
    assert check.exit_code == 0, _output(check)
    assert "Daily digest sent" in check.stdout
    assert _subjects(mailpit)[1:] == ["pfSentinel - Daily summary: 1 problem(s)"]
    body = mailpit.messages[1].message.get_payload()
    assert "home-fw: 1 problem(s)" in body
    assert "Backups stored: 1 (1 in the last 24 hours)" in body
    assert "Restore check failed:" in body
    assert "pfSentinel event 1007 (DAILY_DIGEST)" in body

    again = cli_runner.invoke(app, ["backup", "check-recent", "--non-interactive"])
    assert again.exit_code == 0
    assert len(mailpit.messages) == 2, "one digest per calendar day"

    preview = cli_runner.invoke(app, ["notify", "digest", "send", "--preview"])
    assert "home-fw: 1 problem(s)" in preview.stdout
    assert len(mailpit.messages) == 2, "a preview sends nothing"
