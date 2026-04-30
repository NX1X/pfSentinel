"""Tests for notification service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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
        assert "api.telegram.org" in call_kwargs[0][0]


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
