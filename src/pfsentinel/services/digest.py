"""The daily digest: one message a day that summarizes every enabled device.

The daily check task (``pfs backup check-recent``, at sign-in, at 12:00 and
at the digest time) calls ``send_digest_if_due``. The first run on a
calendar day at or after ``notifications.digest_time`` sends it, and the
date is kept in ``digest_state.json`` so later runs that day do not.

The time of the last send is kept too: the next digest covers everything
since then (at most 7 days), so an alert between two sends is never left
out. A fixed 24 hours back from a send time that moves (sign-in, 12:00)
missed or repeated alerts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from pfsentinel.models.backup import BackupIndexError, BackupRecord, BackupType
from pfsentinel.models.config import AppConfig, NotificationConfig
from pfsentinel.services import alert_history
from pfsentinel.services.alert_history import AlertEntry
from pfsentinel.services.notifications import AlertKind, NotificationService
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

STATE_FILE = "digest_state.json"
WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=7)
# Alert lines per device: a long list hides the summary and passes
# Telegram's 4096-character limit.
MAX_ALERT_LINES = 10
_DEFAULT_TIME = time(8, 0)


@dataclass
class DeviceSummary:
    device_id: str
    last_backup: datetime | None
    stored: int
    recent: int
    problems: list[AlertEntry] = field(default_factory=list)
    # Set when backup_index.json cannot be read; then no backups are counted.
    index_error: str | None = None

    @property
    def problem_count(self) -> int:
        return len(self.problems) + (1 if self.index_error else 0)


@dataclass(frozen=True)
class Digest:
    title: str
    body: str
    problem_count: int


def digest_time(config: NotificationConfig) -> time:
    """The configured digest time, or 08:00 if config.json holds something invalid."""
    from pfsentinel.services.scheduler import parse_hhmm

    try:
        return parse_hhmm(config.digest_time)
    except ValueError:
        logger.warning(f"Invalid notifications.digest_time {config.digest_time!r}; using 08:00")
        return _DEFAULT_TIME


def last_sent_date() -> date | None:
    data = alert_history.read_json(alert_history.state_dir() / STATE_FILE)
    try:
        return date.fromisoformat(str(data["last_sent"])) if isinstance(data, dict) else None
    except (KeyError, ValueError):
        return None


def last_sent_at() -> datetime | None:
    data = alert_history.read_json(alert_history.state_dir() / STATE_FILE)
    try:
        return datetime.fromisoformat(str(data["last_sent_at"])) if isinstance(data, dict) else None
    except (KeyError, ValueError):
        return None


def mark_sent(day: date, at: datetime | None = None) -> None:
    state = {"last_sent": day.isoformat()}
    if at is not None:
        state["last_sent_at"] = at.isoformat(timespec="seconds")
    alert_history.write_private_json(alert_history.state_dir() / STATE_FILE, state)


def window_start(now: datetime, last_at: datetime | None) -> datetime:
    """Start of the period a digest covers: the last send, at most 7 days back."""
    if last_at is None or last_at >= now:
        return now - WINDOW
    return max(last_at, now - MAX_WINDOW)


def _period(now: datetime, since: datetime) -> str:
    if now - since == WINDOW:
        return "the last 24 hours"
    return f"the time since {since:%Y-%m-%d %H:%M}"


def is_due(config: NotificationConfig, now: datetime, last_sent: date | None) -> bool:
    if not config.digest_enabled or last_sent == now.date():
        return False
    return now.time() >= digest_time(config)


def summarize_devices(
    device_ids: list[str],
    list_backups: Callable[[str], list[BackupRecord]],
    alerts: list[AlertEntry],
    now: datetime,
    since: datetime | None = None,
) -> list[DeviceSummary]:
    since = since or now - WINDOW
    summaries: list[DeviceSummary] = []
    for device_id in device_ids:
        try:
            records = list_backups(device_id)
        except BackupIndexError as e:
            summaries.append(
                DeviceSummary(
                    device_id=device_id,
                    last_backup=None,
                    stored=0,
                    recent=0,
                    problems=[a for a in alerts if a.device == device_id],
                    index_error=e.reason,
                )
            )
            continue
        configs = [r for r in records if r.backup_type == BackupType.CONFIG]
        summaries.append(
            DeviceSummary(
                device_id=device_id,
                last_backup=max((r.created_at for r in configs), default=None),
                stored=len(records),
                recent=sum(1 for r in records if r.created_at >= since),
                problems=[a for a in alerts if a.device == device_id],
            )
        )
    return summaries


def _kind_label(kind: int) -> str:
    try:
        return AlertKind(kind).name.replace("_", " ").capitalize()
    except ValueError:
        return f"Event {kind}"


def _alert_line(entry: AlertEntry) -> str:
    return f"  - {entry.at:%Y-%m-%d %H:%M} {_kind_label(entry.kind)}: {entry.summary}"


def _alert_lines(alerts: list[AlertEntry]) -> list[str]:
    lines = [_alert_line(a) for a in alerts[-MAX_ALERT_LINES:]]
    if len(alerts) > MAX_ALERT_LINES:
        lines.insert(0, f"  ({len(alerts) - MAX_ALERT_LINES} earlier, not shown)")
    return lines


def render_digest(
    summaries: list[DeviceSummary],
    other_alerts: list[AlertEntry],
    now: datetime,
    since: datetime | None = None,
) -> Digest:
    problem_count = sum(s.problem_count for s in summaries)
    period = _period(now, since or now - WINDOW)
    lines = [f"Daily summary at {now:%Y-%m-%d %H:%M}, covering {period}."]
    if not summaries:
        lines += ["", "No enabled devices."]
    for s in summaries:
        status = f"{s.problem_count} problem(s)" if s.problem_count else "OK"
        last = f"{s.last_backup:%Y-%m-%d %H:%M}" if s.last_backup else "never"
        lines += ["", f"{s.device_id}: {status}"]
        if s.index_error:
            lines += [
                f"  Backup index cannot be read ({s.index_error}).",
                f"  Rebuild it: pfs backup reindex -d {s.device_id}",
            ]
        else:
            lines += [
                f"  Last successful config backup: {last}",
                f"  Backups stored: {s.stored} ({s.recent} in {period})",
            ]
        if s.problems:
            lines.append(f"  Problems in {period}:")
            lines += _alert_lines(s.problems)
        else:
            lines.append(f"  Problems in {period}: none")
    if other_alerts:
        lines += ["", f"Other alerts in {period}:"]
        lines += _alert_lines(other_alerts)
    if problem_count:
        title = f"pfSentinel - Daily summary: {problem_count} problem(s)"
    else:
        title = "pfSentinel - Daily summary: all OK"
    return Digest(title=title, body="\n".join(lines), problem_count=problem_count)


def build_digest(
    config: AppConfig,
    list_backups: Callable[[str], list[BackupRecord]],
    now: datetime | None = None,
) -> Digest:
    when = now or datetime.now()
    since = window_start(when, last_sent_at())
    alerts = alert_history.load_alerts(since)
    device_ids = [d.id for d in config.enabled_devices()]
    summaries = summarize_devices(device_ids, list_backups, alerts, when, since)
    known = set(device_ids)
    others = [a for a in alerts if a.device not in known]
    return render_digest(summaries, others, when, since)


def send_digest(
    config: AppConfig,
    list_backups: Callable[[str], list[BackupRecord]],
    notifications: NotificationService,
    now: datetime | None = None,
    remember: bool = True,
) -> tuple[Digest, dict[str, str | None]]:
    """Build and send the digest now. With ``remember``, save today's date.

    The date is not saved when every channel failed, so the next check run
    tries again. A manual send (``pfs notify digest send``) does not save it,
    so the scheduled digest still goes out that day.
    """
    when = now or datetime.now()
    digest = build_digest(config, list_backups, when)
    results = notifications.send_digest(digest.title, digest.body, digest.problem_count > 0)
    if not results:
        logger.warning("Daily digest: no notification channel is enabled")
    if remember and (not results or any(err is None for err in results.values())):
        mark_sent(when.date(), when)
    return digest, results


def send_digest_if_due(
    config: AppConfig,
    list_backups: Callable[[str], list[BackupRecord]],
    notifications: NotificationService,
    now: datetime | None = None,
) -> dict[str, str | None] | None:
    """Send the digest if it is on and not yet sent today. None when not due."""
    when = now or datetime.now()
    if not is_due(config.notifications, when, last_sent_date()):
        return None
    _, results = send_digest(config, list_backups, notifications, when)
    return results
