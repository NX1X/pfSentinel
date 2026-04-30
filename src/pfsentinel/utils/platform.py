"""Platform detection and OS-specific helpers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_macos() -> bool:
    return sys.platform == "darwin"


def app_config_dir() -> Path:
    """Return the application config directory (cross-platform)."""
    return Path.home() / ".pfsentinel"


def default_backup_dir() -> Path:
    """Return a sensible default backup directory."""
    if is_windows():
        return Path.home() / "Documents" / "pfSentinel"
    return Path.home() / "pfSentinel"


def run_command(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess command and return the result."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=check,
    )


def create_windows_task(
    task_name: str,
    executable: str,
    args: str,
    schedule_type: str,  # "DAILY" or "WEEKLY"
    start_time: str,
    day_of_week: str = "",
) -> bool:
    """Create a Windows Task Scheduler task using schtasks.exe."""
    if not is_windows():
        return False

    cmd = [
        "schtasks",
        "/Create",
        "/TN",
        task_name,
        "/TR",
        f'"{executable}" {args}',
        "/SC",
        schedule_type,
        "/ST",
        start_time,
        "/F",  # Force overwrite
    ]

    if schedule_type == "WEEKLY" and day_of_week:
        cmd.extend(["/D", day_of_week.upper()[:3]])

    try:
        result = run_command(cmd, check=False)
        return result.returncode == 0
    except FileNotFoundError:
        return False


def delete_windows_task(task_name: str) -> bool:
    """Delete a Windows Task Scheduler task."""
    if not is_windows():
        return False
    try:
        result = run_command(["schtasks", "/Delete", "/TN", task_name, "/F"], check=False)
        return result.returncode == 0
    except FileNotFoundError:
        return False


def query_windows_task(task_name: str) -> dict:
    """Query status of a Windows Task Scheduler task."""
    if not is_windows():
        return {"exists": False}
    try:
        result = run_command(
            ["schtasks", "/Query", "/TN", task_name, "/FO", "CSV", "/NH"], check=False
        )
        if result.returncode != 0:
            return {"exists": False}
        parts = result.stdout.strip().strip('"').split('","')
        return {
            "exists": True,
            "name": parts[0] if parts else task_name,
            "next_run": parts[1] if len(parts) > 1 else None,
            "status": parts[2] if len(parts) > 2 else None,
        }
    except Exception:
        return {"exists": False}


def get_executable_path() -> str:
    """Get path to the current executable or python script."""
    if getattr(sys, "frozen", False):
        # PyInstaller bundle
        return sys.executable
    return f'"{sys.executable}" -m pfsentinel'
