"""Daily digest: alert history, when it is due, what it says, and once-a-day sending."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.backup import BackupRecord, BackupType
from pfsentinel.models.config import AppConfig, NotificationConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services import alert_history, digest
from pfsentinel.services.alert_history import AlertEntry
from pfsentinel.services.notifications import AlertKind, NotificationService

NOW = datetime(2026, 11, 20, 8, 30)


def _record(device: str, at: datetime, kind: BackupType = BackupType.CONFIG) -> BackupRecord:
    return BackupRecord(
        device_id=device,
        filename=f"{device}-{at:%H%M}.xml",
        relative_path=f"x/{device}-{at:%H%M}.xml",
        created_at=at,
        backup_type=kind,
    )


def _app_config(*device_ids: str, **notifications) -> AppConfig:
    config = AppConfig(notifications=NotificationConfig(**notifications))
    for device_id in device_ids:
        config.add_device(DeviceConfig(id=device_id, label=device_id, host="10.0.0.1"))
    return config


class TestAlertHistory:
    def test_records_device_and_summary(self, _isolate_alert_state: Path) -> None:
        alert_history.record_alert(
            AlertKind.BACKUP_FAILED,
            "pfSentinel - Backup Failed",
            "Device: fw1\nError: refused",
            NOW,
        )
        [entry] = alert_history.load_alerts(NOW - timedelta(hours=1))
        assert entry == AlertEntry(NOW, 1001, "fw1", "Error: refused")

    def test_alert_without_a_device_keeps_its_title(self) -> None:
        alert_history.record_alert(AlertKind.UPDATE_AVAILABLE, "Update: v9", "", NOW)
        [entry] = alert_history.load_alerts(NOW - timedelta(hours=1))
        assert entry.device is None and entry.summary == "Update: v9"

    def test_secrets_and_long_lines_are_trimmed(self) -> None:
        long_error = "Error: " + "x" * 500 + " https://api.telegram.org/bot123:ABCdef/sendMessage"
        alert_history.record_alert(AlertKind.BACKUP_FAILED, "t", f"Device: fw\n{long_error}", NOW)
        [entry] = alert_history.load_alerts(NOW - timedelta(hours=1))
        assert len(entry.summary) == 200 and entry.summary.endswith("...")
        alert_history.record_alert(
            AlertKind.BACKUP_FAILED, "t", "Device: fw\nbot123:ABCdef failed", NOW
        )
        assert "ABCdef" not in alert_history.load_alerts(NOW)[-1].summary

    def test_old_entries_are_pruned(self, _isolate_alert_state: Path) -> None:
        alert_history.record_alert(AlertKind.BACKUP_FAILED, "t", "Device: a", NOW - timedelta(8))
        alert_history.record_alert(AlertKind.BACKUP_FAILED, "t", "Device: b", NOW)
        stored = json.loads((_isolate_alert_state / alert_history.HISTORY_FILE).read_text())
        assert [e["device"] for e in stored] == ["b"]

    def test_load_filters_by_time(self) -> None:
        alert_history.record_alert(AlertKind.RISKY_CHANGE, "t", "Device: a", NOW - timedelta(2))
        alert_history.record_alert(AlertKind.RISKY_CHANGE, "t", "Device: b", NOW)
        assert [e.device for e in alert_history.load_alerts(NOW - timedelta(1))] == ["b"]

    def test_corrupt_file_starts_over(self, _isolate_alert_state: Path) -> None:
        (_isolate_alert_state / alert_history.HISTORY_FILE).write_text("{not json")
        assert alert_history.load_alerts(NOW - timedelta(1)) == []
        alert_history.record_alert(AlertKind.RISKY_CHANGE, "t", "Device: a", NOW)
        assert len(alert_history.load_alerts(NOW - timedelta(1))) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_history_is_owner_only(self, _isolate_alert_state: Path) -> None:
        alert_history.record_alert(AlertKind.RISKY_CHANGE, "t", "Device: a", NOW)
        mode = os.stat(_isolate_alert_state / alert_history.HISTORY_FILE).st_mode & 0o777
        assert mode == 0o600

    def test_a_write_error_does_not_stop_the_alert(self) -> None:
        with patch.object(alert_history, "write_private_json", side_effect=OSError("disk full")):
            alert_history.record_alert(AlertKind.RISKY_CHANGE, "t", "Device: a", NOW)


class TestDue:
    @pytest.mark.parametrize(
        ("enabled", "at", "now", "last", "due"),
        [
            (True, "08:00", NOW, None, True),
            (True, "08:00", NOW, NOW.date() - timedelta(1), True),
            (True, "08:00", NOW, NOW.date(), False),
            (True, "09:00", NOW, None, False),
            (True, "08:30", NOW, None, True),
            (False, "08:00", NOW, None, False),
        ],
    )
    def test_is_due(self, enabled, at, now, last, due) -> None:
        config = NotificationConfig(digest_enabled=enabled, digest_time=at)
        assert digest.is_due(config, now, last) is due

    def test_invalid_time_falls_back_to_eight(self) -> None:
        config = NotificationConfig(digest_enabled=True, digest_time="25:99")
        assert digest.digest_time(config).hour == 8
        assert digest.is_due(config, NOW, None)


class TestRender:
    def test_summary_per_device(self) -> None:
        backups = {
            "fw1": [
                _record("fw1", NOW - timedelta(hours=6)),
                _record("fw1", NOW - timedelta(hours=7), BackupType.RRD),
                _record("fw1", NOW - timedelta(days=3)),
            ],
            "fw2": [],
        }
        alerts = [
            AlertEntry(NOW - timedelta(hours=2), 1001, "fw2", "Error: timed out"),
            AlertEntry(NOW - timedelta(hours=1), 1003, "fw2", "Last successful backup: never"),
            AlertEntry(NOW - timedelta(hours=1), 1006, None, "Installed: v0.4.0"),
        ]
        summaries = digest.summarize_devices(["fw1", "fw2"], backups.__getitem__, alerts, NOW)
        result = digest.render_digest(summaries, [alerts[2]], NOW)

        assert result.title == "pfSentinel - Daily summary: 2 problem(s)"
        assert result.problem_count == 2
        body = result.body
        assert "fw1: OK" in body
        assert "Last successful config backup: 2026-11-20 02:30" in body
        assert "Backups stored: 3 (2 in the last 24 hours)" in body
        assert "fw2: 2 problem(s)" in body
        assert "Last successful config backup: never" in body
        assert "2026-11-20 06:30 Backup failed: Error: timed out" in body
        assert "No recent backup: Last successful backup: never" in body
        assert "Other alerts in the last 24 hours:" in body
        assert "Update available: Installed: v0.4.0" in body

    def test_all_ok_title(self) -> None:
        result = digest.render_digest([digest.DeviceSummary("fw", NOW, 1, 1)], [], NOW)
        assert result.title == "pfSentinel - Daily summary: all OK"
        assert result.problem_count == 0
        assert "Problems in the last 24 hours: none" in result.body

    def test_no_devices(self) -> None:
        assert "No enabled devices." in digest.render_digest([], [], NOW).body

    def test_build_reads_the_alert_history(self) -> None:
        config = _app_config("fw1")
        alert_history.record_alert(AlertKind.RISKY_CHANGE, "t", "Device: fw1\nChange: x", NOW)
        result = digest.build_digest(config, lambda _: [], NOW + timedelta(minutes=1))
        assert result.problem_count == 1
        assert "Risky change: Change: x" in result.body


class TestSending:
    def _notifications(self, results: dict[str, str | None]) -> MagicMock:
        svc = MagicMock(spec=NotificationService)
        svc.send_digest.return_value = results
        return svc

    def test_sent_once_per_day(self) -> None:
        config = _app_config("fw1", digest_enabled=True, digest_time="08:00")
        svc = self._notifications({"Email": None})
        assert digest.send_digest_if_due(config, lambda _: [], svc, NOW) == {"Email": None}
        assert digest.send_digest_if_due(config, lambda _: [], svc, NOW) is None
        assert svc.send_digest.call_count == 1
        assert digest.last_sent_date() == NOW.date()
        tomorrow = NOW + timedelta(days=1)
        assert digest.send_digest_if_due(config, lambda _: [], svc, tomorrow) is not None

    def test_not_before_its_time(self) -> None:
        config = _app_config("fw1", digest_enabled=True, digest_time="09:00")
        svc = self._notifications({"Email": None})
        assert digest.send_digest_if_due(config, lambda _: [], svc, NOW) is None
        svc.send_digest.assert_not_called()

    def test_retried_when_every_channel_failed(self) -> None:
        config = _app_config("fw1", digest_enabled=True)
        svc = self._notifications({"Email": "refused", "Slack": "HTTP 500"})
        digest.send_digest_if_due(config, lambda _: [], svc, NOW)
        assert digest.last_sent_date() is None
        digest.send_digest_if_due(config, lambda _: [], svc, NOW)
        assert svc.send_digest.call_count == 2

    def test_one_working_channel_is_enough(self) -> None:
        config = _app_config("fw1", digest_enabled=True)
        svc = self._notifications({"Email": "refused", "Slack": None})
        digest.send_digest_if_due(config, lambda _: [], svc, NOW)
        assert digest.last_sent_date() == NOW.date()

    def test_manual_send_does_not_count(self) -> None:
        config = _app_config("fw1", digest_enabled=True)
        svc = self._notifications({"Email": None})
        digest.send_digest(config, lambda _: [], svc, NOW, remember=False)
        assert digest.last_sent_date() is None

    def test_problems_color_the_message(self) -> None:
        config = _app_config("fw1", digest_enabled=True)
        alert_history.record_alert(AlertKind.BACKUP_FAILED, "t", "Device: fw1\nError: x", NOW)
        svc = self._notifications({"Email": None})
        digest.send_digest(config, lambda _: [], svc, NOW + timedelta(minutes=1))
        title, _, has_problems = svc.send_digest.call_args.args
        assert has_problems is True and "1 problem(s)" in title

    def test_corrupt_state_counts_as_never_sent(self, _isolate_alert_state: Path) -> None:
        (_isolate_alert_state / digest.STATE_FILE).write_text('{"last_sent": "yesterday"}')
        assert digest.last_sent_date() is None
        digest.mark_sent(date(2026, 1, 2))
        assert digest.last_sent_date() == date(2026, 1, 2)


class TestNotificationServiceWithDigest:
    def _service(self, **config) -> NotificationService:
        cfg = NotificationConfig(windows_toast_enabled=False, linux_desktop_enabled=False, **config)
        return NotificationService(cfg, MagicMock())

    def test_backup_ok_is_not_sent_per_event(self) -> None:
        svc = self._service(digest_enabled=True)
        with patch.object(svc, "_dispatch") as dispatch:
            svc.notify_success(_record("fw1", NOW))
        dispatch.assert_not_called()

    def test_failures_are_still_sent_at_once_and_recorded(self) -> None:
        svc = self._service(digest_enabled=True)
        with patch.object(svc, "_dispatch", return_value={}) as dispatch:
            svc.notify_failure("fw1", "connection refused")
        assert dispatch.call_args.kwargs["kind"] is AlertKind.BACKUP_FAILED
        [entry] = alert_history.load_alerts(datetime.now() - timedelta(minutes=1))
        assert (entry.kind, entry.device) == (1001, "fw1")

    def test_recorded_even_when_alerts_are_muted(self) -> None:
        svc = self._service(digest_enabled=True, notify_on_failure=False)
        svc.alert(AlertKind.RISKY_CHANGE, "t", "Device: fw1\nChange: x")
        assert len(alert_history.load_alerts(datetime.now() - timedelta(minutes=1))) == 1

    def test_nothing_recorded_without_the_digest(self, _isolate_alert_state: Path) -> None:
        svc = self._service()
        with patch.object(svc, "_dispatch", return_value={}):
            svc.alert(AlertKind.RISKY_CHANGE, "t", "Device: fw1")
        assert not (_isolate_alert_state / alert_history.HISTORY_FILE).exists()

    def test_digest_uses_its_own_event_id(self) -> None:
        svc = self._service(digest_enabled=True)
        with patch.object(svc, "_dispatch", return_value={}) as dispatch:
            svc.send_digest("title", "body", has_problems=False)
        assert dispatch.call_args.kwargs == {"success": True, "kind": AlertKind.DAILY_DIGEST}
