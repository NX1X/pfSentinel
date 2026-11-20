"""Notification service - Telegram and Windows toast."""

from __future__ import annotations

import logging
import os
import subprocess
from enum import IntEnum
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import requests

from pfsentinel.models.backup import BackupRecord
from pfsentinel.models.config import NotificationConfig
from pfsentinel.services.credentials import CredentialService
from pfsentinel.utils.logging import get_logger, redact_secrets
from pfsentinel.utils.platform import is_windows

logger = get_logger(__name__)

# Toasts need a registered AppUserModelID. Windows PowerShell's own ID is
# present on every Windows install, so the toast shows as "Windows PowerShell".
_TOAST_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

# Static script: title and message arrive via environment variables and are
# XML-escaped in Python, so no notification text is ever parsed as PowerShell.
_TOAST_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "[void][Windows.UI.Notifications.ToastNotificationManager,"
    "Windows.UI.Notifications,ContentType=WindowsRuntime];"
    "[void][Windows.Data.Xml.Dom.XmlDocument,"
    "Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime];"
    "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
    "$x.LoadXml($env:PFS_TOAST_XML);"
    "$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
    "[Windows.UI.Notifications.ToastNotificationManager]::"
    "CreateToastNotifier($env:PFS_TOAST_APP_ID).Show($t)"
)


def build_toast_xml(title: str, message: str) -> str:
    return (
        '<toast duration="short"><visual><binding template="ToastGeneric">'
        f"<text>{xml_escape(title)}</text><text>{xml_escape(message)}</text>"
        '</binding></visual><audio src="ms-winsoundevent:Notification.Default"/></toast>'
    )


def show_windows_toast(title: str, message: str) -> None:
    """Show a toast through the WinRT API via built-in Windows PowerShell.

    Raises on failure so the caller can report it. Needs no third-party package.
    """
    _run_powershell(
        _TOAST_SCRIPT,
        {"PFS_TOAST_XML": build_toast_xml(title, message), "PFS_TOAST_APP_ID": _TOAST_APP_ID},
    )


class AlertKind(IntEnum):
    """Kinds of alert, valued by their fixed Windows Event Log event ID."""

    BACKUP_OK = 1000
    BACKUP_FAILED = 1001
    RISKY_CHANGE = 1002
    NO_RECENT_BACKUP = 1003
    RESTORE_CHECK_FAILED = 1004
    SECOND_COPY_FAILED = 1005
    UPDATE_AVAILABLE = 1006

    @property
    def entry_type(self) -> str:
        if self in (AlertKind.BACKUP_OK, AlertKind.UPDATE_AVAILABLE):
            return "Information"
        if self is AlertKind.RISKY_CHANGE:
            return "Warning"
        return "Error"


EVENT_SOURCE = "pfSentinel"

# Static scripts: the event text arrives through environment variables, so no
# message is ever parsed as PowerShell.
_EVENT_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "Write-EventLog -LogName Application -Source $env:PFS_EVENT_SOURCE "
    "-EventId ([int]$env:PFS_EVENT_ID) -EntryType $env:PFS_EVENT_TYPE "
    "-Message $env:PFS_EVENT_MSG"
)
_REGISTER_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "if (-not [System.Diagnostics.EventLog]::SourceExists($env:PFS_EVENT_SOURCE)) "
    "{ New-EventLog -LogName Application -Source $env:PFS_EVENT_SOURCE }"
)


def _powershell() -> str:
    # Absolute path, so a powershell.exe earlier on PATH cannot be picked up.
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def _run_powershell(script: str, env_extra: dict[str, str]) -> None:
    env = dict(os.environ)
    env.update(env_extra)
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "powershell failed").strip()[:300])


def register_event_source() -> None:
    """Register the "pfSentinel" Application log source (Windows, needs admin once)."""
    _run_powershell(_REGISTER_SCRIPT, {"PFS_EVENT_SOURCE": EVENT_SOURCE})


