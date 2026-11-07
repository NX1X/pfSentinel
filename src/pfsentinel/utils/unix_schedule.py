"""Persistent scheduling on Linux and macOS: systemd user timers, cron fallback.

The in-process scheduler dies with the process that started it, so a
`pfs schedule enable` run from a shell never fired a single backup. These
backends hand the schedule to the OS instead:

- **systemd user timers** (preferred on Linux). ``Persistent=true`` runs a
  missed backup at the next boot or login. Timers fire while the user has a
  session; with ``loginctl enable-linger`` they also fire when logged out.
- **cron** (fallback: WSL without systemd, containers, macOS). Entries are
  tagged with a marker comment so they can be replaced and removed without
  touching the user's other crontab lines.

Times and weekdays are validated before they reach a unit file or crontab
line, so config values cannot inject extra directives or commands.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

SERVICE_UNIT = "pfsentinel-backup.service"
DAILY_TIMER = "pfsentinel-daily.timer"
WEEKLY_TIMER = "pfsentinel-weekly.timer"

CRON_MARKER = "# pfsentinel-managed"

# systemd OnCalendar day names, and cron day-of-week numbers (0 = Sunday).
_DAYS = {
    "monday": ("Mon", 1),
    "tuesday": ("Tue", 2),
    "wednesday": ("Wed", 3),
    "thursday": ("Thu", 4),
    "friday": ("Fri", 5),
    "saturday": ("Sat", 6),
    "sunday": ("Sun", 0),
}


class ScheduleSpecError(ValueError):
    """Raised for a time or weekday that cannot be scheduled."""


_HHMM = re.compile(r"(\d{1,2}):(\d{2})", re.ASCII)


def _hhmm(value: str) -> tuple[int, int]:
    # re.ASCII: str.isdigit() also accepts characters such as "²", which
    # int() then rejects with a bare ValueError instead of a clean error.
    match = _HHMM.fullmatch(value.strip())
    if match is None:
        raise ScheduleSpecError(f"Invalid time {value!r}, expected HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleSpecError(f"Time out of range: {value!r}")
    return hour, minute


def normalize_hhmm(value: str) -> str:
    """Return ``value`` as zero-padded "HH:MM" (Task Scheduler needs two digits)."""
    hour, minute = _hhmm(value)
    return f"{hour:02d}:{minute:02d}"


def _day(value: str) -> tuple[str, int]:
    day = _DAYS.get(value.strip().lower())
    if day is None:
        raise ScheduleSpecError(f"Invalid weekday {value!r}")
    return day


def validate_spec(at: str, day: str | None = None) -> None:
    """Raise ScheduleSpecError if ``at`` (and ``day``, when given) is invalid."""
    _hhmm(at)
    if day is not None:
        _day(day)


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------


def systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def systemd_user_available() -> bool:
    """True if a systemd user manager is running for this user."""
    if shutil.which("systemctl") is None:
        return False
    try:
        return _systemctl("show-environment").returncode == 0
    except OSError:
        return False


def linger_enabled() -> bool | None:
    """Whether systemd keeps this user's timers running while logged out.

    None when it cannot be determined (no loginctl).
    """
    if shutil.which("loginctl") is None:
        return None
    try:
        result = subprocess.run(
            ["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() == "yes"


def daily_on_calendar(at: str) -> str:
    hour, minute = _hhmm(at)
    return f"*-*-* {hour:02d}:{minute:02d}:00"


def weekly_on_calendar(day: str, at: str) -> str:
    hour, minute = _hhmm(at)
    return f"{_day(day)[0]} *-*-* {hour:02d}:{minute:02d}:00"


def _reject_line_breaks(argv: list[str]) -> None:
    """A newline in an argument would start a new unit directive or crontab line."""
    for arg in argv:
        if any(c in arg for c in "\n\r\x00"):
            raise ScheduleSpecError(f"Command argument contains a line break: {arg!r}")


def _systemd_quote(arg: str) -> str:
    """Quote one ExecStart argument.

    '%' starts a unit specifier and '$' a variable, even inside quotes, so both
    are doubled to stay literal (see "literal dollar sign" in systemd.service(5)).
    """
    arg = arg.replace("%", "%%").replace("$", "$$")
    if arg and not any(c.isspace() or c in "\"'\\;$" for c in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_service(argv: list[str]) -> str:
    _reject_line_breaks(argv)
    exec_start = " ".join(_systemd_quote(a) for a in argv)
    return (
        "[Unit]\n"
        "Description=pfSentinel scheduled backup\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
    )


def render_timer(description: str, on_calendar: str) -> str:
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={on_calendar}\n"
        "Persistent=true\n"
        f"Unit={SERVICE_UNIT}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def install_systemd_timers(
    argv: list[str], daily_at: str | None, weekly: tuple[str, str] | None
) -> bool:
    """Write the service and timer units and enable the requested timers.

    ``weekly`` is ``(day, time)``. A timer that is not requested is disabled
    and its unit file removed, so re-running with different options converges.
    """
    wanted: dict[str, str] = {}
    if daily_at is not None:
        wanted[DAILY_TIMER] = render_timer("pfSentinel daily backup", daily_on_calendar(daily_at))
    if weekly is not None:
        wanted[WEEKLY_TIMER] = render_timer("pfSentinel weekly backup", weekly_on_calendar(*weekly))
    if not wanted:
        return False

    unit_dir = systemd_user_dir()
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / SERVICE_UNIT).write_text(render_service(argv), encoding="utf-8")
        for name, body in wanted.items():
            (unit_dir / name).write_text(body, encoding="utf-8")
    except OSError as e:
        logger.error(f"Cannot write systemd units to {unit_dir}: {e}")
        return False

    for name in (DAILY_TIMER, WEEKLY_TIMER):
        if name not in wanted:
            _systemctl("disable", "--now", name)
            (unit_dir / name).unlink(missing_ok=True)

    if _systemctl("daemon-reload").returncode != 0:
        logger.error("systemctl --user daemon-reload failed")
        return False

    ok = True
    for name in wanted:
        result = _systemctl("enable", "--now", name)
        if result.returncode != 0:
            logger.error(f"Failed to enable {name}: {(result.stderr or '').strip()}")
            ok = False
    return ok


def remove_systemd_timers() -> bool:
    """Disable the timers and delete all pfSentinel unit files."""
    unit_dir = systemd_user_dir()
    present = [n for n in (DAILY_TIMER, WEEKLY_TIMER, SERVICE_UNIT) if (unit_dir / n).exists()]
    if not present:
        return True
    if shutil.which("systemctl") is not None:
        for name in (DAILY_TIMER, WEEKLY_TIMER):
            if name in present:
                _systemctl("disable", "--now", name)
    ok = True
    for name in present:
        try:
            (unit_dir / name).unlink()
        except OSError as e:
            logger.error(f"Cannot remove {unit_dir / name}: {e}")
            ok = False
    if shutil.which("systemctl") is not None:
        _systemctl("daemon-reload")
    return ok


def query_systemd_timer(name: str) -> dict:
    """Report whether a timer is installed and active, plus the last run result."""
    if not (systemd_user_dir() / name).exists():
        return {"exists": False}
    info: dict = {"exists": True}
    try:
        timer = _systemctl("show", name, "--property=ActiveState,NextElapseUSecRealtime")
        service = _systemctl("show", SERVICE_UNIT, "--property=Result,ExecMainStatus")
    except OSError:
        return info
    props: dict[str, str] = {}
    for line in (timer.stdout + service.stdout).splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    info["active"] = props.get("ActiveState") == "active"
    info["next_run"] = props.get("NextElapseUSecRealtime") or None
    info["last_result"] = props.get("Result") or None
    return info


def systemd_units_installed() -> bool:
    unit_dir = systemd_user_dir()
    return any((unit_dir / n).exists() for n in (DAILY_TIMER, WEEKLY_TIMER))


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------


def cron_available() -> bool:
    return shutil.which("crontab") is not None


def _read_crontab() -> list[str] | None:
    """Current crontab lines. [] if the user has none, None if it cannot be read."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        # "no crontab for <user>" is the normal empty case.
        return [] if "no crontab" in (result.stderr or "").lower() else None
    return result.stdout.splitlines()


