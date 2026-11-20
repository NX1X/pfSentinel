"""Email alerts against a real SMTP server on 127.0.0.1 (tests/e2e/fake_smtp_server.py)."""

from __future__ import annotations

import base64
import logging
import socket
from collections.abc import Iterator

import pytest

from pfsentinel.models.config import NotificationConfig
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.email_sender import (
    SMTP_KEYSTORE_NAME,
    EmailError,
    parse_addresses,
    send_email,
    validate_address,
)
from pfsentinel.services.notifications import AlertKind, NotificationService

from .fake_smtp_server import FakeSMTPServer

pytestmark = pytest.mark.e2e

USER = "alerts@example.com"
PASSWORD = "S3cret-Pa55word!"


@pytest.fixture
def smtp() -> Iterator[list[FakeSMTPServer]]:
    """Start servers with smtp_start(...); all are stopped after the test."""
    servers: list[FakeSMTPServer] = []
    yield servers
    for server in servers:
        server.stop()


def _start(servers: list[FakeSMTPServer], **options) -> FakeSMTPServer:
    server = FakeSMTPServer.start(**options)
    servers.append(server)
    return server


def _config(server: FakeSMTPServer, **overrides) -> NotificationConfig:
    values = {
        "email_enabled": True,
        "email_host": server.host,
        "email_port": server.port,
        "email_security": "none",
        "email_from": "pfsentinel@example.com",
        "email_to": ["admin@example.com"],
        "email_timeout": 5,
        "windows_toast_enabled": False,
        "linux_desktop_enabled": False,
    }
    values.update(overrides)
    return NotificationConfig(**values)


def _secret_forms() -> list[str]:
    def b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    return [PASSWORD, b64(PASSWORD), b64(f"\0{USER}\0{PASSWORD}")]


def _assert_no_secret(text: str) -> None:
    for form in _secret_forms():
        assert form not in text


class TestPlainLocalServer:
    """Mailpit style: no encryption, no login."""

    def test_sends_to_every_recipient(self, smtp) -> None:
        server = _start(smtp)
        config = _config(server, email_to=["a@example.com", "b@example.com"])
        send_email(config, None, "pfSentinel - Backup Failed", "Device: fw\nError: boom")

        assert len(server.messages) == 1
        mail = server.messages[0]
        assert mail.mail_from == "pfsentinel@example.com"
        assert mail.rcpt_to == ["a@example.com", "b@example.com"]
        msg = mail.message
        assert msg["Subject"] == "pfSentinel - Backup Failed"
        assert msg["From"] == "pfsentinel@example.com"
        assert msg["To"] == "a@example.com, b@example.com"
        assert msg["Message-ID"].endswith("@example.com>")
        assert "Error: boom" in msg.get_payload()
        assert "STARTTLS" not in server.commands()

    def test_a_line_break_in_the_title_cannot_add_a_header(self, smtp) -> None:
        server = _start(smtp)
        send_email(_config(server), None, "Title\r\nBcc: evil@example.com", "body")
        msg = server.messages[0].message
        assert msg["Bcc"] is None
        assert msg["Subject"] == "Title Bcc: evil@example.com"
        assert server.messages[0].rcpt_to == ["admin@example.com"]

    def test_every_alert_kind_reaches_email(self, smtp) -> None:
        server = _start(smtp)
        svc = NotificationService(_config(server), CredentialService())
        for kind in AlertKind:
            if kind is AlertKind.BACKUP_OK:
                results = svc.notify_info("pfSentinel - info", "Device: fw")
            elif kind is AlertKind.DAILY_DIGEST:
                results = svc.send_digest("pfSentinel - Daily summary", "all OK", False)
            else:
                results = svc.alert(kind, f"pfSentinel - {kind.name}", "Device: fw\nDetail")
            assert results == {"Email": None}, kind
        events = [m.message.get_payload() for m in server.messages]
        assert len(events) == len(AlertKind)
        for kind, payload in zip(AlertKind, events, strict=True):
            assert f"pfSentinel event {int(kind)} ({kind.name})" in payload

    def test_a_login_over_plain_text_is_refused_before_connecting(self, smtp) -> None:
        server = _start(smtp)
        with pytest.raises(EmailError, match="unencrypted"):
            send_email(_config(server, email_username=USER), PASSWORD, "t", "b")
        assert server.transcript == []


