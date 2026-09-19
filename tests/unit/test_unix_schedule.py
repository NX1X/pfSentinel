"""Tests for systemd user timer and cron scheduling (utils/unix_schedule.py).

systemctl and crontab are replaced with in-memory fakes, so nothing here
touches the real user session or crontab.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pfsentinel.utils import unix_schedule as us

ARGV = ["/opt/venv/bin/python", "-m", "pfsentinel", "backup", "run"]


# ---------------------------------------------------------------------------
# Validation and rendering
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("value", ["02:00", "0:05", "23:59", " 7:30 "])
    def test_valid_times(self, value: str) -> None:
        us.validate_spec(value)

    @pytest.mark.parametrize(
        "value",
        ["24:00", "12:60", "noon", "", "12", "-1:00", "02:00\nExecStartPre=/bin/sh", "1:2:3"],
    )
    def test_invalid_times(self, value: str) -> None:
        with pytest.raises(us.ScheduleSpecError):
            us.validate_spec(value)

    def test_weekday_case_insensitive(self) -> None:
        us.validate_spec("03:00", "FrIdAy")

    @pytest.mark.parametrize("day", ["someday", "", "sun; rm -rf ~"])
    def test_invalid_weekday(self, day: str) -> None:
        with pytest.raises(us.ScheduleSpecError):
            us.validate_spec("03:00", day)


class TestSystemdRendering:
    def test_on_calendar_values(self) -> None:
        assert us.daily_on_calendar("2:05") == "*-*-* 02:05:00"
        assert us.weekly_on_calendar("sunday", "03:00") == "Sun *-*-* 03:00:00"

    def test_timer_is_persistent_and_targets_the_service(self) -> None:
        body = us.render_timer("pfSentinel daily backup", "*-*-* 02:00:00")
        assert "OnCalendar=*-*-* 02:00:00" in body
        assert "Persistent=true" in body  # missed runs fire at next boot
        assert f"Unit={us.SERVICE_UNIT}" in body
        assert "WantedBy=timers.target" in body

    def test_service_execstart_plain_args(self) -> None:
        body = us.render_service(ARGV)
        assert "Type=oneshot" in body
        assert "ExecStart=/opt/venv/bin/python -m pfsentinel backup run" in body

    def test_service_quotes_spaces_and_escapes_specifiers(self) -> None:
        body = us.render_service(["/home/a b/py%thon", "-m", "pfsentinel"])
        assert 'ExecStart="/home/a b/py%%thon" -m pfsentinel' in body

    @pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
    def test_on_calendar_accepted_by_systemd(self) -> None:
        for spec in (us.daily_on_calendar("02:00"), us.weekly_on_calendar("monday", "04:30")):
            result = subprocess.run(
                ["systemd-analyze", "calendar", spec], capture_output=True, text=True
            )
            assert result.returncode == 0, result.stderr


class TestCronRendering:
    def test_daily_and_weekly_lines(self, tmp_path: Path) -> None:
        lines = us.cron_lines(ARGV, "02:05", ("sunday", "03:00"), tmp_path / "s.log")
        assert len(lines) == 2
        assert lines[0].startswith("5 2 * * * /opt/venv/bin/python -m pfsentinel backup run >> ")
        assert lines[1].startswith("0 3 * * 0 ")
        assert all(us.CRON_MARKER in ln for ln in lines)

    def test_percent_is_escaped(self, tmp_path: Path) -> None:
        (line,) = us.cron_lines(["/x/py%thon"], "01:00", None, tmp_path / "s.log")
        assert "py\\%thon" in line

    def test_paths_with_spaces_are_quoted(self, tmp_path: Path) -> None:
        (line,) = us.cron_lines(["/a b/python"], "01:00", None, tmp_path / "s.log")
        assert "'/a b/python'" in line


# ---------------------------------------------------------------------------
# cron install / remove against a fake crontab
# ---------------------------------------------------------------------------


class FakeCrontab:
    def __init__(self, lines: list[str] | None) -> None:
        self.lines = lines  # None = user has no crontab yet

    def run(self, args: list[str], input: str | None = None, **_: object):
        if args == ["crontab", "-l"]:
            if self.lines is None:
                return subprocess.CompletedProcess(args, 1, "", "no crontab for user\n")
            return subprocess.CompletedProcess(args, 0, "\n".join(self.lines) + "\n", "")
        if args == ["crontab", "-"]:
            self.lines = (input or "").splitlines()
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command {args}")


@pytest.fixture
def fake_cron(monkeypatch: pytest.MonkeyPatch):
    def install(lines: list[str] | None) -> FakeCrontab:
        fake = FakeCrontab(lines)
        monkeypatch.setattr(us.subprocess, "run", fake.run)
        monkeypatch.setattr(us.shutil, "which", lambda name: f"/usr/bin/{name}")
        return fake

    return install


class TestCron:
    def test_install_into_empty_crontab(self, fake_cron, tmp_path: Path) -> None:
        fake = fake_cron(None)
        assert us.install_cron(ARGV, "02:00", None, tmp_path / "logs" / "s.log")
        assert len(fake.lines) == 1
        assert (tmp_path / "logs").is_dir()

    def test_install_keeps_other_lines_and_replaces_ours(self, fake_cron, tmp_path) -> None:
        fake = fake_cron(["MAILTO=me", "0 * * * * other-job", f"1 1 * * * old {us.CRON_MARKER}"])
        assert us.install_cron(ARGV, "02:00", ("friday", "04:00"), tmp_path / "s.log")
        assert fake.lines[:2] == ["MAILTO=me", "0 * * * * other-job"]
        ours = [ln for ln in fake.lines if us.CRON_MARKER in ln]
        assert len(ours) == 2
        assert not any(ln.startswith("1 1 ") for ln in ours)

    def test_unreadable_crontab_is_not_overwritten(self, monkeypatch, tmp_path) -> None:
        calls: list[list[str]] = []

        def run(args, **_):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "permission denied")

        monkeypatch.setattr(us.subprocess, "run", run)
        assert us.install_cron(ARGV, "02:00", None, tmp_path / "s.log") is False
        assert ["crontab", "-"] not in calls

    def test_remove_only_touches_our_lines(self, fake_cron) -> None:
        fake = fake_cron(["0 * * * * other-job", f"0 2 * * * x {us.CRON_MARKER} daily"])
        assert us.remove_cron()
        assert fake.lines == ["0 * * * * other-job"]
        assert us.query_cron() == []

    def test_remove_without_crontab_is_ok(self, fake_cron) -> None:
        fake_cron(None)
        assert us.remove_cron()


# ---------------------------------------------------------------------------
# systemd install / remove against a fake systemctl
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_systemd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    calls: list[tuple[str, ...]] = []

    def systemctl(*args: str):
        calls.append(args)
        return subprocess.CompletedProcess(["systemctl", "--user", *args], 0, "", "")

    monkeypatch.setattr(us, "_systemctl", systemctl)
    monkeypatch.setattr(us.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


class TestSystemd:
    def test_install_writes_units_and_enables_timers(self, fake_systemd) -> None:
        assert us.install_systemd_timers(ARGV, "02:00", ("sunday", "03:00"))
        unit_dir = us.systemd_user_dir()
        assert (unit_dir / us.SERVICE_UNIT).exists()
        assert "OnCalendar=Sun *-*-* 03:00:00" in (unit_dir / us.WEEKLY_TIMER).read_text()
        assert ("daemon-reload",) in fake_systemd
        assert ("enable", "--now", us.DAILY_TIMER) in fake_systemd
        assert ("enable", "--now", us.WEEKLY_TIMER) in fake_systemd

    def test_reinstall_without_weekly_removes_weekly_timer(self, fake_systemd) -> None:
        us.install_systemd_timers(ARGV, "02:00", ("sunday", "03:00"))
        fake_systemd.clear()
        assert us.install_systemd_timers(ARGV, "02:00", None)
        assert not (us.systemd_user_dir() / us.WEEKLY_TIMER).exists()
        assert ("disable", "--now", us.WEEKLY_TIMER) in fake_systemd

    def test_enable_failure_reported(self, fake_systemd, monkeypatch) -> None:
        def systemctl(*args: str):
            rc = 1 if args[:1] == ("enable",) else 0
            return subprocess.CompletedProcess(args, rc, "", "boom")

        monkeypatch.setattr(us, "_systemctl", systemctl)
        assert us.install_systemd_timers(ARGV, "02:00", None) is False

    def test_nothing_requested_installs_nothing(self, fake_systemd) -> None:
        assert us.install_systemd_timers(ARGV, None, None) is False
        assert not us.systemd_user_dir().exists()

    def test_remove_deletes_everything(self, fake_systemd) -> None:
        us.install_systemd_timers(ARGV, "02:00", ("monday", "03:00"))
        assert us.systemd_units_installed()
        assert us.remove_systemd_timers()
        assert not us.systemd_units_installed()
        assert not (us.systemd_user_dir() / us.SERVICE_UNIT).exists()

    def test_remove_when_not_installed_is_noop(self, fake_systemd) -> None:
        assert us.remove_systemd_timers()
        assert fake_systemd == []

    def test_query_parses_state(self, fake_systemd, monkeypatch) -> None:
        us.install_systemd_timers(ARGV, "02:00", None)

        def systemctl(*args: str):
            out = (
                "ActiveState=active\nNextElapseUSecRealtime=Mon 2026-09-21 02:00:00 IDT\n"
                if us.DAILY_TIMER in args
                else "Result=exit-code\nExecMainStatus=1\n"
            )
            return subprocess.CompletedProcess(args, 0, out, "")

        monkeypatch.setattr(us, "_systemctl", systemctl)
        info = us.query_systemd_timer(us.DAILY_TIMER)
        assert info["active"] is True
        assert info["next_run"].startswith("Mon 2026-09-21")
        assert info["last_result"] == "exit-code"
        assert us.query_systemd_timer(us.WEEKLY_TIMER) == {"exists": False}
