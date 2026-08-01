"""Unit tests for schedule CLI helpers and the `schedule enable` command.

Covers:
- The pure `_format_windows_task` renderer across all its result-code branches.
- The `schedule enable` command via CliRunner, including the elevation gate
  on Windows, the successful non-Windows in-process path, and the Windows
  registration-failure remediation path.

AppConfig.config_path() is redirected to a tmp file via monkeypatch to keep
tests hermetic (the root conftest already replaces the keyring with an
in-memory store).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.commands import schedule as schedule_cmd
from pfsentinel.cli.commands.schedule import _format_windows_task
from pfsentinel.cli.commands.schedule import app as schedule_app
from pfsentinel.models.config import AppConfig

# ---------------------------------------------------------------------------
# _format_windows_task pure renderer
# ---------------------------------------------------------------------------


class TestFormatWindowsTask:
    def test_missing_task_returns_not_registered(self):
        lines = _format_windows_task("Windows task (daily)", {})
        assert lines == ["[bold]Windows task (daily):[/] [yellow]Not registered[/]"]

    def test_exists_false_returns_not_registered(self):
        lines = _format_windows_task("Daily", {"exists": False})
        assert len(lines) == 1
        assert "Not registered" in lines[0]

    def test_exists_true_last_result_zero_shows_ok(self):
        lines = _format_windows_task("Daily", {"exists": True, "last_result": 0})
        joined = "\n".join(lines)
        assert "Registered" in joined
        assert "OK" in joined
        assert "FAILED" not in joined

    def test_error_invalid_parameter_shows_remediation(self):
        # 0x80070057 as a signed 32-bit int is -2147024809
        lines = _format_windows_task(
            "Daily",
            {"exists": True, "last_result": -2147024809},
        )
        joined = "\n".join(lines)
        assert "FAILED 0x80070057" in joined
        assert "ERROR_INVALID_PARAMETER" in joined
        # remediation hint present on a separate line
        assert any("re-run" in ln.lower() or "administrator" in ln.lower() for ln in lines)

    def test_other_high_bit_failure_shows_hex_code_no_remediation(self):
        # 0x80070005 (Access denied) as signed int
        lines = _format_windows_task(
            "Daily",
            {"exists": True, "last_result": -2147024891},
        )
        joined = "\n".join(lines)
        assert "FAILED 0x80070005" in joined
        assert "ERROR_INVALID_PARAMETER" not in joined

    def test_last_result_none_omits_result_line(self):
        lines = _format_windows_task(
            "Daily",
            {"exists": True, "last_result": None},
        )
        joined = "\n".join(lines)
        assert "Registered" in joined
        assert "Last result" not in joined

    def test_positive_last_result_is_informational(self):
        # 267009 is a non-negative status code; not a failure
        lines = _format_windows_task("Daily", {"exists": True, "last_result": 267009})
        joined = "\n".join(lines)
        assert "informational" in joined
        assert "267009" in joined
        assert "FAILED" not in joined

    def test_includes_next_run_and_last_run_when_present(self):
        lines = _format_windows_task(
            "Daily",
            {
                "exists": True,
                "next_run": "2026-08-02 02:00:00",
                "last_run": "2026-08-01 02:00:00",
                "last_result": 0,
            },
        )
        joined = "\n".join(lines)
        assert "Next run: 2026-08-02 02:00:00" in joined
        assert "Last run: 2026-08-01 02:00:00" in joined

    def test_omits_next_and_last_run_when_absent(self):
        lines = _format_windows_task("Daily", {"exists": True, "last_result": 0})
        joined = "\n".join(lines)
        assert "Next run" not in joined
        assert "Last run" not in joined


# ---------------------------------------------------------------------------
# schedule enable CLI command
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def tmp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect AppConfig.config_path() into a tmp directory for hermetic runs."""
    pfs_dir = tmp_path / ".pfsentinel"
    pfs_dir.mkdir(parents=True)
    config_path = pfs_dir / "config.json"
    monkeypatch.setattr(AppConfig, "config_path", staticmethod(lambda: config_path))
    return config_path


