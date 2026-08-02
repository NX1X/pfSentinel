"""Unit tests for SchedulerService and pure platform helpers.

Focuses on behavior the existing tests/unit/test_platform.py does not
cover: SchedulerService.apply_schedule / remove_schedule / get_status
branches, plus a few remaining platform edge cases (is_elevated on
non-Windows, _weekly_trigger_xml injection defense, _current_user_id,
query_windows_task on non-Windows).
"""

from __future__ import annotations

import sys
from datetime import datetime
from datetime import time as dt_time
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


class TestScheduleTimeArithmetic:
    """The stdlib replacement for the abandoned `schedule` package.

    These were untestable before - `schedule` owned the clock internally.
    """

    def test_parse_hhmm_valid(self):
        from pfsentinel.services.scheduler import parse_hhmm

        assert parse_hhmm("03:00") == dt_time(3, 0)
        assert parse_hhmm("23:59") == dt_time(23, 59)
        assert parse_hhmm(" 07:05 ") == dt_time(7, 5)

    @pytest.mark.parametrize("bad", ["24:00", "12:60", "-1:00", "abc", "", "12"])
    def test_parse_hhmm_rejects_garbage(self, bad):
        from pfsentinel.services.scheduler import parse_hhmm

        with pytest.raises(ValueError):
            parse_hhmm(bad)

    def test_daily_later_today(self):
        from pfsentinel.services.scheduler import next_daily_run

        now = datetime(2026, 8, 2, 1, 0)
        assert next_daily_run(now, "03:00") == datetime(2026, 8, 2, 3, 0)

    def test_daily_rolls_to_tomorrow_when_passed(self):
        from pfsentinel.services.scheduler import next_daily_run

        now = datetime(2026, 8, 2, 5, 0)
        assert next_daily_run(now, "03:00") == datetime(2026, 8, 3, 3, 0)

    def test_daily_exactly_now_rolls_forward(self):
        """A slot equal to now must not fire twice."""
        from pfsentinel.services.scheduler import next_daily_run

        now = datetime(2026, 8, 2, 3, 0, 0)
        assert next_daily_run(now, "03:00") == datetime(2026, 8, 3, 3, 0)

    def test_weekly_later_this_week(self):
        from pfsentinel.services.scheduler import next_weekly_run

        now = datetime(2026, 8, 2, 1, 0)  # Sunday
        assert next_weekly_run(now, "wednesday", "04:00") == datetime(2026, 8, 5, 4, 0)

    def test_weekly_same_day_later_today(self):
        from pfsentinel.services.scheduler import next_weekly_run

        now = datetime(2026, 8, 2, 1, 0)  # Sunday
        assert next_weekly_run(now, "sunday", "04:00") == datetime(2026, 8, 2, 4, 0)

    def test_weekly_same_day_already_passed_rolls_a_week(self):
        from pfsentinel.services.scheduler import next_weekly_run

        now = datetime(2026, 8, 2, 6, 0)  # Sunday, past 04:00
        assert next_weekly_run(now, "sunday", "04:00") == datetime(2026, 8, 9, 4, 0)

    def test_weekly_unknown_day_returns_none(self):
        from pfsentinel.services.scheduler import next_weekly_run

        assert next_weekly_run(datetime(2026, 8, 2), "notaday", "04:00") is None

    def test_weekly_day_is_case_insensitive(self):
        from pfsentinel.services.scheduler import next_weekly_run

        now = datetime(2026, 8, 2, 1, 0)
        assert next_weekly_run(now, "SuNdAy", "04:00") == datetime(2026, 8, 2, 4, 0)

    def test_next_run_picks_the_earlier_of_daily_and_weekly(self):
        cfg = ScheduleConfig(
            enabled=True,
            daily_enabled=True,
            daily_time="03:00",
            weekly_enabled=True,
            weekly_day="sunday",
            weekly_time="04:00",
        )
        svc = SchedulerService(cfg)
        now = datetime(2026, 8, 2, 1, 0)  # Sunday; daily 03:00 beats weekly 04:00
        assert svc.next_run_after(now) == datetime(2026, 8, 2, 3, 0)

    def test_next_run_none_when_nothing_enabled(self):
        cfg = ScheduleConfig(enabled=True, daily_enabled=False, weekly_enabled=False)
        assert SchedulerService(cfg).next_run_after(datetime(2026, 8, 2)) is None


class TestInProcessLifecycle:
    def test_start_refuses_when_nothing_enabled(self):
        cfg = ScheduleConfig(enabled=True, daily_enabled=False, weekly_enabled=False)
        assert SchedulerService(cfg).start_in_process() is False

    def test_stop_interrupts_promptly(self):
        """stop_in_process must not wait for the next scheduled slot."""
        import time as _time

        cfg = ScheduleConfig(
            enabled=True, daily_enabled=True, daily_time="03:00", weekly_enabled=False
        )
        svc = SchedulerService(cfg)
        assert svc.start_in_process() is True

        started = _time.monotonic()
        svc.stop_in_process()
        elapsed = _time.monotonic() - started

        assert elapsed < 2.0, f"stop took {elapsed:.1f}s; it should be near-instant"
        assert svc._thread is None

    def test_double_start_is_idempotent(self):
        cfg = ScheduleConfig(
            enabled=True, daily_enabled=True, daily_time="03:00", weekly_enabled=False
        )
        svc = SchedulerService(cfg)
        try:
            assert svc.start_in_process() is True
            assert svc.start_in_process() is True
        finally:
            svc.stop_in_process()
