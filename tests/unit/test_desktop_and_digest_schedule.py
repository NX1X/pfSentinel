"""Linux desktop notifications (notify-send) and the daily check run at the digest time."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.config import AppConfig, NotificationConfig, ScheduleConfig
from pfsentinel.services import notifications as notif
from pfsentinel.services import scheduler as scheduler_mod
from pfsentinel.services.notifications import AlertKind, NotificationService
from pfsentinel.services.scheduler import SchedulerService
from pfsentinel.utils import platform, unix_schedule

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="notify-send")


@pytest.fixture
def fake_notify_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A notify-send on PATH that writes its arguments, NUL separated, to a file.

    Returns a function that installs a script body and gives the output path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "args"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PFS_FAKE_OUT", str(out))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

    def install(body: str = "exit 0") -> Path:
        script = bin_dir / "notify-send"
        script.write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do printf \'%s\\0\' "$a"; done > "$PFS_FAKE_OUT"\n' + body + "\n"
        )
        script.chmod(0o755)
        return out

    return install


def _args(out: Path) -> list[str]:
    return out.read_text().split("\0")[:-1]


def _service(**config) -> NotificationService:
    cfg = NotificationConfig(windows_toast_enabled=False, **config)
    return NotificationService(cfg, MagicMock())


@linux_only
class TestNotifySend:
    def test_arguments(self, fake_notify_send) -> None:
        out = fake_notify_send()
        shown = notif.show_linux_notification(
            "-pfSentinel - Backup Failed", "Device: fw\n<b>x</b> & y", AlertKind.BACKUP_FAILED
        )
        assert shown is True
        assert _args(out) == [
            "--app-name=pfSentinel",
            "--urgency=critical",
            "--",
            "-pfSentinel - Backup Failed",
            "Device: fw\n&lt;b&gt;x&lt;/b&gt; &amp; y",
        ]

    def test_information_is_normal_urgency(self, fake_notify_send) -> None:
        out = fake_notify_send()
        notif.show_linux_notification("t", "m", AlertKind.DAILY_DIGEST)
        assert "--urgency=normal" in _args(out)

    def test_every_alert_kind_reaches_the_desktop(self, fake_notify_send) -> None:
        out = fake_notify_send()
        svc = _service()
        for kind in AlertKind:
            out.unlink(missing_ok=True)
            assert svc._dispatch(f"title {kind.name}", "m", success=True, kind=kind) == {
                "Desktop": None
            }
            assert f"title {kind.name}" in _args(out)

    def test_no_session_is_a_silent_no_op(self, fake_notify_send, monkeypatch) -> None:
        out = fake_notify_send()
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS")
        assert notif.show_linux_notification("t", "m", AlertKind.BACKUP_OK) is False
        assert _service().alert(AlertKind.BACKUP_FAILED, "t", "m") == {}
        assert not out.exists()

    def test_not_installed_is_a_silent_no_op(self, fake_notify_send) -> None:
        assert notif.show_linux_notification("t", "m", AlertKind.BACKUP_OK) is False
        assert _service().alert(AlertKind.BACKUP_FAILED, "t", "m") == {}

    def test_no_notification_daemon_is_a_silent_no_op(self, fake_notify_send) -> None:
        fake_notify_send(
            "echo 'GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: The name "
            "org.freedesktop.Notifications was not provided by any .service files' >&2; exit 1"
        )
        assert notif.show_linux_notification("t", "m", AlertKind.BACKUP_OK) is False
        assert _service().alert(AlertKind.BACKUP_FAILED, "t", "m") == {}

    def test_other_failures_are_reported(self, fake_notify_send) -> None:
        fake_notify_send("echo 'Invalid number of options.' >&2; exit 1")
        with pytest.raises(RuntimeError, match="Invalid number of options"):
            notif.show_linux_notification("t", "m", AlertKind.BACKUP_OK)
        results = _service().alert(AlertKind.BACKUP_FAILED, "t", "m")
        assert results == {"Desktop": "Invalid number of options."}

    def test_can_be_turned_off(self, fake_notify_send) -> None:
        out = fake_notify_send()
        assert _service(linux_desktop_enabled=False).alert(AlertKind.BACKUP_FAILED, "t", "m") == {}
        assert not out.exists()


def test_runs_an_absolute_path_without_a_shell(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    with (
        patch.object(notif, "is_linux", return_value=True),
        patch.object(notif.shutil, "which", return_value="/usr/bin/notify-send"),
        patch.object(notif.subprocess, "run", return_value=MagicMock(returncode=0)) as run,
    ):
        assert notif.show_linux_notification("t; rm -rf ~", "$(id)", AlertKind.BACKUP_OK)
    args, kwargs = run.call_args
    assert args[0][0] == "/usr/bin/notify-send"
    assert args[0][-2:] == ["t; rm -rf ~", "$(id)"]
    assert "shell" not in kwargs
    assert kwargs["timeout"] == 10


def test_not_linux_is_a_no_op(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    with (
        patch.object(notif, "is_linux", return_value=False),
        patch.object(notif.subprocess, "run") as run,
    ):
        assert notif.show_linux_notification("t", "m", AlertKind.BACKUP_OK) is False
    run.assert_not_called()


class TestCheckRunsAtTheDigestTime:
    def test_check_times(self) -> None:
        assert unix_schedule.check_times(None) == ["12:00"]
        assert unix_schedule.check_times("7:05") == ["12:00", "07:05"]
        assert unix_schedule.check_times("12:00") == ["12:00"]

    def test_systemd_timer_gets_a_second_calendar(self) -> None:
        timer = unix_schedule.render_check_timer("07:30")
        assert "OnCalendar=*-*-* 12:00:00\nOnCalendar=*-*-* 07:30:00\n" in timer
        assert unix_schedule.render_check_timer().count("OnCalendar=") == 1

    def test_cron_gets_a_digest_line(self, tmp_path: Path) -> None:
        lines = unix_schedule.cron_lines(
            ["/usr/bin/pfs", "backup", "run"],
            "02:00",
            None,
            tmp_path / "log",
            check_argv=["/usr/bin/pfs", "backup", "check-recent", "--non-interactive"],
            check_extra_at="18:15",
        )
        digest_lines = [ln for ln in lines if ln.endswith("check-digest")]
        assert len(digest_lines) == 1 and digest_lines[0].startswith("15 18 * * * ")

    def test_windows_check_task_gets_a_second_daily_trigger(self) -> None:
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
                "C:\\pfs.exe",
                "backup check-recent",
                "CHECK",
                "12:00",
                extra_times=("07:30",),
            )
        xml = captured["xml"]
        assert xml.count("<CalendarTrigger>") == 2
        assert "T12:00:00</StartBoundary>" in xml and "T07:30:00</StartBoundary>" in xml
        assert "<LogonTrigger>" in xml

    def test_scheduler_passes_the_digest_time(self) -> None:
        svc = SchedulerService(ScheduleConfig(enabled=True), digest_at="07:30")
        us = scheduler_mod.unix_schedule
        with (
            patch.object(scheduler_mod, "is_windows", return_value=False),
            patch.object(us, "systemd_user_available", return_value=True),
            patch.object(us, "install_systemd_timers", return_value=True) as inst,
            patch.object(us, "remove_cron", return_value=True),
        ):
            assert svc.apply_schedule() is True
        assert inst.call_args.kwargs["check_extra_at"] == "07:30"

    def test_windows_scheduler_passes_the_digest_time(self) -> None:
        svc = SchedulerService(ScheduleConfig(enabled=True), digest_at="7:30")
        with (
            patch.object(scheduler_mod, "is_windows", return_value=True),
            patch.object(scheduler_mod, "create_windows_task", return_value=True) as create,
            patch.object(scheduler_mod, "get_executable_path", return_value=("pfs.exe", "")),
            patch("pfsentinel.services.notifications.register_event_source"),
        ):
            assert svc.apply_schedule() is True
        check = next(c for c in create.call_args_list if c.kwargs["schedule_type"] == "CHECK")
        assert check.kwargs["extra_times"] == ("07:30",)

    def test_schedule_cli_ignores_an_invalid_digest_time(self) -> None:
        from pfsentinel.cli.commands.schedule import scheduler_for

        config = AppConfig(
            notifications=NotificationConfig(digest_enabled=True, digest_time="nope")
        )
        assert scheduler_for(config)._digest_at is None
        config.notifications.digest_time = "06:45"
        assert scheduler_for(config)._digest_at == "06:45"
        config.notifications.digest_enabled = False
        assert scheduler_for(config)._digest_at is None
