"""Backup scheduler service."""

from __future__ import annotations

import threading
from datetime import datetime, time, timedelta

from pfsentinel.models.config import ScheduleConfig
from pfsentinel.utils import unix_schedule
from pfsentinel.utils.logging import get_logger
from pfsentinel.utils.platform import (
    app_config_dir,
    create_windows_task,
    delete_windows_task,
    get_executable_path,
    is_elevated,
    is_windows,
    query_windows_task,
)

logger = get_logger(__name__)

# datetime.weekday(): Monday is 0, Sunday is 6.
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def parse_hhmm(value: str) -> time:
    """Parse a 'HH:MM' schedule time. Raises ValueError on anything else."""
    hour_str, _, minute_str = value.strip().partition(":")
    hour, minute = int(hour_str), int(minute_str)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time out of range: {value!r}")
    return time(hour=hour, minute=minute)


def next_daily_run(now: datetime, at: str) -> datetime:
    """Next occurrence of ``at`` strictly after ``now``.

    A target equal to ``now`` rolls to tomorrow so a job cannot fire twice
    for the same slot.
    """
    target = parse_hhmm(at)
    candidate = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def next_weekly_run(now: datetime, day: str, at: str) -> datetime | None:
    """Next ``day`` at ``at`` strictly after ``now``. None if day is unknown."""
    weekday = _WEEKDAYS.get(day.strip().lower())
    if weekday is None:
        logger.warning(f"Unknown weekly_day {day!r}; weekly schedule disabled")
        return None

    target = parse_hhmm(at)
    candidate = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    days_ahead = (weekday - now.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


_DAILY_TASK_NAME = "pfSentinel\\DailyBackup"
_WEEKLY_TASK_NAME = "pfSentinel\\WeeklyBackup"
_CHECK_TASK_NAME = "pfSentinel\\BackupCheck"
# Without admin rights, tasks cannot go into the \\pfSentinel folder, but a
# user may create tasks for their own account in the Task Scheduler root.
_USER_TASK_NAMES = {
    _DAILY_TASK_NAME: "pfSentinel-DailyBackup",
    _WEEKLY_TASK_NAME: "pfSentinel-WeeklyBackup",
    _CHECK_TASK_NAME: "pfSentinel-BackupCheck",
}


def _query_either(name: str) -> dict:
    """Status of the folder task, or of the no-admin root task if that one exists."""
    folder = query_windows_task(name)
    if folder.get("exists"):
        return folder
    root = query_windows_task(_USER_TASK_NAMES[name])
    return root if root.get("exists") else folder


def _task_name(name: str, elevated: bool) -> str:
    return name if elevated else _USER_TASK_NAMES[name]


class SchedulerService:
    """Manages scheduled backup jobs.

    Backends. The first three persist after the process exits; the last does not:
    - Windows Task Scheduler (schtasks.exe)
    - systemd user timers (Linux)
    - cron (Linux without a systemd user manager, macOS)
    - In-process scheduler (stdlib threading) - lives only as long as the
      process that started it, so it is used only when the OS scheduler is
      explicitly turned off
    """

    def __init__(self, config: ScheduleConfig, digest_at: str | None = None) -> None:
        """``digest_at`` (HH:MM) adds a run of the daily check task for the daily digest."""
        self._config = config
        self._digest_at = digest_at
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop_event = threading.Event()
        # Which backend the last apply_schedule() used: "windows-task",
        # "systemd", "cron" or "in-process". None if nothing was applied.
        self.backend: str | None = None

    def apply_schedule(self) -> bool:
        """Create/update scheduled tasks based on config. Returns True on success."""
        if not self._config.enabled:
            return self.remove_schedule()

        if not self._config.use_windows_task_scheduler:
            # The OS tasks of an earlier `schedule enable` stay: the
            # in-process scheduler ends with this process, so removing them
            # would leave nothing scheduled.
            self.backend = "in-process"
            return self.start_in_process()

        if is_windows():
            self.backend = "windows-task"
            return self._apply_windows_schedule()

        return self._apply_unix_schedule()

    def remove_schedule(self) -> bool:
        """Remove all scheduled tasks."""
        success = self._remove_os_schedule()
        self.stop_in_process()
        return success

    def _remove_os_schedule(self) -> bool:
        success = True
        if is_windows():
            # Remove both the folder tasks (admin) and the root tasks (no admin);
            # a missing task is fine, a task we cannot delete is a failure.
            for folder_name, root_name in _USER_TASK_NAMES.items():
                for name in (folder_name, root_name):
                    if query_windows_task(name).get("exists") and not delete_windows_task(name):
                        success = False
        else:
            # Remove from both backends: the schedule may have been applied
            # with systemd on one run and cron on another.
            if not unix_schedule.remove_systemd_timers():
                success = False
            if not unix_schedule.remove_cron():
                success = False
        return success

    def _remove_unwanted_windows_tasks(self, wanted: set[str], elevated: bool) -> bool:
        """Delete every pfSentinel task that this apply does not create.

        Turning weekly off, or enabling once as administrator and once
        without, left the other task behind: both then ran at 02:00.

        As administrator, the no-admin root tasks (pfSentinel-*) are only
        named, not deleted: they may belong to another user of this PC.
        """
        success = True
        for folder_name, root_name in _USER_TASK_NAMES.items():
            for name in (folder_name, root_name):
                if name in wanted or not query_windows_task(name).get("exists"):
                    continue
                if elevated and name == root_name:
                    logger.warning(
                        f"An older task {name} also exists. If it is yours, remove it: "
                        f'schtasks /Delete /TN "{name}" /F'
                    )
                    continue
                if delete_windows_task(name):
                    logger.info(f"Removed the scheduled task {name}")
                else:
                    hint = " (run in an administrator shell)" if "\\" in name else ""
                    logger.warning(f"Could not remove the old scheduled task {name}{hint}")
                    success = False
        return success

    def _apply_windows_schedule(self) -> bool:
        # Times come from config.json and end up inside the task XML, so check
        # them here too, not only in the `schedule enable` command.
        try:
            if self._config.daily_enabled:
                unix_schedule.validate_spec(self._config.daily_time)
            if self._config.weekly_enabled:
                unix_schedule.validate_spec(self._config.weekly_time, self._config.weekly_day)
            extra_check_times = tuple(unix_schedule.check_times(self._digest_at)[1:])
        except unix_schedule.ScheduleSpecError as e:
            logger.error(f"Invalid schedule in config.json: {e}")
            return False

        executable, prefix_args = get_executable_path()
        args = f"{prefix_args} backup run --non-interactive".strip()
        success = True
        elevated = is_elevated()
        wanted = {_task_name(_CHECK_TASK_NAME, elevated)}
        if self._config.daily_enabled:
            wanted.add(_task_name(_DAILY_TASK_NAME, elevated))
        if self._config.weekly_enabled:
            wanted.add(_task_name(_WEEKLY_TASK_NAME, elevated))

        if self._config.daily_enabled:
            ok = create_windows_task(
                task_name=_task_name(_DAILY_TASK_NAME, elevated),
                executable=executable,
                args=args,
                schedule_type="DAILY",
                start_time=unix_schedule.normalize_hhmm(self._config.daily_time),
            )
            if not ok:
                logger.error("Failed to create daily Windows Task Scheduler task")
                success = False
            else:
                logger.info(
                    f"Daily backup scheduled at {self._config.daily_time} via Task Scheduler"
                )

        if self._config.weekly_enabled:
            ok = create_windows_task(
                task_name=_task_name(_WEEKLY_TASK_NAME, elevated),
                executable=executable,
                args=args,
                schedule_type="WEEKLY",
                start_time=unix_schedule.normalize_hhmm(self._config.weekly_time),
                day_of_week=self._config.weekly_day,
            )
            if not ok:
                logger.error("Failed to create weekly Windows Task Scheduler task")
                success = False
            else:
                logger.info(
                    f"Weekly backup scheduled {self._config.weekly_day}"
                    f" at {self._config.weekly_time}"
                )

        check_args = f"{prefix_args} backup check-recent --non-interactive".strip()
        if create_windows_task(
            task_name=_task_name(_CHECK_TASK_NAME, elevated),
            executable=executable,
            args=check_args,
            schedule_type="CHECK",
            start_time=unix_schedule.CHECK_AT,
            extra_times=extra_check_times,
        ):
            logger.info("No-recent-backup check scheduled at sign-in and daily")
        else:
            logger.error("Failed to create the no-recent-backup check task")
            success = False

        # Remove old tasks only once the new ones exist: a failed create must
        # not leave nothing scheduled. A task that cannot be removed only
        # warns; the device lock keeps two runs apart.
        if success:
            self._remove_unwanted_windows_tasks(wanted, elevated)

        # As administrator, this is the one moment the Event Log source can be
        # registered. Without admin it is skipped: `pfs notify eventlog enable`
        # in an administrator shell does it later.
        if not elevated:
            logger.info("Not elevated: Event Log source not registered")
            return success
        try:
            from pfsentinel.services.notifications import register_event_source

            register_event_source()
        except Exception as e:
            logger.warning(f"Could not register the Windows Event Log source: {e}")
        return success

    def _backup_argv(self) -> list[str]:
        executable, prefix_args = get_executable_path()
        return [executable, *prefix_args.split(), "backup", "run", "--non-interactive"]

    def _check_argv(self) -> list[str]:
        executable, prefix_args = get_executable_path()
        return [executable, *prefix_args.split(), "backup", "check-recent", "--non-interactive"]

    def _unix_spec(self) -> tuple[str | None, tuple[str, str] | None]:
        daily = self._config.daily_time if self._config.daily_enabled else None
        weekly = (
            (self._config.weekly_day, self._config.weekly_time)
            if self._config.weekly_enabled
            else None
        )
        return daily, weekly

    def _apply_unix_schedule(self) -> bool:
        """Install systemd user timers, or cron entries when systemd is absent."""
        daily, weekly = self._unix_spec()
        if daily is None and weekly is None:
            logger.error("Neither daily nor weekly backups are enabled - nothing to schedule")
            return False
        try:
            unix_schedule.validate_spec(daily or "00:00")
            if weekly is not None:
                unix_schedule.validate_spec(weekly[1], weekly[0])
            if self._digest_at is not None:
                unix_schedule.validate_spec(self._digest_at)
        except unix_schedule.ScheduleSpecError as e:
            logger.error(str(e))
            return False

        argv = self._backup_argv()
        try:
            if unix_schedule.systemd_user_available():
                self.backend = "systemd"
                ok = unix_schedule.install_systemd_timers(
                    argv, daily, weekly, self._check_argv(), check_extra_at=self._digest_at
                )
                if ok:
                    # Do not leave a stale cron copy that would double-run backups.
                    unix_schedule.remove_cron()
                return ok

            if unix_schedule.cron_available():
                self.backend = "cron"
                log_path = app_config_dir() / "logs" / "scheduled.log"
                return unix_schedule.install_cron(
                    argv,
                    daily,
                    weekly,
                    log_path,
                    self._check_argv(),
                    check_extra_at=self._digest_at,
                )
        except unix_schedule.ScheduleSpecError as e:
            logger.error(f"Cannot install the schedule: {e}")
            return False

        logger.error(
            "No persistent scheduler found: need a systemd user session or crontab. "
            "Install cron, or run 'pfs backup run' from another scheduler."
        )
        self.backend = None
        return False

    def next_run_after(self, now: datetime) -> datetime | None:
        """Return the next scheduled run strictly after ``now``, or None.

        Pure function of the config - no clock, no threads - so the schedule
        arithmetic is directly testable.
        """
        candidates: list[datetime] = []
        if self._config.daily_enabled:
            candidates.append(next_daily_run(now, self._config.daily_time))
        if self._config.weekly_enabled:
            weekly = next_weekly_run(now, self._config.weekly_day, self._config.weekly_time)
            if weekly is not None:
                candidates.append(weekly)
        return min(candidates) if candidates else None

    def start_in_process(self) -> bool:
        """Start an in-process background scheduler thread. Returns True on success."""
        if self._running:
            return True

        if self.next_run_after(datetime.now()) is None:
            logger.error("Neither daily nor weekly backups are enabled - nothing to schedule")
            return False

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._schedule_loop, name="pfsentinel-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("In-process scheduler started")
        return True

    def stop_in_process(self) -> None:
        """Stop the in-process scheduler thread and wait briefly for it to exit."""
        self._running = False
        self._stop_event.set()  # interrupts the wait immediately
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        logger.info("In-process scheduler stopped")

    def _schedule_loop(self) -> None:
        """Sleep until the next due run, fire it, repeat.

        Waits on an Event rather than polling, so stop_in_process() takes
        effect immediately instead of after a poll interval.
        """
        while self._running:
            now = datetime.now()
            due = self.next_run_after(now)
            if due is None:
                logger.warning("No enabled schedule remains; scheduler thread exiting")
                return

            delay = (due - now).total_seconds()
            logger.debug(f"Next scheduled backup at {due:%Y-%m-%d %H:%M} ({delay:.0f}s)")

            if self._stop_event.wait(timeout=max(0.0, delay)):
                return  # stop requested during the wait

            if self._running:
                self._run_backup_job()

    def _run_backup_job(self) -> None:
        """Called by in-process scheduler to trigger a backup."""
        from pfsentinel.models.config import AppConfig
        from pfsentinel.services.backup import BackupService
        from pfsentinel.services.credentials import CredentialService

        logger.info("Scheduled backup starting...")
        config = AppConfig.load()
        creds = CredentialService()
        svc = BackupService(config, creds)
        try:
            results = svc.run_all_backups()
            logger.info(f"Scheduled backup completed: {len(results)} device(s)")
        except Exception as e:
            logger.error(f"Scheduled backup failed: {e}")

    def get_status(self) -> dict:
        """Return scheduler status information."""
        status: dict = {
            "enabled": self._config.enabled,
            "in_process_running": self._running,
            "daily_enabled": self._config.daily_enabled,
            "daily_time": self._config.daily_time,
            "weekly_enabled": self._config.weekly_enabled,
            "weekly_day": self._config.weekly_day,
            "weekly_time": self._config.weekly_time,
        }

        if is_windows() and self._config.use_windows_task_scheduler:
            daily = _query_either(_DAILY_TASK_NAME)
            weekly = _query_either(_WEEKLY_TASK_NAME)
            status["windows_daily"] = daily
            status["windows_weekly"] = weekly
            status["windows_check"] = _query_either(_CHECK_TASK_NAME)
        elif not is_windows():
            status["systemd_daily"] = unix_schedule.query_systemd_timer(unix_schedule.DAILY_TIMER)
            status["systemd_weekly"] = unix_schedule.query_systemd_timer(unix_schedule.WEEKLY_TIMER)
            status["systemd_check"] = unix_schedule.query_systemd_timer(unix_schedule.CHECK_TIMER)
            status["cron_entries"] = unix_schedule.query_cron()

        return status
