"""Tests for platform detection and OS-specific helpers."""

from __future__ import annotations

import sys

from pfsentinel.utils import platform as platform_mod
from pfsentinel.utils.platform import (
    _current_user_id,
    _daily_trigger_xml,
    _weekly_trigger_xml,
    app_config_dir,
    create_windows_task,
    default_backup_dir,
    delete_windows_task,
    get_executable_path,
    is_linux,
    is_macos,
    is_windows,
    query_windows_task,
    run_command,
)


class TestPlatformDetection:
    def test_is_windows_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert is_windows() is True

    def test_is_windows_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert is_windows() is False

    def test_is_linux_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert is_linux() is True

    def test_is_linux_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert is_linux() is False

    def test_is_macos_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert is_macos() is True

    def test_is_macos_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert is_macos() is False


class TestPaths:
    def test_app_config_dir(self):
        d = app_config_dir()
        assert d.name == ".pfsentinel"

    def test_default_backup_dir_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        d = default_backup_dir()
        assert "Documents" in str(d)
        assert "pfSentinel" in str(d)

    def test_default_backup_dir_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        d = default_backup_dir()
        assert "pfSentinel" in str(d)
        assert "Documents" not in str(d)


import pytest


class TestRunCommand:
    @pytest.mark.skipif(sys.platform == "win32", reason="echo not on Windows")
    def test_success(self):
        # echo is available on both Linux and Windows (Git Bash)
        result = run_command(["echo", "hello"], check=True)
        assert result.returncode == 0
        assert "hello" in result.stdout


class TestGetExecutablePath:
    def test_frozen_returns_sys_executable(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", "/usr/local/bin/pfs")
        assert get_executable_path() == ("/usr/local/bin/pfs", "")

    def test_normal_returns_module_cmd(self, monkeypatch):
        # Ensure 'frozen' is not set
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen")
        executable, prefix = get_executable_path()
        assert executable == sys.executable
        assert prefix == "-m pfsentinel"


class TestWindowsTaskNonWindows:
    """On non-Windows, these should return False/{exists: False}."""

    def test_create_task(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert create_windows_task("test", "pfs", "backup", "DAILY", "02:00") is False

    def test_delete_task(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert delete_windows_task("test") is False

    def test_query_task(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        result = query_windows_task("test")
        assert result["exists"] is False


class TestTaskXmlBuilders:
    def test_daily_trigger_contains_time_and_interval(self):
        xml = _daily_trigger_xml("02:00")
        assert "<StartBoundary>2025-01-01T02:00:00</StartBoundary>" in xml
        assert "<DaysInterval>1</DaysInterval>" in xml
        assert "<ScheduleByDay>" in xml

    def test_weekly_trigger_contains_day(self):
        xml = _weekly_trigger_xml("03:00", "sunday")
        assert "<Sunday/>" in xml
        assert "<StartBoundary>2025-01-01T03:00:00</StartBoundary>" in xml
        assert "<WeeksInterval>1</WeeksInterval>" in xml

    def test_weekly_trigger_defaults_to_sunday(self):
        xml = _weekly_trigger_xml("03:00", "")
        assert "<Sunday/>" in xml

    def test_current_user_id_uses_env(self, monkeypatch):
        monkeypatch.setenv("USERDOMAIN", "TESTHOST")
        monkeypatch.setenv("USERNAME", "tester")
        assert _current_user_id() == "TESTHOST\\tester"

    def test_current_user_id_falls_back_without_domain(self, monkeypatch):
        monkeypatch.delenv("USERDOMAIN", raising=False)
        monkeypatch.setenv("USERNAME", "tester")
        assert _current_user_id() == "tester"


class TestCreateWindowsTaskXml:
    """Verify the XML payload sent to schtasks has the desired flags."""

    def test_xml_contains_s4u_and_battery_settings(self, monkeypatch):
        monkeypatch.setattr(platform_mod, "is_windows", lambda: True)

        captured: dict[str, str] = {}

        class FakeResult:
            returncode = 0
            stderr = ""
            stdout = ""

        def fake_run_command(args, check=False):
            xml_path = args[args.index("/XML") + 1]
            with open(xml_path, "rb") as f:
                captured["xml"] = f.read().decode("utf-16")
            return FakeResult()

        monkeypatch.setattr(platform_mod, "run_command", fake_run_command)

        ok = create_windows_task(
            task_name="pfSentinel\\DailyBackup",
            executable=r"C:\Python\python.exe",
            args="-m pfsentinel backup run",
            schedule_type="DAILY",
            start_time="02:00",
        )
        assert ok is True

        xml = captured["xml"]
        assert "<LogonType>S4U</LogonType>" in xml
        assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
        assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
        assert "<WakeToRun>true</WakeToRun>" in xml
        assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
        assert "<Command>C:\\Python\\python.exe</Command>" in xml
        assert "<Arguments>-m pfsentinel backup run</Arguments>" in xml
        assert "<DaysInterval>1</DaysInterval>" in xml

    def test_weekly_xml_uses_weekday(self, monkeypatch):
        monkeypatch.setattr(platform_mod, "is_windows", lambda: True)

        captured: dict[str, str] = {}

        class FakeResult:
            returncode = 0
            stderr = ""
            stdout = ""

        def fake_run_command(args, check=False):
            xml_path = args[args.index("/XML") + 1]
            with open(xml_path, "rb") as f:
                captured["xml"] = f.read().decode("utf-16")
            return FakeResult()

        monkeypatch.setattr(platform_mod, "run_command", fake_run_command)

        ok = create_windows_task(
            task_name="pfSentinel\\WeeklyBackup",
            executable=r"C:\Python\python.exe",
            args="-m pfsentinel backup run",
            schedule_type="WEEKLY",
            start_time="03:00",
            day_of_week="sunday",
        )
        assert ok is True
        assert "<Sunday/>" in captured["xml"]
        assert "<WeeksInterval>1</WeeksInterval>" in captured["xml"]

    def test_invalid_schedule_type_returns_false(self, monkeypatch):
        monkeypatch.setattr(platform_mod, "is_windows", lambda: True)
        assert create_windows_task("test", "pfs", "backup", "MONTHLY", "02:00") is False