class TestStartTLS:
    def test_login_and_send_after_starttls(self, smtp) -> None:
        server = _start(smtp, starttls=True, login=(USER, PASSWORD))
        config = _config(
            server,
            email_security="starttls",
            email_username=USER,
            email_ca_cert=str(server.cert_path),
        )
        send_email(config, PASSWORD, "Subject", "Body")

        assert server.logins == [(USER, PASSWORD)]
        assert len(server.messages) == 1
        commands = server.commands()
        assert commands.index("STARTTLS") < commands.index("AUTH") < commands.index("MAIL")

    def test_certificate_is_verified_by_default(self, smtp) -> None:
        server = _start(smtp, starttls=True, login=(USER, PASSWORD))
        config = _config(server, email_security="starttls", email_username=USER)
        with pytest.raises(EmailError, match="certificate check failed") as e:
            send_email(config, PASSWORD, "Subject", "Body")
        assert "--ca-cert" in str(e.value)
        assert server.logins == [], "no login before the certificate is trusted"
        assert server.messages == []

    def test_a_trusted_certificate_passes_verification(self, smtp, monkeypatch) -> None:
        server = _start(smtp, starttls=True)
        # The fake server's certificate is self-signed with SAN 127.0.0.1;
        # trust it the way a system CA store would.
        monkeypatch.setenv("SSL_CERT_FILE", str(server.cert_path))
        send_email(_config(server, email_security="starttls"), None, "Subject", "Body")
        assert len(server.messages) == 1

    def test_server_without_starttls_is_refused(self, smtp) -> None:
        server = _start(smtp, login=(USER, PASSWORD), auth_in_plain_text=True)
        config = _config(server, email_security="starttls", email_username=USER)
        with pytest.raises(EmailError, match="does not offer STARTTLS") as e:
            send_email(config, PASSWORD, "Subject", "Body")
        _assert_no_secret(str(e.value))
        commands = server.commands()
        assert "AUTH" not in commands and "MAIL" not in commands
        assert not any(PASSWORD.encode() in line for line in server.transcript)

    def test_wrong_password_error_never_contains_the_password(self, smtp, caplog) -> None:
        # The server quotes the AUTH line and the password back in its reply.
        server = _start(smtp, starttls=True, login=(USER, "other"), echo_auth=True)
        config = _config(
            server,
            email_security="starttls",
            email_username=USER,
            email_ca_cert=str(server.cert_path),
        )
        with pytest.raises(EmailError, match="Login refused") as e:
            send_email(config, PASSWORD, "Subject", "Body")
        assert "<redacted>" in str(e.value)
        _assert_no_secret(str(e.value))
        assert e.value.__cause__ is None and e.value.__suppress_context__

        creds = CredentialService()
        creds.store_secret(SMTP_KEYSTORE_NAME, PASSWORD)
        svc = NotificationService(config, creds)
        with caplog.at_level(logging.DEBUG, logger="pfsentinel"):
            results = svc.alert(AlertKind.BACKUP_FAILED, "t", "Device: fw")
        assert results["Email"] and "Login refused" in results["Email"]
        _assert_no_secret(results["Email"])
        _assert_no_secret(caplog.text)

    def test_password_comes_from_the_keystore(self, smtp) -> None:
        server = _start(smtp, starttls=True, login=(USER, PASSWORD))
        creds = CredentialService()
        creds.store_secret(SMTP_KEYSTORE_NAME, PASSWORD)
        config = _config(
            server,
            email_security="starttls",
            email_username=USER,
            email_ca_cert=str(server.cert_path),
        )
        assert USER in config.model_dump_json()
        assert PASSWORD not in config.model_dump_json()
        results = NotificationService(config, creds).alert(AlertKind.RISKY_CHANGE, "t", "m")
        assert results == {"Email": None}
        assert server.logins == [(USER, PASSWORD)]

    def test_missing_password_is_reported(self, smtp) -> None:
        server = _start(smtp, starttls=True, login=(USER, PASSWORD))
        config = _config(server, email_security="starttls", email_username=USER)
        with pytest.raises(EmailError, match="password missing"):
            send_email(config, None, "Subject", "Body")
        assert server.transcript == []


class TestImplicitTLS:
    def test_send_over_implicit_tls(self, smtp) -> None:
        server = _start(smtp, implicit_tls=True, login=(USER, PASSWORD))
        config = _config(
            server, email_security="tls", email_username=USER, email_ca_cert=str(server.cert_path)
        )
        send_email(config, PASSWORD, "Subject", "Body")
        assert server.logins == [(USER, PASSWORD)]
        assert len(server.messages) == 1

    def test_tls_against_a_plain_server_explains_the_port(self, smtp) -> None:
        server = _start(smtp)
        with pytest.raises(EmailError, match="security mode matches the port"):
            send_email(_config(server, email_security="tls"), None, "Subject", "Body")


class TestFailures:
    def test_one_refused_recipient_is_reported(self, smtp) -> None:
        server = _start(smtp, reject={"gone@example.com"})
        config = _config(server, email_to=["admin@example.com", "gone@example.com"])
        with pytest.raises(EmailError, match="Not delivered to: gone@example.com"):
            send_email(config, None, "Subject", "Body")
        assert server.messages[0].rcpt_to == ["admin@example.com"]

    def test_all_recipients_refused(self, smtp) -> None:
        server = _start(smtp, reject={"admin@example.com"})
        with pytest.raises(EmailError, match="All recipients refused"):
            send_email(_config(server), None, "Subject", "Body")

    def test_connection_refused(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        config = NotificationConfig(
            email_host="127.0.0.1",
            email_port=port,
            email_security="none",
            email_from="a@example.com",
            email_to=["b@example.com"],
        )
        with pytest.raises(EmailError, match=f"Cannot connect to 127.0.0.1:{port}"):
            send_email(config, None, "Subject", "Body")

    def test_silent_server_times_out(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)  # accepts the TCP connection, never greets
            config = NotificationConfig(
                email_host="127.0.0.1",
                email_port=listener.getsockname()[1],
                email_security="none",
                email_from="a@example.com",
                email_to=["b@example.com"],
                email_timeout=1,
            )
            with pytest.raises(EmailError, match="Timed out after 1 s"):
                send_email(config, None, "Subject", "Body")

    def test_not_configured(self) -> None:
        with pytest.raises(EmailError, match="pfs notify email setup"):
            send_email(NotificationConfig(), None, "Subject", "Body")


class TestAddresses:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "no-at-sign",
            "a@b",
            "a b@example.com",
            "a@example.com\nBcc: x@example.com",
            "<a@example.com>",
            "a@example.com,b@example.com",
        ],
    )
    def test_invalid_addresses_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            validate_address(bad)

    def test_parse_a_list(self) -> None:
        assert parse_addresses(" a@example.com, b@example.org  c@x.io ") == [
            "a@example.com",
            "b@example.org",
            "c@x.io",
        ]
