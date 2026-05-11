"""CLI commands for backup scheduling."""

from __future__ import annotations

import typer
from rich.panel import Panel

from pfsentinel.cli.formatters import console, print_error, print_info, print_success
from pfsentinel.models.config import AppConfig
from pfsentinel.services.scheduler import SchedulerService

app = typer.Typer(help="Backup scheduling")


def _get_scheduler() -> tuple[AppConfig, SchedulerService]:
    config = AppConfig.load()
    return config, SchedulerService(config.schedule)


@app.command("enable")
def schedule_enable(
    daily_time: str = typer.Option("02:00", "--daily-time", help="Daily backup time HH:MM"),
    weekly_day: str = typer.Option("sunday", "--weekly-day", help="Weekly backup day"),
    weekly_time: str = typer.Option("03:00", "--weekly-time", help="Weekly backup time HH:MM"),
    no_weekly: bool = typer.Option(False, "--no-weekly", help="Disable weekly backup"),
    use_task_scheduler: bool = typer.Option(
        True, "--task-scheduler/--no-task-scheduler", help="Use Windows Task Scheduler"
    ),
) -> None:
    """Enable scheduled backups."""
    config = AppConfig.load()
    config.schedule.enabled = True
    config.schedule.daily_enabled = True
    config.schedule.daily_time = daily_time
    config.schedule.weekly_enabled = not no_weekly
    config.schedule.weekly_day = weekly_day
    config.schedule.weekly_time = weekly_time
    config.schedule.use_windows_task_scheduler = use_task_scheduler
    config.save()

    scheduler = SchedulerService(config.schedule)
    success = scheduler.apply_schedule()

    if success:
        print_success("Scheduling enabled")
        print_info(f"  Daily: {daily_time}")
        if not no_weekly:
            print_info(f"  Weekly: {weekly_day} at {weekly_time}")
        if not (
            use_task_scheduler
            and __import__("pfsentinel.utils.platform", fromlist=["is_windows"]).is_windows()
        ):
            print_info("  Mode: in-process (runs only while this process is alive)")
            print_info(
                "  Tip:  use 'schedule run-now' or a cron job for persistent scheduling on Linux"
            )
    else:
        from pfsentinel.utils.platform import is_windows

        if use_task_scheduler and is_windows():
            print_error("Failed to register Windows Task Scheduler tasks.")
            print_info("  Tasks run as your user with LogonType=S4U so they fire when")
            print_info("  you are signed out. This requires the 'Log on as a batch job'")
            print_info("  privilege (default for local Administrators).")
            print_info("  Try running PowerShell as Administrator, or use --no-task-scheduler.")
        else:
            print_error("Failed to start in-process scheduler.")
            print_info("  Install the 'schedule' package: pip install schedule")
        raise typer.Exit(1)


@app.command("disable")
def schedule_disable() -> None:
    """Disable scheduled backups."""
    config = AppConfig.load()
    config.schedule.enabled = False
    config.save()

    scheduler = SchedulerService(config.schedule)
    scheduler.remove_schedule()
    print_success("Scheduling disabled")


@app.command("status")
def schedule_status() -> None:
    """Show scheduling status."""
    config, scheduler = _get_scheduler()
    status = scheduler.get_status()

    lines = [
        f"[bold]Enabled:[/] {'Yes' if status['enabled'] else 'No'}",
        f"[bold]Daily:[/] {'Yes' if status['daily_enabled'] else 'No'} at {status['daily_time']}",
        (
            f"[bold]Weekly:[/] {'Yes' if status['weekly_enabled'] else 'No'}"
            f" - {status['weekly_day']} at {status['weekly_time']}"
        ),
        f"[bold]In-process running:[/] {'Yes' if status['in_process_running'] else 'No'}",
    ]

    if "windows_daily" in status:
        wd = status["windows_daily"]
        wd_status = "Created" if wd.get("exists") else "Not found"
        lines.append(f"[bold]Windows task (daily):[/] {wd_status}")
        if wd.get("next_run"):
            lines.append(f"  Next run: {wd['next_run']}")

    console.print(Panel("\n".join(lines), title="Scheduler Status", border_style="cyan"))


@app.command("run-now")
def schedule_run_now() -> None:
    """Trigger an immediate backup for all devices (same as backup run)."""
    from pfsentinel.cli.formatters import print_progress
    from pfsentinel.services.backup import BackupService
    from pfsentinel.services.credentials import CredentialService

    config = AppConfig.load()
    creds = CredentialService()
    svc = BackupService(config, creds)

    devices = config.enabled_devices()
    if not devices:
        print_error("No enabled devices configured")
        raise typer.Exit(1)

    for device in devices:
        print_info(f"Backing up {device.id}...")
        try:
            record = svc.run_backup(device.id, progress=print_progress)
            print_success(f"{device.id}: {record.filename}")
        except Exception as e:
            print_error(f"{device.id}: {e}")