def _write_crontab(lines: list[str]) -> bool:
    body = "\n".join(lines) + "\n" if lines else ""
    try:
        result = subprocess.run(
            ["crontab", "-"], input=body, capture_output=True, text=True, check=False
        )
    except OSError:
        return False
    if result.returncode != 0:
        logger.error(f"crontab install failed: {(result.stderr or '').strip()}")
    return result.returncode == 0


def cron_lines(
    argv: list[str], daily_at: str | None, weekly: tuple[str, str] | None, log_path: Path
) -> list[str]:
    """Crontab entries for the requested schedule. '%' is escaped (cron treats it as newline)."""
    _reject_line_breaks([*argv, str(log_path)])
    command = " ".join(shlex.quote(a) for a in argv) + f" >> {shlex.quote(str(log_path))} 2>&1"
    command = command.replace("%", "\\%")
    lines: list[str] = []
    if daily_at is not None:
        hour, minute = _hhmm(daily_at)
        lines.append(f"{minute} {hour} * * * {command} {CRON_MARKER} daily")
    if weekly is not None:
        day, at = weekly
        hour, minute = _hhmm(at)
        lines.append(f"{minute} {hour} * * {_day(day)[1]} {command} {CRON_MARKER} weekly")
    return lines


def _prepare_private_log(log_path: Path) -> None:
    """Create the cron log owner-only before cron first appends to it.

    cron's ">>" would otherwise create it with the default umask (often 0644),
    and the log can contain device names, hosts and error details.
    """
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    os.chmod(log_path, 0o600)


def install_cron(
    argv: list[str], daily_at: str | None, weekly: tuple[str, str] | None, log_path: Path
) -> bool:
    """Replace pfSentinel's crontab entries, leaving every other line untouched."""
    current = _read_crontab()
    if current is None:
        logger.error("Cannot read the current crontab; not modifying it")
        return False
    new = cron_lines(argv, daily_at, weekly, log_path)
    if not new:
        return False
    try:
        _prepare_private_log(log_path)
    except OSError as e:
        logger.warning(f"Cannot prepare log file {log_path}: {e}")
    kept = [ln for ln in current if CRON_MARKER not in ln]
    return _write_crontab(kept + new)


def remove_cron() -> bool:
    if not cron_available():
        return True
    current = _read_crontab()
    if current is None:
        return False
    if not any(CRON_MARKER in ln for ln in current):
        return True
    return _write_crontab([ln for ln in current if CRON_MARKER not in ln])


def query_cron() -> list[str]:
    """pfSentinel's installed crontab entries (empty if none)."""
    if not cron_available():
        return []
    return [ln for ln in (_read_crontab() or []) if CRON_MARKER in ln]
