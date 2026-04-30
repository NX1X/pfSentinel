"""Notification service - Telegram and Windows toast."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests
from loguru import logger

from pfsentinel.models.backup import BackupRecord
from pfsentinel.models.config import NotificationConfig
from pfsentinel.services.credentials import CredentialService
from pfsentinel.utils.platform import is_windows


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

        results = self._dispatch(title, msg, success=True)
        for channel, err in results.items():
            if err:
                logger.warning(f"{channel} backup-success notification failed: {err}")

    def notify_failure(self, device_id: str, error: str) -> None:
        if not self._config.notify_on_failure:
            return
        title = "pfSentinel - Backup Failed"
        msg = f"Device: {device_id}\nError: {error}"
        results = self._dispatch(title, msg, success=False)
        for channel, err in results.items():
            if err:
                logger.warning(f"{channel} backup-failure notification failed: {err}")

    def notify_info(self, title: str, message: str) -> dict[str, str | None]:
        """Dispatch an informational notification. Returns per-channel results.

        Returns a dict mapping channel name -> None (success) or error string (failure).
        """
        return self._dispatch(title, message, success=True)

    def _dispatch(self, title: str, message: str, success: bool) -> dict[str, str | None]:
        """Send to all enabled channels. Returns {channel: error_or_None}."""
        results: dict[str, str | None] = {}

        if self._config.telegram_enabled:
            try:
                self._send_telegram(title, message)
                results["Telegram"] = None
            except Exception as e:
                logger.warning(f"Telegram notification failed: {e}")
                results["Telegram"] = str(e)

        if self._config.slack_enabled:
            try:
                self._send_slack(title, message, success)
                results["Slack"] = None
            except Exception as e:
                logger.warning(f"Slack notification failed: {e}")
                results["Slack"] = str(e)

        if self._config.windows_toast_enabled and is_windows():
            try:
                self._send_windows_toast(title, message, success)
                results["Windows Toast"] = None
            except Exception as e:
                logger.debug(f"Windows toast failed: {e}")
                results["Windows Toast"] = str(e)

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
        if not parsed.hostname or not parsed.hostname.endswith("hooks.slack.com"):
            logger.warning(f"Slack webhook hostname is not hooks.slack.com: {parsed.hostname}")

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
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.debug("Slack notification sent")

    def _send_windows_toast(self, title: str, message: str, success: bool) -> None:
        try:
            from winotify import Notification, audio

            toast = Notification(
                app_id="pfSentinel",
                title=title,
                msg=message,
                duration="short",
            )
            toast.set_audio(audio.Default, loop=False)
            toast.show()
            logger.debug("Windows toast notification sent")
        except ImportError:
            # winotify not installed - silently skip
            pass
        except Exception as e:
            logger.debug(f"Windows toast failed: {e}")
