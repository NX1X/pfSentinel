"""Alert channels and the scheduled no-recent-backup check (operator request, 2026-11-20)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.config import NotificationConfig
from pfsentinel.services import notifications as notif
from pfsentinel.services.notifications import AlertKind, NotificationService
from pfsentinel.utils import platform, unix_schedule


def test_event_ids_are_fixed() -> None:
    """Event Log filters and alerts rely on these numbers: never renumber them."""
    assert {k.name: int(k) for k in AlertKind} == {
        "BACKUP_OK": 1000,
        "BACKUP_FAILED": 1001,
        "RISKY_CHANGE": 1002,
        "NO_RECENT_BACKUP": 1003,
        "RESTORE_CHECK_FAILED": 1004,
        "SECOND_COPY_FAILED": 1005,
        "UPDATE_AVAILABLE": 1006,
        "DAILY_DIGEST": 1007,
    }
    assert AlertKind.BACKUP_OK.entry_type == "Information"
    assert AlertKind.RISKY_CHANGE.entry_type == "Warning"
    assert AlertKind.NO_RECENT_BACKUP.entry_type == "Error"
    assert AlertKind.DAILY_DIGEST.entry_type == "Information"


def test_windows_event_text_travels_in_environment_variables() -> None:
    """The message is never part of the PowerShell command line."""
    with (
        patch.object(notif, "is_windows", return_value=True),
        patch.object(notif, "_run_powershell") as run,
    ):
        notif.write_system_log(AlertKind.RISKY_CHANGE, "Title", "'; Remove-Item C:\\ -Recurse")
    script, env = run.call_args.args
    assert "Remove-Item" not in script
    assert env["PFS_EVENT_ID"] == "1002"
    assert env["PFS_EVENT_TYPE"] == "Warning"
    assert env["PFS_EVENT_SOURCE"] == "pfSentinel"
    assert "Remove-Item" in env["PFS_EVENT_MSG"]


def test_unregistered_source_says_how_to_fix_it() -> None:
    with (
        patch.object(notif, "is_windows", return_value=True),
        patch.object(
            notif, "_run_powershell", side_effect=RuntimeError("The source was not found")
        ),
        pytest.raises(RuntimeError, match="pfs notify eventlog enable"),
    ):
        notif.write_system_log(AlertKind.BACKUP_FAILED, "t", "m")


@pytest.mark.skipif(not hasattr(__import__("os"), "uname"), reason="syslog is POSIX only")
def test_linux_writes_syslog_with_the_event_id() -> None:
    fake = MagicMock(LOG_INFO=6, LOG_WARNING=4, LOG_ERR=3, LOG_USER=8)
    with (
        patch.object(notif, "is_windows", return_value=False),
        patch.dict("sys.modules", {"syslog": fake}),
    ):
        notif.write_system_log(AlertKind.NO_RECENT_BACKUP, "No recent backup", "Device: fw")
    fake.openlog.assert_called_once_with("pfsentinel", 0, 8)
    lines = [c.args for c in fake.syslog.call_args_list]
    assert lines == [(3, "[1003] No recent backup"), (3, "[1003] Device: fw")]


class TestDispatch:
    def _service(self, **config) -> NotificationService:
        cfg = NotificationConfig(windows_toast_enabled=False, **config)
        return NotificationService(cfg, MagicMock())

    def test_alert_reaches_the_system_log_with_its_kind(self) -> None:
        svc = self._service(windows_event_log_enabled=True)
        with patch.object(notif, "write_system_log") as write:
            results = svc.alert(AlertKind.RISKY_CHANGE, "t", "m")
        write.assert_called_once_with(AlertKind.RISKY_CHANGE, "t", "m")
        assert list(results.values()) == [None]

    def test_alerts_follow_notify_on_failure(self) -> None:
        svc = self._service(windows_event_log_enabled=True, notify_on_failure=False)
        with patch.object(notif, "write_system_log") as write:
            assert svc.alert(AlertKind.RISKY_CHANGE, "t", "m") == {}
        write.assert_not_called()

    def test_a_failing_channel_does_not_stop_the_others(self) -> None:
        svc = self._service(windows_event_log_enabled=True, slack_enabled=True)
        with (
            patch.object(notif, "write_system_log", side_effect=RuntimeError("boom")),
            patch.object(svc, "_send_slack") as slack,
        ):
            results = svc.alert(AlertKind.BACKUP_FAILED, "t", "m")
        slack.assert_called_once()
        assert results["Slack"] is None
        assert "boom" in (results.get("System log") or results.get("Windows Event Log") or "")


class TestScheduledCheck:
    def test_windows_task_runs_at_sign_in_and_daily(self, tmp_path: Path) -> None:
        captured: dict[str, str] = {}

        def fake_run(args, check=True):
            captured["xml"] = Path(args[args.index("/XML") + 1]).read_bytes().decode("utf-16")
            return MagicMock(returncode=0)

        with (
            patch.object(platform, "is_windows", return_value=True),
            patch.object(platform, "run_command", side_effect=fake_run),
            patch.object(platform, "_current_user_id", return_value="PC\\me"),
        ):
            assert platform.create_windows_task(
                "pfSentinel\\BackupCheck",
                "C:\\tools\\pfs.exe",
                "backup check-recent --non-interactive",
                "CHECK",
                "12:00",
            )
        xml = captured["xml"]
        assert "<LogonTrigger>" in xml and "<Delay>PT5M</Delay>" in xml
        assert "<UserId>PC\\me</UserId>" in xml
        assert "T12:00:00</StartBoundary>" in xml
        assert "<LogonType>InteractiveToken</LogonType>" in xml
        assert "backup check-recent --non-interactive" in xml

    def test_systemd_check_timer(self) -> None:
        timer = unix_schedule.render_check_timer()
        assert "OnStartupSec=5min" in timer
        assert "OnCalendar=*-*-* 12:00:00" in timer
        assert f"Unit={unix_schedule.CHECK_SERVICE}" in timer

    def test_cron_check_lines(self, tmp_path: Path) -> None:
        lines = unix_schedule.cron_lines(
            ["/usr/bin/pfs", "backup", "run"],
            "02:00",
            None,
            tmp_path / "log",
            check_argv=["/usr/bin/pfs", "backup", "check-recent", "--non-interactive"],
        )
        assert any(ln.startswith("@reboot sleep 300 && ") and "check-recent" in ln for ln in lines)
        assert any(ln.startswith("0 12 * * * ") and "check-recent" in ln for ln in lines)
        assert all(unix_schedule.CRON_MARKER in ln for ln in lines)