def write_system_log(kind: AlertKind, title: str, message: str) -> None:
    """Write one alert to the Windows Event Log, or to syslog/journald on Linux."""
    text = f"{title}\n{message}"
    if is_windows():
        try:
            _run_powershell(
                _EVENT_SCRIPT,
                {
                    "PFS_EVENT_SOURCE": EVENT_SOURCE,
                    "PFS_EVENT_ID": str(int(kind)),
                    "PFS_EVENT_TYPE": kind.entry_type,
                    "PFS_EVENT_MSG": text,
                },
            )
        except RuntimeError as e:
            if "source" in str(e).lower():
                raise RuntimeError(
                    "Event source 'pfSentinel' is not registered. In an administrator "
                    "PowerShell, run: pfs notify eventlog enable"
                ) from e
            raise
        return
    import syslog

    priority = {
        "Information": syslog.LOG_INFO,
        "Warning": syslog.LOG_WARNING,
        "Error": syslog.LOG_ERR,
    }[kind.entry_type]
    syslog.openlog("pfsentinel", 0, syslog.LOG_USER)
    for line in text.splitlines():
        syslog.syslog(priority, f"[{int(kind)}] {line}")
    syslog.closelog()


def describe_send_error(error: Exception) -> str:
    """A user-facing description of a failed send that never contains a secret.

    requests puts the full URL into its messages, and for Telegram (token in
    the path) and Slack (the URL is the credential) that URL is the secret.
    """
    if isinstance(error, requests.HTTPError) and error.response is not None:
        resp = error.response
        return f"HTTP {resp.status_code} {resp.reason or ''}".strip()
    if isinstance(error, requests.RequestException):
        return f"{type(error).__name__}: {redact_secrets(str(error))}"
    return redact_secrets(str(error))


