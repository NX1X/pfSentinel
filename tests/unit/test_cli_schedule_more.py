"""More `pfs schedule` CLI tests: status, disable, run-now, backend notes.

The OS scheduler is the edge: systemctl, crontab and schtasks are never
called. Their wrappers in utils.unix_schedule and utils.platform are
replaced per test; SchedulerService and the CLI run for real.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services import scheduler as scheduler_mod
from pfsentinel.utils import unix_schedule


def _out(result) -> str:
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


@dataclass
class FakeUnixScheduler:
    """Stand-in for systemd --user and crontab."""

    systemd: bool = True
    cron: bool = True
    install_ok: bool = True
    linger: bool | None = True
    timers: dict[str, dict] = field(default_factory=dict)
    cron_entries: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        us = unix_schedule
        monkeypatch.setattr(us, "systemd_user_available", lambda: self.systemd)
        monkeypatch.setattr(us, "cron_available", lambda: self.cron)
        monkeypatch.setattr(us, "install_systemd_timers", lambda *a, **k: self.install_ok)
        monkeypatch.setattr(us, "install_cron", lambda *a, **k: self.install_ok)
        monkeypatch.setattr(us, "linger_enabled", lambda: self.linger)
        monkeypatch.setattr(us, "query_systemd_timer", lambda name: self.timers.get(name, {}))
        monkeypatch.setattr(us, "query_cron", lambda: list(self.cron_entries))

        def remove(kind: str):
            def _remove() -> bool:
                self.removed.append(kind)
                return True

            return _remove

        monkeypatch.setattr(us, "remove_systemd_timers", remove("systemd"))
        monkeypatch.setattr(us, "remove_cron", remove("cron"))


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> FakeUnixScheduler:
    monkeypatch.setattr("pfsentinel.utils.platform.is_windows", lambda: False)
    monkeypatch.setattr(scheduler_mod, "is_windows", lambda: False)
    fake = FakeUnixScheduler()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    """Pretend to be an elevated Windows shell; returns the task table."""
    tasks: dict[str, dict] = {}
    monkeypatch.setattr("pfsentinel.utils.platform.is_windows", lambda: True)
    monkeypatch.setattr("pfsentinel.utils.platform.is_elevated", lambda: True)
    monkeypatch.setattr(scheduler_mod, "is_windows", lambda: True)
    monkeypatch.setattr(scheduler_mod, "query_windows_task", lambda name: tasks.get(name, {}))
    return tasks


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _enable_in_config() -> None:
    config = AppConfig.load()
    config.schedule.enabled = True
    config.save()


# ---------------------------------------------------------------------------
# enable: backend notes and backend failures
# ---------------------------------------------------------------------------


class TestEnableOnLinux:
    def test_systemd_success_names_the_timers(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        result = cli_runner.invoke(app, ["schedule", "enable"])

        assert result.exit_code == 0, _out(result)
        assert "systemd user timers" in _out(result)
        assert AppConfig.load().schedule.enabled is True

    def test_systemd_success_removes_a_stale_cron_copy(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        cli_runner.invoke(app, ["schedule", "enable"])

        assert "cron" in linux.removed

    def test_systemd_without_linger_explains_logged_out_runs(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        linux.linger = False
        result = cli_runner.invoke(app, ["schedule", "enable"])

        assert "loginctl enable-linger" in _out(result)

    def test_desktop_keyring_warning_is_shown(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        # The root conftest keyring is a (fake) OS keyring, which is locked until login.
        result = cli_runner.invoke(app, ["schedule", "enable"])

        assert "desktop keyring" in _out(result)

    def test_file_store_needs_no_keyring_warning(
        self,
        cli_runner: CliRunner,
        saved_config: AppConfig,
        linux: FakeUnixScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("pfsentinel.services.credentials._KEYRING_AVAILABLE", False)
        result = cli_runner.invoke(app, ["schedule", "enable"])

        assert result.exit_code == 0, _out(result)
        assert "desktop keyring" not in _out(result)

    def test_cron_success_shows_the_log_file(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        linux.systemd = False
        result = cli_runner.invoke(app, ["schedule", "enable"])

        assert result.exit_code == 0, _out(result)
        assert "Mode: cron" in _out(result)
        assert "scheduled.log" in _out(result)

    def test_systemd_install_failure_points_at_systemctl(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        linux.install_ok = False
        result = cli_runner.invoke(app, ["schedule", "enable"])

        assert result.exit_code == 1
        assert "systemctl --user status" in _out(result)

    def test_cron_install_failure_points_at_crontab(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        linux.systemd = False
        linux.install_ok = False
        result = cli_runner.invoke(app, ["schedule", "enable"])

        assert result.exit_code == 1
        assert "crontab -l" in _out(result)

    def test_in_process_mode_says_it_stops_with_the_process(
        self,
        cli_runner: CliRunner,
        saved_config: AppConfig,
        linux: FakeUnixScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(scheduler_mod.SchedulerService, "start_in_process", lambda self: True)
        result = cli_runner.invoke(app, ["schedule", "enable", "--no-task-scheduler"])

        assert result.exit_code == 0, _out(result)
        assert "in-process" in _out(result)


def test_enable_on_windows_describes_task_scheduler(
    cli_runner: CliRunner,
    saved_config: AppConfig,
    windows: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def registered(self) -> bool:
        # Stands in for schtasks; the real method would shell out.
        self.backend = "windows-task"
        return True

    monkeypatch.setattr(scheduler_mod.SchedulerService, "apply_schedule", registered)
    result = cli_runner.invoke(app, ["schedule", "enable"])

    assert result.exit_code == 0, _out(result)
    assert "Windows Task Scheduler" in _out(result)


# ---------------------------------------------------------------------------
# disable
# ---------------------------------------------------------------------------


def test_disable_turns_the_schedule_off_and_removes_the_os_entries(
    cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
) -> None:
    _enable_in_config()
    result = cli_runner.invoke(app, ["schedule", "disable"])

    assert result.exit_code == 0, _out(result)
    assert AppConfig.load().schedule.enabled is False
    assert sorted(linux.removed) == ["cron", "systemd"]


def test_disable_fails_when_a_task_cannot_be_removed(
    cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
) -> None:
    """Before rc.3 it said "disabled" while the tasks kept running."""
    _enable_in_config()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scheduler_mod.SchedulerService, "remove_schedule", lambda self: False)
        result = cli_runner.invoke(app, ["schedule", "disable"])

    assert result.exit_code == 1
    assert "could not be removed" in _out(result)
    assert "Scheduling disabled" not in _out(result)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatusOnLinux:
    def test_shows_the_configured_times(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        config = AppConfig.load()
        config.schedule.daily_time = "04:30"
        config.save()
        result = cli_runner.invoke(app, ["schedule", "status"])

        assert result.exit_code == 0, _out(result)
        assert "04:30" in _out(result)

    def test_shows_what_a_scheduled_run_backs_up(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        result = cli_runner.invoke(app, ["schedule", "status"])
        assert "Backs up: config" in _out(result)
        assert "pfs config set extras" in _out(result)

        config = AppConfig.load()
        config.backup_policy.extras.set_targets(["rrd", "certs"])
        config.save()
        result = cli_runner.invoke(app, ["schedule", "status"])
        assert "Backs up: config, rrd, certs" in _out(result)
        assert "pfs config set extras" not in _out(result)

    def test_shows_active_timers_and_their_last_result(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        _enable_in_config()
        linux.timers[unix_schedule.DAILY_TIMER] = {
            "exists": True,
            "active": True,
            "next_run": "Sun 2026-11-22 02:00",
            "last_result": "success",
        }
        linux.timers[unix_schedule.WEEKLY_TIMER] = {
            "exists": True,
            "active": False,
            "last_result": "exit-code",
        }
        result = cli_runner.invoke(app, ["schedule", "status"])

        output = _out(result)
        assert "Sun 2026-11-22 02:00" in output
        assert "Last result: OK" in output
        assert "exit-code" in output
        assert "inactive" in output
        assert "Not installed" in output  # the check timer

    def test_counts_cron_entries(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        _enable_in_config()
        linux.cron_entries = ["0 2 * * * pfs backup run", "0 3 * * 0 pfs backup run"]
        result = cli_runner.invoke(app, ["schedule", "status"])

        assert "2 installed" in _out(result)
        assert "nothing is installed" not in _out(result)

    def test_enabled_but_nothing_installed_is_flagged(
        self, cli_runner: CliRunner, saved_config: AppConfig, linux: FakeUnixScheduler
    ) -> None:
        _enable_in_config()
        result = cli_runner.invoke(app, ["schedule", "status"])

        assert "nothing is installed" in _out(result)


def test_status_on_windows_shows_each_task(
    cli_runner: CliRunner, saved_config: AppConfig, windows: dict
) -> None:
    windows[scheduler_mod._DAILY_TASK_NAME] = {"exists": True, "last_result": 0}
    windows[scheduler_mod._CHECK_TASK_NAME] = {"exists": True, "last_result": 0}
    result = cli_runner.invoke(app, ["schedule", "status"])

    output = _out(result)
    assert result.exit_code == 0, output
    assert "Windows task (daily): Registered" in output
    assert "Windows task (weekly): Not registered" in output
    assert "no-recent-backup check" in output


# ---------------------------------------------------------------------------
# run-now
# ---------------------------------------------------------------------------


def test_run_now_without_devices_fails(cli_runner: CliRunner, saved_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["schedule", "run-now"])

    assert result.exit_code == 1
    assert "No enabled devices" in _out(result)


def test_run_now_reports_a_device_that_fails(
    cli_runner: CliRunner, saved_config: AppConfig
) -> None:
    saved_config.add_device(
        DeviceConfig(
            id="down-fw",
            label="Down",
            host="127.0.0.1",
            ssh_port=_closed_port(),
            fallback_method=None,
            timeout=5,
        )
    )
    saved_config.save()
    result = cli_runner.invoke(app, ["schedule", "run-now"])

    output = _out(result)
    assert result.exit_code == 0, output
    assert "Backing up down-fw" in output
    assert "down-fw:" in output
