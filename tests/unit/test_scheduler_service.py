"""Unit tests for SchedulerService and pure platform helpers.

Focuses on behavior the existing tests/unit/test_platform.py does not
cover: SchedulerService.apply_schedule / remove_schedule / get_status
branches, plus a few remaining platform edge cases (is_elevated on
non-Windows, _weekly_trigger_xml injection defense, _current_user_id,
query_windows_task on non-Windows).
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from pfsentinel.models.config import ScheduleConfig
from pfsentinel.services import scheduler as scheduler_mod
from pfsentinel.services.scheduler import SchedulerService
from pfsentinel.utils import platform as platform_mod
from pfsentinel.utils.platform import (
    _current_user_id,
    _weekly_trigger_xml,
    is_elevated,
    query_windows_task,
)

# ---------------------------------------------------------------------------
# SchedulerService.apply_schedule
# ---------------------------------------------------------------------------


class TestApplySchedule:
    def test_disabled_config_calls_remove_schedule(self):
        cfg = ScheduleConfig(enabled=False)
        svc = SchedulerService(cfg)
        with patch.object(svc, "remove_schedule", return_value=True) as mock_remove:
            result = svc.apply_schedule()
        assert result is True
        mock_remove.assert_called_once_with()

    def test_windows_both_daily_and_weekly_creates_two_tasks(self):
        cfg = ScheduleConfig(
            enabled=True,
            daily_enabled=True,
            daily_time="02:00",
            weekly_enabled=True,
            weekly_day="sunday",
            weekly_time="03:00",
            use_windows_task_scheduler=True,
        )
        svc = SchedulerService(cfg)
        with (
            patch.object(scheduler_mod, "is_windows", return_value=True),
            patch.object(scheduler_mod, "create_windows_task", return_value=True) as mock_create,
            patch.object(scheduler_mod, "get_executable_path", return_value=("pfs.exe", "")),
        ):
            result = svc.apply_schedule()

        assert result is True
        assert mock_create.call_count == 2
        schedule_types = [c.kwargs["schedule_type"] for c in mock_create.call_args_list]
        assert schedule_types == ["DAILY", "WEEKLY"]
        # weekly call should carry the configured day
        weekly_call = mock_create.call_args_list[1]
        assert weekly_call.kwargs["day_of_week"] == "sunday"
        assert weekly_call.kwargs["start_time"] == "03:00"
        # daily call should carry its own time
        daily_call = mock_create.call_args_list[0]
        assert daily_call.kwargs["start_time"] == "02:00"

    def test_windows_only_daily_creates_one_task(self):
        cfg = ScheduleConfig(
            enabled=True,
            daily_enabled=True,
            daily_time="04:00",
            weekly_enabled=False,
            use_windows_task_scheduler=True,
        )
        svc = SchedulerService(cfg)
        with (
            patch.object(scheduler_mod, "is_windows", return_value=True),
            patch.object(scheduler_mod, "create_windows_task", return_value=True) as mock_create,
            patch.object(scheduler_mod, "get_executable_path", return_value=("pfs.exe", "")),
        ):
            svc.apply_schedule()

        assert mock_create.call_count == 1
        assert mock_create.call_args.kwargs["schedule_type"] == "DAILY"

    def test_windows_only_weekly_creates_one_task(self):
        cfg = ScheduleConfig(
            enabled=True,
            daily_enabled=False,
            weekly_enabled=True,
            weekly_day="wednesday",
            weekly_time="05:30",
            use_windows_task_scheduler=True,
        )
        svc = SchedulerService(cfg)
        with (
            patch.object(scheduler_mod, "is_windows", return_value=True),
            patch.object(scheduler_mod, "create_windows_task", return_value=True) as mock_create,
            patch.object(scheduler_mod, "get_executable_path", return_value=("pfs.exe", "")),
        ):
            svc.apply_schedule()

        assert mock_create.call_count == 1
        assert mock_create.call_args.kwargs["schedule_type"] == "WEEKLY"
        assert mock_create.call_args.kwargs["day_of_week"] == "wednesday"

    def test_windows_task_creation_failure_returns_false(self):
        cfg = ScheduleConfig(
            enabled=True,
            daily_enabled=True,
            weekly_enabled=False,
            use_windows_task_scheduler=True,
        )
        svc = SchedulerService(cfg)
        with (
            patch.object(scheduler_mod, "is_windows", return_value=True),
            patch.object(scheduler_mod, "create_windows_task", return_value=False),
            patch.object(scheduler_mod, "get_executable_path", return_value=("pfs.exe", "")),
        ):
            result = svc.apply_schedule()
        assert result is False

    def test_non_windows_falls_back_to_in_process(self):
        cfg = ScheduleConfig(
            enabled=True,
            daily_enabled=True,
            weekly_enabled=False,
            use_windows_task_scheduler=True,
        )
        svc = SchedulerService(cfg)
        with (
            patch.object(scheduler_mod, "is_windows", return_value=False),
            patch.object(svc, "start_in_process", return_value=True) as mock_start,
        ):
            result = svc.apply_schedule()
        assert result is True
        mock_start.assert_called_once()


# ---------------------------------------------------------------------------
# SchedulerService.remove_schedule
# ---------------------------------------------------------------------------


class TestRemoveSchedule:
    def test_windows_path_deletes_both_tasks(self):
        cfg = ScheduleConfig(enabled=True)
        svc = SchedulerService(cfg)
        with (
            patch.object(scheduler_mod, "is_windows", return_value=True),
            patch.object(scheduler_mod, "delete_windows_task", return_value=True) as mock_delete,
        ):
            result = svc.remove_schedule()
        assert result is True
        assert mock_delete.call_count == 2
        called_names = [c.args[0] for c in mock_delete.call_args_list]
        assert "pfSentinel\\DailyBackup" in called_names
        assert "pfSentinel\\WeeklyBackup" in called_names

    def test_windows_delete_failure_returns_false(self):
        cfg = ScheduleConfig(enabled=True)
        svc = SchedulerService(cfg)
        with (
            patch.object(scheduler_mod, "is_windows", return_value=True),
            patch.object(scheduler_mod, "delete_windows_task", return_value=False),
        ):
            result = svc.remove_schedule()
        assert result is False

    def test_non_windows_still_stops_in_process(self):
        cfg = ScheduleConfig(enabled=True)
        svc = SchedulerService(cfg)
        with (
            patch.object(scheduler_mod, "is_windows", return_value=False),
            patch.object(scheduler_mod, "delete_windows_task") as mock_delete,
            patch.object(svc, "stop_in_process") as mock_stop,
        ):
            result = svc.remove_schedule()
        assert result is True
        mock_delete.assert_not_called()
        mock_stop.assert_called_once()


# ---------------------------------------------------------------------------
# SchedulerService.get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_non_windows_omits_windows_task_keys(self):
        cfg = ScheduleConfig(
            enabled=True,
            daily_enabled=True,
            weekly_enabled=True,
            use_windows_task_scheduler=True,
        )
        svc = SchedulerService(cfg)
        with patch.object(scheduler_mod, "is_windows", return_value=False):
            status = svc.get_status()
        assert "windows_daily" not in status
        assert "windows_weekly" not in status
        assert status["enabled"] is True
        assert status["daily_enabled"] is True

    def test_windows_populates_windows_task_keys(self):
        cfg = ScheduleConfig(
            enabled=True,
            daily_enabled=True,
            weekly_enabled=True,
            use_windows_task_scheduler=True,
        )
        svc = SchedulerService(cfg)

        daily_info = {"exists": True, "name": "Daily", "last_result": 0}
        weekly_info = {"exists": False}

        def fake_query(name: str):
            return daily_info if "Daily" in name else weekly_info

        with (
            patch.object(scheduler_mod, "is_windows", return_value=True),
            patch.object(scheduler_mod, "query_windows_task", side_effect=fake_query),
        ):
            status = svc.get_status()

        assert status["windows_daily"] == daily_info
        assert status["windows_weekly"] == weekly_info

    def test_windows_but_task_scheduler_disabled_omits_keys(self):
        cfg = ScheduleConfig(enabled=True, use_windows_task_scheduler=False)
        svc = SchedulerService(cfg)
        with patch.object(scheduler_mod, "is_windows", return_value=True):
            status = svc.get_status()
        assert "windows_daily" not in status


# ---------------------------------------------------------------------------
# platform.is_elevated
# ---------------------------------------------------------------------------


class TestIsElevated:
    def test_non_windows_always_returns_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert is_elevated() is True

    def test_macos_also_returns_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert is_elevated() is True

    def test_windows_ctypes_failure_returns_false(self, monkeypatch):
        monkeypatch.setattr(platform_mod, "is_windows", lambda: True)

        # Simulate ctypes import/lookup failure by patching import to raise
        import builtins

        real_import = builtins.__import__

        def raising_import(name, *args, **kwargs):
            if name == "ctypes":
                raise RuntimeError("no ctypes here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", raising_import)
        assert is_elevated() is False


# ---------------------------------------------------------------------------
# platform._weekly_trigger_xml (additional edge cases beyond test_platform.py)
# ---------------------------------------------------------------------------


class TestWeeklyTriggerXml:
    def test_valid_capitalized_day(self):
        xml = _weekly_trigger_xml("03:00", "Monday")
        assert "<Monday/>" in xml

    def test_case_insensitive_lowercase(self):
        xml = _weekly_trigger_xml("03:00", "monday")
        assert "<Monday/>" in xml

    def test_invalid_day_falls_back_to_sunday(self):
        xml = _weekly_trigger_xml("03:00", "notaday")
        assert "<Sunday/>" in xml
        assert "Notaday" not in xml

    def test_empty_string_falls_back_to_sunday(self):
        xml = _weekly_trigger_xml("03:00", "")
        assert "<Sunday/>" in xml

    def test_injection_attempt_falls_back_to_sunday(self):
        malicious = "Sunday><InjectedElement"
        xml = _weekly_trigger_xml("03:00", malicious)
        # Falls back to plain Sunday, not the injected variant.
        assert "<Sunday/>" in xml
        assert "InjectedElement" not in xml


# ---------------------------------------------------------------------------
# platform._current_user_id
# ---------------------------------------------------------------------------


class TestCurrentUserId:
    def test_with_userdomain(self, monkeypatch):
        monkeypatch.setenv("USERDOMAIN", "CORP")
        monkeypatch.setenv("USERNAME", "alice")
        assert _current_user_id() == "CORP\\alice"

    def test_without_userdomain(self, monkeypatch):
        monkeypatch.delenv("USERDOMAIN", raising=False)
        monkeypatch.setenv("USERNAME", "bob")
        assert _current_user_id() == "bob"

    def test_empty_userdomain_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("USERDOMAIN", "")
        monkeypatch.setenv("USERNAME", "carol")
        # Empty string is falsy so it takes the no-domain branch
        assert _current_user_id() == "carol"


# ---------------------------------------------------------------------------
# platform.query_windows_task (non-Windows smoke)
# ---------------------------------------------------------------------------


class TestQueryWindowsTaskNonWindows:
    def test_returns_exists_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert query_windows_task("anything") == {"exists": False}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