class TestScheduleEnableCommand:
    def test_windows_without_admin_exits_and_does_not_persist(
        self,
        cli_runner: CliRunner,
        tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Simulate Windows without admin - should refuse and leave config untouched.
        import pfsentinel.utils.platform as platform_mod

        monkeypatch.setattr(platform_mod, "is_windows", lambda: True)
        monkeypatch.setattr(platform_mod, "is_elevated", lambda: False)

        result = cli_runner.invoke(schedule_app, ["enable"])

        assert result.exit_code == 1
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Administrator privileges" in combined
        # No config file should have been written
        assert not tmp_config.exists()

    def test_non_windows_success_persists_config(
        self,
        cli_runner: CliRunner,
        tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # On non-Windows, apply_schedule uses the in-process backend. We patch
        # start_in_process to avoid needing the `schedule` package or spawning
        # a real thread.
        import pfsentinel.utils.platform as platform_mod

        monkeypatch.setattr(platform_mod, "is_windows", lambda: False)
        monkeypatch.setattr(platform_mod, "is_elevated", lambda: True)
        # Also patch the module-level is_windows used by SchedulerService.
        monkeypatch.setattr(schedule_cmd, "SchedulerService", schedule_cmd.SchedulerService)
        from pfsentinel.services import scheduler as scheduler_mod

        monkeypatch.setattr(scheduler_mod, "is_windows", lambda: False)
        monkeypatch.setattr(scheduler_mod.SchedulerService, "start_in_process", lambda self: True)

        result = cli_runner.invoke(
            schedule_app,
            [
                "enable",
                "--daily-time",
                "04:15",
                "--weekly-day",
                "monday",
                "--weekly-time",
                "05:30",
            ],
        )

        assert result.exit_code == 0, result.stdout
        # Config should have been written with the flags we passed.
        assert tmp_config.exists()
        cfg = AppConfig.load()
        assert cfg.schedule.enabled is True
        assert cfg.schedule.daily_enabled is True
        assert cfg.schedule.daily_time == "04:15"
        assert cfg.schedule.weekly_enabled is True
        assert cfg.schedule.weekly_day == "monday"
        assert cfg.schedule.weekly_time == "05:30"
        # In-process mode message should surface on the non-Windows happy path.
        assert "in-process" in result.stdout

    def test_non_windows_no_weekly_flag_disables_weekly(
        self,
        cli_runner: CliRunner,
        tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import pfsentinel.utils.platform as platform_mod
        from pfsentinel.services import scheduler as scheduler_mod

        monkeypatch.setattr(platform_mod, "is_windows", lambda: False)
        monkeypatch.setattr(platform_mod, "is_elevated", lambda: True)
        monkeypatch.setattr(scheduler_mod, "is_windows", lambda: False)
        monkeypatch.setattr(scheduler_mod.SchedulerService, "start_in_process", lambda self: True)

        result = cli_runner.invoke(schedule_app, ["enable", "--no-weekly"])

        assert result.exit_code == 0, result.stdout
        cfg = AppConfig.load()
        assert cfg.schedule.weekly_enabled is False

    def test_windows_apply_failure_shows_remediation_and_exits(
        self,
        cli_runner: CliRunner,
        tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Elevated Windows shell, but Task Scheduler registration fails.
        import pfsentinel.utils.platform as platform_mod
        from pfsentinel.services import scheduler as scheduler_mod

        monkeypatch.setattr(platform_mod, "is_windows", lambda: True)
        monkeypatch.setattr(platform_mod, "is_elevated", lambda: True)
        monkeypatch.setattr(scheduler_mod, "is_windows", lambda: True)
        monkeypatch.setattr(scheduler_mod.SchedulerService, "apply_schedule", lambda self: False)

        result = cli_runner.invoke(schedule_app, ["enable"])

        assert result.exit_code == 1
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Failed to register Windows Task Scheduler" in combined
        # remediation hints should be present
        assert "Administrator" in combined or "batch job" in combined

    def test_non_windows_start_in_process_failure_exits_with_hint(
        self,
        cli_runner: CliRunner,
        tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import pfsentinel.utils.platform as platform_mod
        from pfsentinel.services import scheduler as scheduler_mod

        monkeypatch.setattr(platform_mod, "is_windows", lambda: False)
        monkeypatch.setattr(platform_mod, "is_elevated", lambda: True)
        monkeypatch.setattr(scheduler_mod, "is_windows", lambda: False)
        monkeypatch.setattr(scheduler_mod.SchedulerService, "start_in_process", lambda self: False)

        result = cli_runner.invoke(schedule_app, ["enable"])
        assert result.exit_code == 1
        combined = (result.stdout or "") + (result.stderr or "")
        assert "in-process scheduler" in combined
        assert "schedule" in combined  # tells user to install the package


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
