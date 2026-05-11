"""Backup scheduler service."""

from __future__ import annotations

import threading

from loguru import logger

from pfsentinel.models.config import ScheduleConfig
from pfsentinel.utils.platform import (
    create_windows_task,
    delete_windows_task,
    get_executable_path,
    is_windows,
    query_windows_task,
)

_DAILY_TASK_NAME = "pfSentinel\\DailyBackup"
_WEEKLY_TASK_NAME = "pfSentinel\\WeeklyBackup"


class SchedulerService:
    """Manages scheduled backup jobs.

    Supports two backends:
    - Windows Task Scheduler (schtasks.exe) - persists after process exit
    - In-process scheduler (schedule library) - lives with the process
    """

    def __init__(self, config: ScheduleConfig) -> None:
        self._config = config
        self._thread: threading.Thread | None = None
        self._running = False

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

    def start_in_process(self) -> bool:
        """Start an in-process background scheduler thread. Returns True on success."""
        if self._running:
            return True

        try:
            import schedule
        except ImportError:
            logger.error("'schedule' package not installed — run: pip install schedule")
            return False

        schedule.clear("pfsentinel")

        if self._config.daily_enabled:
            schedule.every().day.at(self._config.daily_time).do(self._run_backup_job).tag(
                "pfsentinel"
            )

        if self._config.weekly_enabled:
            day_fn = getattr(schedule.every(), self._config.weekly_day.lower(), None)
            if day_fn:
                day_fn.at(self._config.weekly_time).do(self._run_backup_job).tag("pfsentinel")

        self._running = True
        self._thread = threading.Thread(target=self._schedule_loop, daemon=True)
        self._thread.start()
        logger.info("In-process scheduler started")
        return True

    def stop_in_process(self) -> None:
        """Stop the in-process scheduler thread."""
        self._running = False
        try:
            import schedule

            schedule.clear("pfsentinel")
        except ImportError:
            pass
        logger.info("In-process scheduler stopped")

    def _schedule_loop(self) -> None:
        import time

        import schedule

        while self._running:
            schedule.run_pending()
            time.sleep(30)

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
