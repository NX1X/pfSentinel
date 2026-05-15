"""Platform detection and OS-specific helpers."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape


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


_TASK_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>pfSentinel</Author>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    {trigger}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_id}</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _current_user_id() -> str:
    """Return the current user as 'DOMAIN\\Username' (or just 'Username' if no domain)."""
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def _daily_trigger_xml(start_time: str) -> str:
    return (
        "<CalendarTrigger>"
        f"<StartBoundary>2025-01-01T{start_time}:00</StartBoundary>"
        "<Enabled>true</Enabled>"
        "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
        "</CalendarTrigger>"
    )


def _weekly_trigger_xml(start_time: str, day_of_week: str) -> str:
    day_tag = day_of_week.strip().capitalize() or "Sunday"
    return (
        "<CalendarTrigger>"
        f"<StartBoundary>2025-01-01T{start_time}:00</StartBoundary>"
        "<Enabled>true</Enabled>"
        "<ScheduleByWeek>"
        f"<DaysOfWeek><{day_tag}/></DaysOfWeek>"
        "<WeeksInterval>1</WeeksInterval>"
        "</ScheduleByWeek>"
        "</CalendarTrigger>"
    )


def create_windows_task(
    task_name: str,
    executable: str,
    args: str,
    schedule_type: str,  # "DAILY" or "WEEKLY"
    start_time: str,
    day_of_week: str = "",
) -> bool:
    """Create a Windows Task Scheduler task via XML registration.

    The task is registered with LogonType=S4U so it runs whether the user is
    signed in or not (no stored password), wakes the machine if asleep, and
    ignores battery state. Returns True on success.
    """
    if not is_windows():
        return False

    if schedule_type == "DAILY":
        trigger = _daily_trigger_xml(start_time)
    elif schedule_type == "WEEKLY":
        trigger = _weekly_trigger_xml(start_time, day_of_week)
    else:
        return False

    description = f"pfSentinel scheduled backup ({schedule_type.lower()})"
    xml_doc = _TASK_XML_TEMPLATE.format(
        description=xml_escape(description),
        trigger=trigger,
        user_id=xml_escape(_current_user_id()),
        command=xml_escape(executable),
        arguments=xml_escape(args),
    )

    # schtasks expects task XML in UTF-16 LE with BOM. We write+close via a
    # context manager (delete=False keeps the file around for schtasks to read).
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".xml", delete=False, prefix="pfsentinel-task-"
    ) as tmp:
        tmp.write(xml_doc.encode("utf-16"))
        tmp_name = tmp.name

    try:
        result = run_command(
            ["schtasks", "/Create", "/TN", task_name, "/XML", tmp_name, "/F"],
            check=False,
        )
        if result.returncode != 0:
            from loguru import logger

            stderr = (result.stderr or "").strip() or (result.stdout or "").strip()
            logger.error(f"schtasks /Create failed for '{task_name}': {stderr}")
        return result.returncode == 0
    except FileNotFoundError:
        return False
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


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


def get_executable_path() -> tuple[str, str]:
    """Get the executable path and any prefix args needed to invoke pfSentinel.

    Returns (executable, prefix_args). The executable is returned unquoted; the
    caller is responsible for quoting it when building a command line.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller bundle
        return sys.executable, ""
    return sys.executable, "-m pfsentinel"
