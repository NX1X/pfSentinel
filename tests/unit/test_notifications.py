"""Tests for notification service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from pfsentinel.models.backup import BackupRecord, BackupType, ChangeCategory
from pfsentinel.models.config import NotificationConfig
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.notifications import NotificationService


def _make_record(**overrides):
    defaults = {
        "device_id": "fw1",
        "filename": "fw1_backup.xml.gz",
        "relative_path": "2025/03/05/fw1_backup.xml.gz",
        "size_bytes": 5120,
        "sha256": "abc123",
        "changes": [ChangeCategory.MINOR],
        "backup_type": BackupType.CONFIG,
    }
    defaults.update(overrides)
    return BackupRecord(**defaults)


def _make_service(
    telegram_enabled=False,
    slack_enabled=False,
    notify_on_success=True,
    notify_on_failure=True,
    telegram_chat_id=None,
):
    config = NotificationConfig(
        telegram_enabled=telegram_enabled,
        slack_enabled=slack_enabled,
        notify_on_success=notify_on_success,
        notify_on_failure=notify_on_failure,
        telegram_chat_id=telegram_chat_id,
    )
    creds = CredentialService()
    return NotificationService(config, creds)


class TestNotifySuccess:
    def test_disabled_does_nothing(self):
        svc = _make_service(notify_on_success=False, telegram_enabled=True)
        # Should not raise or dispatch
        svc.notify_success(_make_record())

    @patch("pfsentinel.services.notifications.NotificationService._send_telegram")
    def test_single_record(self, mock_send):
        svc = _make_service(telegram_enabled=True, telegram_chat_id="123")
        svc._creds.store_telegram_token("tok")
        svc.notify_success(_make_record())
        mock_send.assert_called_once()
        title_arg = mock_send.call_args[0][0]
        assert "Backup Complete" in title_arg

    @patch("pfsentinel.services.notifications.NotificationService._send_telegram")
    def test_multiple_records_summary(self, mock_send):
        svc = _make_service(telegram_enabled=True, telegram_chat_id="123")
        svc._creds.store_telegram_token("tok")
        records = [_make_record(), _make_record(), _make_record()]
        svc.notify_success(records)
        mock_send.assert_called_once()
        title_arg = mock_send.call_args[0][0]
        assert "3 Backup(s)" in title_arg


class TestNotifyFailure:
    def test_disabled_does_nothing(self):
        svc = _make_service(notify_on_failure=False, telegram_enabled=True)
        svc.notify_failure("fw1", "connection error")

    @patch("pfsentinel.services.notifications.NotificationService._send_telegram")
    def test_dispatches(self, mock_send):
        svc = _make_service(telegram_enabled=True, telegram_chat_id="123", notify_on_failure=True)
        svc._creds.store_telegram_token("tok")
        svc.notify_failure("fw1", "SSH timeout")
        mock_send.assert_called_once()
        msg_arg = mock_send.call_args[0][1]
        assert "fw1" in msg_arg
        assert "SSH timeout" in msg_arg


class TestNotifyInfo:
    @patch("pfsentinel.services.notifications.NotificationService._send_telegram")
    def test_returns_results(self, mock_send):
        svc = _make_service(telegram_enabled=True, telegram_chat_id="123")
        svc._creds.store_telegram_token("tok")
        results = svc.notify_info("Test", "Hello")
        assert "Telegram" in results
        assert results["Telegram"] is None


class TestSendTelegram:
    def test_missing_token_raises(self):
        svc = _make_service(telegram_enabled=True, telegram_chat_id="123")
        with pytest.raises(RuntimeError, match="missing token"):
            svc._send_telegram("Title", "Msg")

    def test_missing_chat_id_raises(self):
        svc = _make_service(telegram_enabled=True, telegram_chat_id=None)
        svc._creds.store_telegram_token("tok")
        with pytest.raises(RuntimeError, match="missing token or chat_id"):
            svc._send_telegram("Title", "Msg")

    @patch("pfsentinel.services.notifications.requests")
    def test_success_calls_post(self, mock_requests):
        mock_resp = MagicMock()
        mock_requests.post.return_value = mock_resp
        svc = _make_service(telegram_enabled=True, telegram_chat_id="123")
        svc._creds.store_telegram_token("tok")
        svc._send_telegram("Title", "Message")
        mock_requests.post.assert_called_once()
        call_kwargs = mock_requests.post.call_args
        assert call_kwargs[0][0].startswith("https://api.telegram.org/")


class TestSendSlack:
    def test_missing_webhook_raises(self):
        svc = _make_service(slack_enabled=True)
        with pytest.raises(RuntimeError, match="missing webhook"):
            svc._send_slack("Title", "Msg", True)

    def test_non_https_raises(self):
        svc = _make_service(slack_enabled=True)
        svc._creds.store_slack_webhook("http://hooks.slack.com/xxx")
        with pytest.raises(RuntimeError, match="HTTPS"):
            svc._send_slack("Title", "Msg", True)

    @patch("pfsentinel.services.notifications.requests")
    def test_success_posts(self, mock_requests):
        mock_resp = MagicMock()
        mock_requests.post.return_value = mock_resp
        svc = _make_service(slack_enabled=True)
        svc._creds.store_slack_webhook("https://hooks.slack.com/services/xxx")
        svc._send_slack("Title", "Msg", True)
        mock_requests.post.assert_called_once()


class TestWindowsToast:
    @patch("pfsentinel.services.notifications.is_windows", return_value=False)
    def test_non_windows_skipped(self, mock_win):
        svc = _make_service()
        config = NotificationConfig(windows_toast_enabled=True)
        svc._config = config
        # dispatch should not try _send_windows_toast on non-windows
        results = svc._dispatch("Title", "Msg", True)
        assert "Windows Toast" not in results


class TestWindowsToastBuiltin:
    """Toasts go through built-in Windows PowerShell, not a third-party package."""

    def test_toast_xml_escapes_text(self):
        from pfsentinel.services.notifications import build_toast_xml

        xml = build_toast_xml("a<b", 'x & "y" </text><script>')
        assert "<text>a&lt;b</text>" in xml
        assert "</text><script>" not in xml
        assert "&amp;" in xml

    def test_text_travels_by_env_not_script(self, monkeypatch):
        import subprocess

        from pfsentinel.services import notifications as n

        seen = {}

        def fake_run(args, env=None, **kwargs):
            seen["args"], seen["env"] = args, env
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(n.subprocess, "run", fake_run)
        n.show_windows_toast("$(Remove-Item x)", "`; calc")
        assert seen["args"][0].lower().endswith("powershell.exe")
        assert "System32" in seen["args"][0]
        assert seen["args"][-1] == n._TOAST_SCRIPT  # script is static
        assert "Remove-Item" not in " ".join(seen["args"])
        assert "$(Remove-Item x)" in seen["env"]["PFS_TOAST_XML"]
        assert seen["env"]["PFS_TOAST_APP_ID"] == n._TOAST_APP_ID

    def test_failure_is_raised_so_dispatch_reports_it(self, monkeypatch):
        import subprocess

        from pfsentinel.services import notifications as n

        monkeypatch.setattr(
            n.subprocess,
            "run",
            lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "WinRT unavailable"),
        )
        with pytest.raises(RuntimeError, match="WinRT unavailable"):
            n.show_windows_toast("t", "m")


class TestSecretsNeverReachOutput:
    """Telegram puts the bot token in the URL, and a Slack webhook URL is the secret.

    requests quotes the URL in its error messages, so a failed send used to
    print the credential to the console and to scheduled-run logs.
    """

    TOKEN = "123456789:AAHsecretTOKENvalue_-x"
    WEBHOOK = "https://hooks.slack.com/services/T000/B000/SECRETpart"

    def _service(self, **flags):
        config = NotificationConfig(
            telegram_chat_id="42", notify_on_success=True, windows_toast_enabled=False, **flags
        )
        creds = CredentialService()
        creds.store_telegram_token(self.TOKEN)
        creds.store_slack_webhook(self.WEBHOOK)
        return NotificationService(config, creds)

    @responses.activate
    def test_telegram_http_error_hides_token(self, caplog):
        responses.add(
            responses.POST,
            f"https://api.telegram.org/bot{self.TOKEN}/sendMessage",
            status=401,
        )
        svc = self._service(telegram_enabled=True)
        with caplog.at_level("WARNING"):
            results = svc._dispatch("t", "m", success=True)
        assert results["Telegram"].startswith("HTTP 401")
        assert "AAHsecret" not in results["Telegram"]
        assert "AAHsecret" not in caplog.text

    @responses.activate
    def test_slack_connection_error_hides_webhook(self, caplog):
        responses.add(
            responses.POST,
            self.WEBHOOK,
            body=requests.ConnectionError(f"Max retries exceeded with url: {self.WEBHOOK}"),
        )
        svc = self._service(slack_enabled=True)
        with caplog.at_level("WARNING"):
            results = svc._dispatch("t", "m", success=True)
        assert "SECRETpart" not in results["Slack"]
        assert "SECRETpart" not in caplog.text

    def test_slack_webhook_on_another_host_is_refused(self):
        svc = self._service(slack_enabled=True)
        svc._creds.store_slack_webhook("https://evil.example/services/x")
        with patch("pfsentinel.services.notifications.requests.post") as post:
            results = svc._dispatch("t", "m", success=True)
        post.assert_not_called()
        assert results["Slack"].startswith("Slack webhook host must be")

    def test_log_formatter_redacts_tracebacks_too(self):
        import io
        import logging

        from pfsentinel.utils.logging import configure_logging

        buf = io.StringIO()
        configure_logging("DEBUG", stream=buf)
        log = logging.getLogger("pfsentinel.test")
        try:
            raise RuntimeError(f"failed for https://api.telegram.org/bot{self.TOKEN}/x")
        except RuntimeError:
            log.exception("send failed via %s", self.WEBHOOK)
        out = buf.getvalue()
        assert "AAHsecret" not in out
        assert "SECRETpart" not in out
        assert "bot<redacted>" in out
