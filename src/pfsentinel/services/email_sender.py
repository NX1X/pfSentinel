"""Email alerts over SMTP, with the standard library only.

Works with any mail server: a provider (STARTTLS on 587 or implicit TLS on
465, with a login) or a local relay such as Mailpit (plain, no login).
TLS certificates are verified unless the user turned that off for a lab
server with a self-signed certificate.

The SMTP password never appears in an error: every message built here is
scrubbed of the password and of its base64 forms (what AUTH PLAIN and AUTH
LOGIN send), in case a server echoes the command back.
"""

from __future__ import annotations

import base64
import contextlib
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from pfsentinel.models.config import NotificationConfig
from pfsentinel.utils.logging import get_logger, redact_secrets

logger = get_logger(__name__)

# Keystore name of the SMTP password (CredentialService.store_secret / get_secret).
SMTP_KEYSTORE_NAME = "__smtp_password__"

# Deliberately simple: one "@", no spaces, quotes, commas or angle brackets.
# The address goes into a header, so anything that could start a second
# address or header is refused.
_ADDRESS = re.compile(r"[^@\s<>,;:\"'()\[\]\\]+@[^@\s<>,;:\"'()\[\]\\]+\.[^@\s<>,;:\"'()\[\]\\]+")


class EmailError(Exception):
    """A failed email send. The message never contains the SMTP password."""


def validate_address(address: str) -> str:
    """Return the stripped address, or raise ValueError if it is not a plain address."""
    value = address.strip()
    if not _ADDRESS.fullmatch(value):
        raise ValueError(f"Not a valid email address: {value!r}")
    return value


def parse_addresses(text: str) -> list[str]:
    """Split a comma or space separated list of addresses. Raises ValueError."""
    parts = [p for p in re.split(r"[,\s]+", text) if p]
    if not parts:
        raise ValueError("At least one address is required")
    return [validate_address(p) for p in parts]


def tls_context(ca_cert: str | None) -> ssl.SSLContext:
    """A verifying TLS context; ``ca_cert`` adds a private CA (lab) as a trust anchor.

    Certificate and host name checks are never turned off: a lab server with
    its own CA is trusted by naming that CA's certificate file instead.
    """
    if ca_cert and not Path(ca_cert).expanduser().is_file():
        raise EmailError(f"CA certificate file not found: {ca_cert}")
    return ssl.create_default_context(cafile=str(Path(ca_cert).expanduser()) if ca_cert else None)


def build_message(config: NotificationConfig, subject: str, body: str) -> EmailMessage:
    sender = validate_address(config.email_from or "")
    recipients = [validate_address(a) for a in config.email_to]
    msg = EmailMessage()
    # A header value may not hold a line break; a title never needs one.
    msg["Subject"] = " ".join(subject.split())
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    msg.set_content(body)
    return msg


def _secret_forms(username: str | None, password: str | None) -> tuple[str, ...]:
    if not password:
        return ()
    user = username or ""

    def b64(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    # AUTH PLAIN sends b64("\0user\0password"), AUTH LOGIN sends b64(password).
    forms = (password, b64(password), b64(f"\0{user}\0{password}"))
    return tuple(sorted(set(forms), key=len, reverse=True))


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return redact_secrets(text)


def _reply_text(reply: bytes | str) -> str:
    if isinstance(reply, bytes):
        return reply.decode("utf-8", errors="replace")
    return str(reply)


def send_email(config: NotificationConfig, password: str | None, subject: str, body: str) -> None:
    """Send one plain-text message to every address in ``config.email_to``.

    Raises EmailError with a message that is safe to show and log.
    """
    host = config.email_host
    if not host or not config.email_from or not config.email_to:
        raise EmailError(
            "Email not configured: missing server, From or To. Run: pfs notify email setup"
        )
    if config.email_username and config.email_security == "none":
        raise EmailError(
            "Refusing to send the SMTP password over an unencrypted connection. "
            "Use security 'starttls' or 'tls', or remove the login."
        )
    if config.email_username and not password:
        raise EmailError("SMTP password missing from the keystore. Run: pfs notify email setup")

    secrets = _secret_forms(config.email_username, password)
    where = f"{host}:{config.email_port}"
    try:
        message = build_message(config, subject, body)
    except ValueError as e:
        raise EmailError(str(e)) from None
    try:
        _deliver(config, password, message)
    except EmailError:
        raise
    except smtplib.SMTPAuthenticationError as e:
        reply = _scrub(_reply_text(e.smtp_error), secrets)
        raise EmailError(f"Login refused by {where} ({e.smtp_code}): {reply}") from None
    except smtplib.SMTPRecipientsRefused as e:
        refused = ", ".join(
            f"{addr} ({code} {_reply_text(reply)})" for addr, (code, reply) in e.recipients.items()
        )
        raise EmailError(_scrub(f"All recipients refused: {refused}", secrets)) from None
    except smtplib.SMTPResponseException as e:
        reply = _scrub(_reply_text(e.smtp_error), secrets)
        raise EmailError(f"SMTP error from {where} ({e.smtp_code}): {reply}") from None
    except smtplib.SMTPServerDisconnected as e:
        # smtplib reports a read timeout as a closed connection.
        if "timed out" in str(e):
            raise EmailError(
                f"Timed out after {config.email_timeout} s talking to {where}"
            ) from None
        reason = _scrub(str(e), secrets)
        raise EmailError(f"{where} closed the connection: {reason}") from None
    except smtplib.SMTPException as e:
        raise EmailError(_scrub(f"SMTP error from {where}: {e}", secrets)) from None
    except ssl.SSLCertVerificationError as e:
        raise EmailError(
            f"TLS certificate check failed for {where}: {e.verify_message}. "
            "For a lab server with its own CA, give that CA's certificate file: "
            "pfs notify email setup --ca-cert FILE"
        ) from None
    except ssl.SSLError as e:
        raise EmailError(
            f"TLS error with {where}: {e.reason or e}. Check that the security mode "
            "matches the port (starttls: usually 587, tls: usually 465)."
        ) from None
    except TimeoutError:
        raise EmailError(f"Timed out after {config.email_timeout} s talking to {where}") from None
    except OSError as e:
        raise EmailError(f"Cannot connect to {where}: {e.strerror or e}") from None
    logger.debug(f"Email sent to {len(config.email_to)} recipient(s) through {where}")


def _deliver(config: NotificationConfig, password: str | None, message: EmailMessage) -> None:
    host = config.email_host or ""
    context = tls_context(config.email_ca_cert)
    server: smtplib.SMTP
    if config.email_security == "tls":
        server = smtplib.SMTP_SSL(
            host, config.email_port, timeout=config.email_timeout, context=context
        )
    else:
        server = smtplib.SMTP(host, config.email_port, timeout=config.email_timeout)
    try:
        server.ehlo()
        if config.email_security == "starttls":
            if not server.has_extn("starttls"):
                raise EmailError(
                    f"{host}:{config.email_port} does not offer STARTTLS, so nothing was "
                    "sent in plain text. Check the port, or use security 'tls' for port 465."
                )
            server.starttls(context=context)
            server.ehlo()
        if config.email_username:
            server.login(config.email_username, password or "")
        refused = server.send_message(message)
        if refused:
            raise EmailError(f"Not delivered to: {', '.join(sorted(refused))}")
    finally:
        with contextlib.suppress(smtplib.SMTPException, OSError):
            server.quit()
        server.close()
