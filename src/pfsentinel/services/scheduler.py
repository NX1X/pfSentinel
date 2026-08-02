"""Backup scheduler service."""

from __future__ import annotations

import threading
from datetime import datetime, time, timedelta

from loguru import logger

from pfsentinel.models.config import ScheduleConfig
from pfsentinel.utils.platform import (
    create_windows_task,
    delete_windows_task,
    get_executable_path,
    is_windows,
    query_windows_task,
)

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


class SchedulerService:
    """Manages scheduled backup jobs.

    Supports two backends:
    - Windows Task Scheduler (schtasks.exe) - persists after process exit
    - In-process scheduler (stdlib threading) - lives with the process
    """

    def __init__(self, config: ScheduleConfig) -> None:
        self._config = config
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop_event = threading.Event()

    def apply_schedule(self) -> bool:
        """Create/update scheduled tasks based on config. Returns True on success."""
        if not self._config.enabled:
            return self.remove_schedule()

        if self._config.use_windows_task_scheduler and is_windows():
            return self._apply_windows_schedule()
        else:
            return self.start_in_process()

    def remove_schedule(self) -> bool:
        """Remove all scheduled tasks."""
        success = True
        if is_windows():
            if not delete_windows_task(_DAILY_TASK_NAME):
                success = False
            if not delete_windows_task(_WEEKLY_TASK_NAME):
                success = False
        self.stop_in_process()
        return success

    def _apply_windows_schedule(self) -> bool:
        executable, prefix_args = get_executable_path()
        args = f"{prefix_args} backup run".strip()
        success = True

        if self._config.daily_enabled:
            ok = create_windows_task(
                task_name=_DAILY_TASK_NAME,
                executable=executable,
                args=args,
                schedule_type="DAILY",
                start_time=self._config.daily_time,
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
                task_name=_WEEKLY_TASK_NAME,
                executable=executable,
                args=args,
                schedule_type="WEEKLY",
                start_time=self._config.weekly_time,
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

        return success

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
            daily = query_windows_task(_DAILY_TASK_NAME)
            weekly = query_windows_task(_WEEKLY_TASK_NAME)
            status["windows_daily"] = daily
            status["windows_weekly"] = weekly

        return status
