"""CLI commands for backup scheduling."""

from __future__ import annotations

import typer
from rich.markup import escape
from rich.panel import Panel

from pfsentinel.cli.formatters import (
    console,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from pfsentinel.models.config import AppConfig
from pfsentinel.services.scheduler import SchedulerService
from pfsentinel.utils import unix_schedule

app = typer.Typer(help="Backup scheduling")


def _get_scheduler() -> tuple[AppConfig, SchedulerService]:
    config = AppConfig.load()
    return config, scheduler_for(config)


def scheduler_for(config: AppConfig) -> SchedulerService:
    """The scheduler for this config, with the daily digest time when the digest is on."""
    notifications = config.notifications
    digest_at = notifications.digest_time if notifications.digest_enabled else None
    if digest_at is not None:
        try:
            unix_schedule.validate_spec(digest_at)
        except unix_schedule.ScheduleSpecError as e:
            print_warning(f"Daily digest time ignored: {e}. Run: pfs notify digest enable --time")
            digest_at = None
    return SchedulerService(config.schedule, digest_at=digest_at)


@app.command("enable")
def schedule_enable(
    daily_time: str = typer.Option("02:00", "--daily-time", help="Daily backup time HH:MM"),
    weekly_day: str = typer.Option("sunday", "--weekly-day", help="Weekly backup day"),
    weekly_time: str = typer.Option("03:00", "--weekly-time", help="Weekly backup time HH:MM"),
    no_weekly: bool = typer.Option(False, "--no-weekly", help="Disable weekly backup"),
    use_task_scheduler: bool = typer.Option(
        True,
        "--task-scheduler/--no-task-scheduler",
        help=(
            "Use the OS scheduler (Windows Task Scheduler, systemd user timer or cron). "
            "--no-task-scheduler runs in-process and stops when this command exits."
        ),
    ),
) -> None:
    """Enable scheduled backups."""
    from pfsentinel.utils.platform import is_elevated, is_windows

    # Validate before anything is written: these values end up in task XML,
    # systemd unit files and crontab lines.
    try:
        unix_schedule.validate_spec(daily_time)
        if not no_weekly:
            unix_schedule.validate_spec(weekly_time, weekly_day)
    except unix_schedule.ScheduleSpecError as e:
        print_error(str(e))
        raise typer.Exit(2)

    if use_task_scheduler and is_windows() and not is_elevated():
        print_info("Not an administrator shell: the tasks are created for your user only,")
        print_info("in the Task Scheduler root (pfSentinel-DailyBackup and so on). From an")
        print_info("Administrator PowerShell they go into the \\pfSentinel folder instead, and")
        print_info("the Windows Event Log source is registered too.")

    config = AppConfig.load()
    config.schedule.enabled = True
    config.schedule.daily_enabled = True
    config.schedule.daily_time = daily_time
    config.schedule.weekly_enabled = not no_weekly
    config.schedule.weekly_day = weekly_day
    config.schedule.weekly_time = weekly_time
    config.schedule.use_windows_task_scheduler = use_task_scheduler
    config.save()

    scheduler = scheduler_for(config)
    success = scheduler.apply_schedule()

    if success:
        print_success("Scheduling enabled")
        print_info(f"  Daily: {daily_time}")
        if not no_weekly:
            print_info(f"  Weekly: {weekly_day} at {weekly_time}")
        if config.notifications.digest_enabled:
            print_info(f"  Daily digest: {config.notifications.digest_time}")
        _print_backend_notes(scheduler.backend)
    else:
        if scheduler.backend == "windows-task":
            print_error("Failed to register Windows Task Scheduler tasks.")
            print_info("  Try running PowerShell as Administrator, or use --no-task-scheduler.")
        elif scheduler.backend == "systemd":
            print_error("Failed to install systemd user timers.")
            print_info("  Check: systemctl --user status pfsentinel-daily.timer")
        elif scheduler.backend == "cron":
            print_error("Failed to install crontab entries.")
            print_info("  Check that 'crontab -l' works for your user.")
        elif scheduler.backend == "in-process":
            print_error("Failed to start in-process scheduler.")
        else:
            print_error("No persistent scheduler available (no systemd user session, no crontab).")
            print_info("  Install cron, or call 'pfs backup run' from your own scheduler.")
        raise typer.Exit(1)


def _print_backend_notes(backend: str | None) -> None:
    """Tell the user what will actually run the backups, and its limits."""
    if backend == "windows-task":
        print_info("  Mode: Windows Task Scheduler")
        print_info("  Runs while you are signed in (a locked screen counts). A run missed")
        print_info("  while signed out or asleep starts at your next sign-in.")
    elif backend == "systemd":
        print_info("  Mode: systemd user timers (pfsentinel-daily.timer, pfsentinel-weekly.timer)")
        print_info("  Missed runs (machine off) start at next boot. Logs: journalctl --user -u")
        print_info("  pfsentinel-backup.service")
        if unix_schedule.linger_enabled() is False:
            print_info(
                "  [yellow]Timers only fire while you are logged in.[/] To back up while",
                markup=True,
            )
            print_info("  logged out, run once:  loginctl enable-linger $USER")
        _warn_if_desktop_keyring()
    elif backend == "cron":
        from pfsentinel.utils.platform import app_config_dir

        print_info("  Mode: cron")
        print_info(f"  Logs: {app_config_dir() / 'logs' / 'scheduled.log'}")
        _warn_if_desktop_keyring()
    elif backend == "in-process":
        print_info("  Mode: in-process (stops when this process exits)")
        print_info("  Drop --no-task-scheduler to use the OS scheduler instead.")


def _warn_if_desktop_keyring() -> None:
    """A desktop keyring is locked until login, so backups before login fail."""
    from pfsentinel.services.credentials import CredentialService

    name = CredentialService().backend_name()
    if "file store" in name or "in-memory" in name:
        return
    print_info(
        f"  [yellow]Passwords are in your desktop keyring ({escape(name)}).[/] It stays locked",
        markup=True,
    )
    print_info("  until you log in, so a backup that runs before login cannot read them.")


@app.command("disable")
def schedule_disable() -> None:
    """Disable scheduled backups."""
    config = AppConfig.load()
    config.schedule.enabled = False
    config.save()

    scheduler = scheduler_for(config)
    if not scheduler.remove_schedule():
        # Before rc.3 this said "disabled" while the tasks kept running.
        print_error("Some scheduled tasks could not be removed: they still run.")
        print_info("Tasks made as administrator need an administrator shell:")
        print_info("run pfs schedule disable there. Check with: pfs schedule status")
        raise typer.Exit(1)
    print_success("Scheduling disabled")


# Task Scheduler last-result codes worth explaining. A task can be registered
# and still fail every single run, so the remediation matters more than the code.
_WINDOWS_TASK_ERRORS: dict[int, tuple[str, tuple[str, ...]]] = {
    0x80070002: (
        "ERROR_FILE_NOT_FOUND",
        (
            "[red]The task points at a program that is no longer there[/] (moved,",
            "uninstalled, or registered by an older pfSentinel). Re-run",
            "'pfs schedule enable' from an [bold]Administrator[/] shell to point it",
            "at the current pfs executable.",
        ),
    ),
    0x80070057: (
        "ERROR_INVALID_PARAMETER",
        (
            "[red]Task command line is malformed[/] - re-run 'pfs schedule enable'",
            "from an [bold]Administrator[/] shell to re-register it with the",
            "correct command.",
        ),
    ),
    0x80070005: (
        "ERROR_ACCESS_DENIED",
        (
            "The task could not start the program. Re-run 'pfs schedule enable'",
            "from an [bold]Administrator[/] shell, and check that pfs is in a",
            "folder your user can read and execute.",
        ),
    ),
}


# Informational task results (SCHED_S_* codes), which are states, not errors.
_SCHED_S_TASK_HAS_NOT_RUN = 0x41303
_WINDOWS_TASK_STATES = {
    0x41301: "running now",
    0x41302: "task is disabled",
    0x41306: "stopped by the user or a timeout",
}


def _format_windows_task(label: str, task: dict, *, check_task: bool = False) -> list[str]:
    """Render a Windows task's real health, not just whether it's registered."""
    if not task or not task.get("exists"):
        return [f"[bold]{label}:[/] [yellow]Not registered[/]"]

    out = [f"[bold]{label}:[/] Registered"]
    if task.get("next_run"):
        out.append(f"  Next run: {task['next_run']}")

    lr = task.get("last_result")
    if lr == _SCHED_S_TASK_HAS_NOT_RUN:
        # Windows reports a placeholder date (30/11/1999) for a task that
        # never ran; show the plain fact instead.
        out.append("  Last run: never")
        out.append("  Last result: [dim]not run yet[/]")
        return out
    if task.get("last_run"):
        out.append(f"  Last run: {task['last_run']}")

    if lr is None:
        return out
    if lr in _WINDOWS_TASK_STATES:
        out.append(f"  Last result: [dim]{_WINDOWS_TASK_STATES[lr]}[/]")
    elif lr == 0:
        out.append("  Last result: [green]OK[/]")
    elif lr & 0x80000000:
        code = f"0x{lr & 0xFFFFFFFF:08X}"
        known = _WINDOWS_TASK_ERRORS.get(lr & 0xFFFFFFFF)
        if known is not None:
            name, explanation = known
            out.append(f"  Last result: [red]FAILED {code} ({name})[/]")
            out.extend(f"  {line}" for line in explanation)
        else:
            out.append(f"  Last result: [red]FAILED {code}[/]")
    elif check_task:
        # backup check-recent exits 1 when a device had no recent backup.
        out.append(f"  Last result: [yellow]{lr} (a device had no recent backup; alert sent)[/]")
    else:
        # pfs backup run exits non-zero when the backup did not finish.
        out.append(f"  Last result: [red]FAILED (exit code {lr})[/]")
        out.append("  Run the same backup by hand to see the error: pfs backup run")
    return out


@app.command("status")
def schedule_status(
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show scheduling status."""
    config, scheduler = _get_scheduler()
    status = scheduler.get_status()
    status["backs_up"] = config.backup_policy.run_contents()
    if output_json:
        import json

        console.print_json(json.dumps(status, default=str))
        return

    lines = [
        f"[bold]Enabled:[/] {'Yes' if status['enabled'] else 'No'}",
        f"[bold]Daily:[/] {'Yes' if status['daily_enabled'] else 'No'} at {status['daily_time']}",
        (
            f"[bold]Weekly:[/] {'Yes' if status['weekly_enabled'] else 'No'}"
            f" - {status['weekly_day']} at {status['weekly_time']}"
        ),
        f"[bold]In-process running:[/] {'Yes' if status['in_process_running'] else 'No'}",
        f"[bold]Backs up:[/] {status['backs_up']}",
    ]
    if not config.backup_policy.extras.enabled_targets():
        lines.append("  [dim]Add extras: pfs config set extras rrd,pkg,dhcp,aliases,certs[/]")

    if "windows_daily" in status:
        lines.append("")
        lines.extend(_format_windows_task("Windows task (daily)", status["windows_daily"]))
        lines.extend(
            _format_windows_task("Windows task (weekly)", status.get("windows_weekly", {}))
        )
        if "windows_check" in status:
            lines.extend(
                _format_windows_task(
                    "Windows task (no-recent-backup check)",
                    status["windows_check"],
                    check_task=True,
                )
            )

    if "systemd_daily" in status:
        timers = [
            ("systemd timer (daily)", status["systemd_daily"]),
            ("systemd timer (weekly)", status["systemd_weekly"]),
            ("systemd timer (no-recent-backup check)", status.get("systemd_check", {})),
        ]
        if any(t.get("exists") for _, t in timers):
            lines.append("")
            for label, timer in timers:
                lines.extend(_format_systemd_timer(label, timer))
        if status.get("cron_entries"):
            lines.append("")
            lines.append(f"[bold]cron entries:[/] {len(status['cron_entries'])} installed")
        if status["enabled"] and not (
            any(t.get("exists") for _, t in timers) or status.get("cron_entries")
        ):
            lines.append("")
            lines.append(
                "[yellow]Enabled in config but nothing is installed in the OS scheduler.[/]"
            )
            lines.append("Re-run 'pfs schedule enable' to install it.")

    console.print(Panel("\n".join(lines), title="Scheduler Status", border_style="cyan"))


def _format_systemd_timer(label: str, timer: dict) -> list[str]:
    if not timer.get("exists"):
        return [f"[bold]{label}:[/] [yellow]Not installed[/]"]
    state = "[green]active[/]" if timer.get("active") else "[red]inactive[/]"
    out = [f"[bold]{label}:[/] {state}"]
    if timer.get("next_run"):
        out.append(f"  Next run: {timer['next_run']}")
    result = timer.get("last_result")
    if result == "success":
        out.append("  Last result: [green]OK[/]")
    elif result:
        out.append(f"  Last result: [red]{result}[/] (journalctl --user -u pfsentinel-backup)")
    return out


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