class NotificationService:
    """Dispatches notifications to configured channels."""

    def __init__(self, config: NotificationConfig, credential_service: CredentialService) -> None:
        self._config = config
        self._creds = credential_service

    def notify_success(self, record: BackupRecord | list[BackupRecord]) -> None:
        if not self._config.notify_on_success:
            return

        records = record if isinstance(record, list) else [record]
        if len(records) == 1:
            r = records[0]
            title = "pfSentinel - Backup Complete"
            msg = (
                f"Device: {r.device_id}\n"
                f"Type: {r.type_label}\n"
                f"File: {r.filename}\n"
                f"Size: {r.size_bytes / 1024:.1f} KB\n"
                f"Changes: {r.changes_label}"
            )
        else:
            title = f"pfSentinel - {len(records)} Backup(s) Complete"
            device = records[0].device_id
            total_size = sum(r.size_bytes for r in records)
            types = ", ".join(r.type_label for r in records)
            msg = (
                f"Device: {device}\n"
                f"Files: {len(records)}\n"
                f"Types: {types}\n"
                f"Total size: {total_size / 1024:.1f} KB"
            )

        results = self._dispatch(title, msg, success=True, kind=AlertKind.BACKUP_OK)
        for channel, err in results.items():
            if err:
                logger.warning(f"{channel} backup-success notification failed: {err}")

    def notify_failure(self, device_id: str, error: str) -> None:
        self.alert(
            AlertKind.BACKUP_FAILED,
            "pfSentinel - Backup Failed",
            f"Device: {device_id}\nError: {error}",
        )

    def alert(self, kind: AlertKind, title: str, message: str) -> dict[str, str | None]:
        """Send a problem alert (anything but BACKUP_OK) to every enabled channel."""
        if not self._config.notify_on_failure:
            return {}
        results = self._dispatch(title, message, success=False, kind=kind)
        for channel, err in results.items():
            if err:
                logger.warning(f"{channel} alert ({kind.name}) failed: {err}")
        return results

    def notify_info(self, title: str, message: str) -> dict[str, str | None]:
        """Dispatch an informational notification. Returns per-channel results.

        Returns a dict mapping channel name -> None (success) or error string (failure).
        """
        return self._dispatch(title, message, success=True)

    def _dispatch(
        self, title: str, message: str, success: bool, kind: AlertKind | None = None
    ) -> dict[str, str | None]:
        """Send to all enabled channels. Returns {channel: error_or_None}."""
        results: dict[str, str | None] = {}

        if self._config.windows_event_log_enabled:
            event_kind = kind or (AlertKind.BACKUP_OK if success else AlertKind.BACKUP_FAILED)
            channel = "Windows Event Log" if is_windows() else "System log"
            try:
                write_system_log(event_kind, title, message)
                results[channel] = None
            except Exception as e:
                reason = redact_secrets(str(e))
                logger.warning(f"{channel} write failed: {reason}")
                results[channel] = reason

        if self._config.telegram_enabled:
            try:
                self._send_telegram(title, message)
                results["Telegram"] = None
            except Exception as e:
                reason = describe_send_error(e)
                logger.warning(f"Telegram notification failed: {reason}")
                results["Telegram"] = reason

        if self._config.slack_enabled:
            try:
                self._send_slack(title, message, success)
                results["Slack"] = None
            except Exception as e:
                reason = describe_send_error(e)
                logger.warning(f"Slack notification failed: {reason}")
                results["Slack"] = reason

        if self._config.windows_toast_enabled and is_windows():
            try:
                self._send_windows_toast(title, message, success)
                results["Windows Toast"] = None
            except Exception as e:
                reason = describe_send_error(e)
                logger.debug(f"Windows toast failed: {reason}")
                results["Windows Toast"] = reason

        return results

    def _send_telegram(self, title: str, message: str) -> None:
        token = self._creds.get_telegram_token()
        chat_id = self._config.telegram_chat_id

        if not token or not chat_id:
            raise RuntimeError("Telegram not configured: missing token or chat_id")

        # Suppress urllib3/requests debug logging to prevent token-in-URL leaking
        # into log output.  The Telegram Bot API requires the token in the URL path.
        urllib3_logger = logging.getLogger("urllib3")
        prev_level = urllib3_logger.level
        urllib3_logger.setLevel(logging.WARNING)
        try:
            text = f"*{title}*\n{message}"
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            resp = requests.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
                allow_redirects=False,
            )
            resp.raise_for_status()
        finally:
            urllib3_logger.setLevel(prev_level)
        logger.debug("Telegram notification sent")

    def _send_slack(self, title: str, message: str, success: bool) -> None:
        webhook_url = self._creds.get_slack_webhook()
        if not webhook_url:
            raise RuntimeError("Slack not configured: missing webhook URL")

        # Validate webhook URL to prevent SSRF / data exfiltration
        parsed = urlparse(webhook_url)
        if parsed.scheme != "https":
            raise RuntimeError("Slack webhook must use HTTPS")
        if not parsed.hostname or (
            parsed.hostname != "hooks.slack.com"
            and not parsed.hostname.endswith(".hooks.slack.com")
        ):
            # Refuse rather than warn: the message carries backup details, and
            # a webhook stored for some other host would send them there.
            raise RuntimeError(
                f"Slack webhook host must be hooks.slack.com, not {parsed.hostname!r}. "
                "Run: pfs notify slack setup"
            )

        icon = ":white_check_mark:" if success else ":x:"
        payload = {
            "attachments": [
                {
                    "color": "good" if success else "danger",
                    "title": f"{icon} {title}",
                    "text": message,
                    "footer": "pfSentinel",
                }
            ]
        }
        resp = requests.post(webhook_url, json=payload, timeout=10, allow_redirects=False)
        resp.raise_for_status()
        logger.debug("Slack notification sent")

    def _send_windows_toast(self, title: str, message: str, success: bool) -> None:
        show_windows_toast(title, message)
        logger.debug("Windows toast notification sent")
