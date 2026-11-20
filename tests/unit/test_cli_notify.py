"""CLI tests for `pfs notify` (Telegram, Slack, Event Log, test, status).

The HTTP edge is faked: `requests.get` and `requests.post` are replaced
with a recorder, so no test reaches Telegram or Slack. Everything between
the CLI and that edge (config, credential store, NotificationService) is
real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.services.credentials import CredentialService

TOKEN = "123456789:TESTtokenTESTtokenTESTtoken"
WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXXXXXX"


def _out(result) -> str:
    """Output with Rich line wrapping undone, so phrases can be matched."""
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


class _Response:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status_code = status
        self.reason = "OK" if status < 400 else "Bad Request"
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url", response=self)


@dataclass
class FakeHTTP:
    """Records outgoing requests and answers with canned responses."""

    get_payload: dict = field(default_factory=dict)
    post_status: int = 200
    get_error: Exception | None = None
    post_error: Exception | None = None
    gets: list[str] = field(default_factory=list)
    posts: list[tuple[str, Any]] = field(default_factory=list)

    def get(self, url: str, **kwargs) -> _Response:
        self.gets.append(url)
        if self.get_error:
            raise self.get_error
        return _Response(200, self.get_payload)

    def post(self, url: str, json: Any = None, **kwargs) -> _Response:
        self.posts.append((url, json))
        if self.post_error:
            raise self.post_error
        return _Response(self.post_status)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> FakeHTTP:
    fake = FakeHTTP()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


@pytest.fixture
def not_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pfsentinel.utils.platform.is_windows", lambda: False)
    monkeypatch.setattr("pfsentinel.services.notifications.is_windows", lambda: False)


def _chat_update(chat_id: int) -> dict:
    return {"result": [{"message": {"chat": {"id": chat_id}}}]}


def _telegram_configured(enabled: bool = True) -> None:
    CredentialService().store_telegram_token(TOKEN)
    config = AppConfig.load()
    config.notifications.telegram_chat_id = "4242"
    config.notifications.telegram_enabled = enabled
    config.save()


def _slack_configured(enabled: bool = True) -> None:
    CredentialService().store_slack_webhook(WEBHOOK)
    config = AppConfig.load()
    config.notifications.slack_enabled = enabled
    config.save()


# ---------------------------------------------------------------------------
# Telegram setup
# ---------------------------------------------------------------------------


class TestTelegramSetup:
    def test_detected_chat_id_is_saved_and_enabled(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.get_payload = _chat_update(777)
        result = cli_runner.invoke(app, ["notify", "telegram", "setup"], input=f"{TOKEN}\ny\nn\n")

        assert result.exit_code == 0, _out(result)
        config = AppConfig.load()
        assert config.notifications.telegram_chat_id == "777"
        assert config.notifications.telegram_enabled is True

    def test_token_goes_to_the_credential_store_not_the_config(
        self,
        cli_runner: CliRunner,
        saved_config: AppConfig,
        http: FakeHTTP,
        cli_config_path: Path,
    ) -> None:
        http.get_payload = _chat_update(777)
        cli_runner.invoke(app, ["notify", "telegram", "setup"], input=f"{TOKEN}\ny\nn\n")

        assert CredentialService().get_telegram_token() == TOKEN
        assert TOKEN not in cli_config_path.read_text(encoding="utf-8")

    def test_rejecting_detected_chat_id_uses_the_manual_one(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.get_payload = _chat_update(777)
        result = cli_runner.invoke(
            app, ["notify", "telegram", "setup"], input=f"{TOKEN}\nn\n-100555\nn\n"
        )

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().notifications.telegram_chat_id == "-100555"

    def test_manual_chat_id_when_detection_finds_nothing(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.get_payload = {"result": []}
        result = cli_runner.invoke(
            app, ["notify", "telegram", "setup"], input=f"{TOKEN}\n31337\nn\n"
        )

        assert result.exit_code == 0, _out(result)
        assert "auto-detect" in _out(result)
        assert AppConfig.load().notifications.telegram_chat_id == "31337"

    def test_network_error_during_detection_falls_back_to_manual(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.get_error = requests.ConnectionError("offline")
        result = cli_runner.invoke(
            app, ["notify", "telegram", "setup"], input=f"{TOKEN}\n31337\nn\n"
        )

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().notifications.telegram_chat_id == "31337"

    def test_empty_manual_chat_id_cancels_without_saving(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.get_payload = {"result": []}
        result = cli_runner.invoke(app, ["notify", "telegram", "setup"], input=f"{TOKEN}\n\n")

        assert result.exit_code == 0, _out(result)
        assert "cancelled" in _out(result).lower()
        assert AppConfig.load().notifications.telegram_enabled is False
        assert CredentialService().get_telegram_token() is None

    def test_no_input_aborts_cleanly(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "telegram", "setup"], input="")

        assert result.exit_code == 0, _out(result)
        assert "Aborted" in _out(result)
        assert http.gets == []

    def test_abort_at_the_confirm_saves_nothing(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.get_payload = _chat_update(777)
        result = cli_runner.invoke(app, ["notify", "telegram", "setup"], input=f"{TOKEN}\n")

        assert "Aborted" in _out(result)
        assert AppConfig.load().notifications.telegram_chat_id is None

    def test_test_message_is_sent_to_the_chosen_chat(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.get_payload = _chat_update(777)
        result = cli_runner.invoke(app, ["notify", "telegram", "setup"], input=f"{TOKEN}\ny\ny\n")

        assert result.exit_code == 0, _out(result)
        assert "sent" in _out(result).lower()
        assert len(http.posts) == 1
        assert http.posts[0][1]["chat_id"] == "777"

    def test_failed_test_message_never_prints_the_token(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.get_payload = _chat_update(777)
        http.post_status = 400
        result = cli_runner.invoke(app, ["notify", "telegram", "setup"], input=f"{TOKEN}\ny\ny\n")

        output = _out(result)
        assert "Failed to send test message" in output
        assert "HTTP 400" in output
        assert TOKEN not in output


class TestTelegramToggles:
    def test_enable_without_setup_fails(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "telegram", "enable"])

        assert result.exit_code == 1
        assert "not configured" in _out(result)

    def test_enable_after_setup_turns_it_on(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _telegram_configured(enabled=False)
        result = cli_runner.invoke(app, ["notify", "telegram", "enable"])

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().notifications.telegram_enabled is True

    def test_disable_keeps_the_token(self, cli_runner: CliRunner, saved_config: AppConfig) -> None:
        _telegram_configured()
        result = cli_runner.invoke(app, ["notify", "telegram", "disable"])

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().notifications.telegram_enabled is False
        assert CredentialService().get_telegram_token() == TOKEN


class TestTelegramStatus:
    def test_fully_configured(self, cli_runner: CliRunner, saved_config: AppConfig) -> None:
        _telegram_configured()
        result = cli_runner.invoke(app, ["notify", "telegram", "status"])

        assert result.exit_code == 0, _out(result)
        assert "fully configured" in _out(result)
        assert "4242" in _out(result)

    def test_configured_but_disabled_suggests_enable(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _telegram_configured(enabled=False)
        result = cli_runner.invoke(app, ["notify", "telegram", "status"])

        assert "pfs notify telegram enable" in _out(result)

    def test_not_configured_suggests_setup(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "telegram", "status"])

        output = _out(result)
        assert "missing" in output
        assert "pfs notify telegram setup" in output


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


class TestSlackSetup:
    def test_valid_webhook_is_stored_and_enabled(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "slack", "setup"], input=f"{WEBHOOK}\nn\n")

        assert result.exit_code == 0, _out(result)
        assert CredentialService().get_slack_webhook() == WEBHOOK
        assert AppConfig.load().notifications.slack_enabled is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://hooks.slack.com/services/T/B/X",
            "https://hooks.slack.com.evil.example/services/T/B/X",
            "https://example.com/hook",
        ],
    )
    def test_non_slack_url_is_refused_and_nothing_saved(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP, url: str
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "slack", "setup"], input=f"{url}\n")

        assert result.exit_code == 1
        assert "not a Slack webhook" in _out(result)
        assert CredentialService().get_slack_webhook() is None
        assert AppConfig.load().notifications.slack_enabled is False

    def test_no_input_aborts_cleanly(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "slack", "setup"], input="")

        assert result.exit_code == 0
        assert "Aborted" in _out(result)

    def test_test_message_goes_to_the_webhook(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "slack", "setup"], input=f"{WEBHOOK}\ny\n")

        assert result.exit_code == 0, _out(result)
        assert "sent" in _out(result).lower()
        assert [url for url, _ in http.posts] == [WEBHOOK]

    def test_network_failure_on_test_message_is_reported(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.post_error = requests.ConnectionError(f"cannot reach {WEBHOOK}")
        result = cli_runner.invoke(app, ["notify", "slack", "setup"], input=f"{WEBHOOK}\ny\n")

        output = _out(result)
        assert "Failed to send test message" in output
        assert WEBHOOK not in output

    def test_unexpected_failure_on_test_message_is_reported(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        http.post_error = ValueError(f"odd failure for {WEBHOOK}")
        result = cli_runner.invoke(app, ["notify", "slack", "setup"], input=f"{WEBHOOK}\ny\n")

        output = _out(result)
        assert "Failed to send test message" in output
        assert WEBHOOK not in output


class TestSlackTogglesAndStatus:
    def test_enable_without_setup_fails(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "slack", "enable"])

        assert result.exit_code == 1
        assert "not configured" in _out(result)

    def test_enable_after_setup_turns_it_on(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _slack_configured(enabled=False)
        result = cli_runner.invoke(app, ["notify", "slack", "enable"])

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().notifications.slack_enabled is True

    def test_disable_keeps_the_webhook(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _slack_configured()
        result = cli_runner.invoke(app, ["notify", "slack", "disable"])

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().notifications.slack_enabled is False
        assert CredentialService().get_slack_webhook() == WEBHOOK

    def test_status_fully_configured(self, cli_runner: CliRunner, saved_config: AppConfig) -> None:
        _slack_configured()
        result = cli_runner.invoke(app, ["notify", "slack", "status"])

        assert "fully configured" in _out(result)

    def test_status_configured_but_disabled(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _slack_configured(enabled=False)
        result = cli_runner.invoke(app, ["notify", "slack", "status"])

        assert "pfs notify slack enable" in _out(result)

    def test_status_not_configured(self, cli_runner: CliRunner, saved_config: AppConfig) -> None:
        result = cli_runner.invoke(app, ["notify", "slack", "status"])

        assert "pfs notify slack setup" in _out(result)


# ---------------------------------------------------------------------------
# Event Log / syslog
# ---------------------------------------------------------------------------


class TestEventLog:
    def test_enable_on_linux_writes_to_syslog(
        self, cli_runner: CliRunner, saved_config: AppConfig, not_windows: None
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "eventlog", "enable"])

        assert result.exit_code == 0, _out(result)
        assert "syslog" in _out(result)
        assert AppConfig.load().notifications.windows_event_log_enabled is True

    def test_enable_on_windows_registers_the_source(
        self, cli_runner: CliRunner, saved_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registered: list[bool] = []
        monkeypatch.setattr("pfsentinel.utils.platform.is_windows", lambda: True)
        monkeypatch.setattr(
            "pfsentinel.services.notifications.register_event_source",
            lambda: registered.append(True),
        )
        result = cli_runner.invoke(app, ["notify", "eventlog", "enable"])

        assert result.exit_code == 0, _out(result)
        assert registered == [True]
        assert "Event Log" in _out(result)

    def test_enable_on_windows_without_admin_explains_and_saves_nothing(
        self, cli_runner: CliRunner, saved_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse() -> None:
            raise RuntimeError("access denied")

        monkeypatch.setattr("pfsentinel.utils.platform.is_windows", lambda: True)
        monkeypatch.setattr("pfsentinel.utils.platform.is_elevated", lambda: False)
        monkeypatch.setattr("pfsentinel.services.notifications.register_event_source", refuse)
        result = cli_runner.invoke(app, ["notify", "eventlog", "enable"])

        assert result.exit_code == 1
        assert "administrator" in _out(result).lower()
        assert AppConfig.load().notifications.windows_event_log_enabled is False

    def test_enable_on_windows_as_admin_shows_the_error(
        self, cli_runner: CliRunner, saved_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> None:
            raise RuntimeError("registry is read-only")

        monkeypatch.setattr("pfsentinel.utils.platform.is_windows", lambda: True)
        monkeypatch.setattr("pfsentinel.utils.platform.is_elevated", lambda: True)
        monkeypatch.setattr("pfsentinel.services.notifications.register_event_source", broken)
        result = cli_runner.invoke(app, ["notify", "eventlog", "enable"])

        assert result.exit_code == 1
        assert "registry is read-only" in _out(result)

    def test_disable_turns_it_off(self, cli_runner: CliRunner, saved_config: AppConfig) -> None:
        config = AppConfig.load()
        config.notifications.windows_event_log_enabled = True
        config.save()
        result = cli_runner.invoke(app, ["notify", "eventlog", "disable"])

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().notifications.windows_event_log_enabled is False


# ---------------------------------------------------------------------------
# notify test / notify status
# ---------------------------------------------------------------------------


class TestNotifyTest:
    def test_no_channels_enabled_tells_how_to_set_one_up(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP
    ) -> None:
        config = AppConfig.load()
        config.notifications.windows_toast_enabled = False
        config.save()
        result = cli_runner.invoke(app, ["notify", "test"])

        assert result.exit_code == 0
        assert "No notification channels enabled" in _out(result)
        assert http.posts == []

    def test_all_channels_succeed(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP, not_windows: None
    ) -> None:
        _telegram_configured()
        _slack_configured()
        result = cli_runner.invoke(app, ["notify", "test"])

        assert "All notifications sent successfully" in _out(result)
        assert len(http.posts) == 2

    def test_partial_failure_is_reported_per_channel(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP, not_windows: None
    ) -> None:
        _telegram_configured()
        # Slack is enabled but its webhook is gone, so only Telegram can send.
        config = AppConfig.load()
        config.notifications.slack_enabled = True
        config.save()
        result = cli_runner.invoke(app, ["notify", "test"])

        output = _out(result)
        assert "Telegram: sent" in output
        assert "Slack: failed" in output
        assert "Some notifications failed" in output

    def test_every_channel_failing_is_reported(
        self, cli_runner: CliRunner, saved_config: AppConfig, http: FakeHTTP, not_windows: None
    ) -> None:
        _telegram_configured()
        http.post_status = 500
        result = cli_runner.invoke(app, ["notify", "test"])

        output = _out(result)
        assert "Telegram: failed" in output
        assert "All notifications failed" in output
        assert TOKEN not in output


class TestNotifyStatus:
    def test_lists_every_channel(
        self, cli_runner: CliRunner, saved_config: AppConfig, not_windows: None
    ) -> None:
        result = cli_runner.invoke(app, ["notify", "status"])

        output = _out(result)
        assert result.exit_code == 0, output
        for channel in ("Telegram", "Slack", "Windows Toast", "syslog"):
            assert channel in output

    def test_shows_enabled_channels(
        self, cli_runner: CliRunner, saved_config: AppConfig, not_windows: None
    ) -> None:
        _telegram_configured()
        result = cli_runner.invoke(app, ["notify", "status"])

        assert "Telegram: enabled" in _out(result)

    def test_shows_the_stale_backup_threshold(
        self, cli_runner: CliRunner, saved_config: AppConfig, not_windows: None
    ) -> None:
        config = AppConfig.load()
        config.notifications.stale_after_days = 9
        config.save()
        result = cli_runner.invoke(app, ["notify", "status"])

        assert "9 day(s)" in _out(result)
