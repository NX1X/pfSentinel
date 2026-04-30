"""Backup filename generation and parsing."""

from __future__ import annotations

import re
from datetime import datetime

from pfsentinel.models.backup import ChangeCategory

# Pattern: device-id_YYYY-MM-DD_HHMMSS_#SEQ_changes.xml[.gz]
_FILENAME_RE = re.compile(
    r"^(?P<device_id>[a-z0-9\-]+)_"
    r"(?P<date>\d{4}-\d{2}-\d{2})_"
    r"(?P<time>\d{6})_"
    r"#(?P<seq>\d{3})_"
    r"(?P<changes>[a-z+]+)"
    r"\.xml(?P<gz>\.gz)?$"
)


def generate_filename(
    device_id: str,
    changes: list[ChangeCategory],
    sequence: int,
    compressed: bool,
    timestamp: datetime | None = None,
) -> str:
    """Generate a backup filename from components.

    Example: home-fw_2025-07-06_143022_#001_interfaces+system.xml.gz
    """
    ts = timestamp or datetime.now()
    date_str = ts.strftime("%Y-%m-%d")
    time_str = ts.strftime("%H%M%S")
    seq_str = f"#{sequence:03d}"

    if not changes:
        changes_str = "minor"
    else:
        # Max 3 categories joined with +
        changes_str = "+".join(c.value for c in changes[:3])

    ext = ".xml.gz" if compressed else ".xml"
    return f"{device_id}_{date_str}_{time_str}_{seq_str}_{changes_str}{ext}"


def generate_relative_path(filename: str, timestamp: datetime | None = None) -> str:
    """Generate date-organized relative path for backup storage.

    Example: 2025/07/06/home-fw_2025-07-06_143022_#001_interfaces.xml.gz
    """
    ts = timestamp or datetime.now()
    return f"{ts.year}/{ts.month:02d}/{ts.day:02d}/{filename}"


_DEFAULT_EXTENSIONS: dict[str, str] = {
    "config": ".xml",
    "rrd": ".tar",
    "pkg": ".tar",
    "dhcp": ".txt",
    "aliases": ".tar",
    "certs": ".tar",
    "logs": ".tar",
    "zfs": ".zfs",
    "archive": ".tar",
}


def generate_backup_filename(
    device_id: str,
    backup_type: str,
    compressed: bool,
    timestamp: datetime | None = None,
    sequence: int = 1,
    label: str = "",
    extension: str = "",
) -> str:
    """Generate filename for any backup type.

    Config type should still use generate_filename() for backward-compatibility.
    Other types use: {device_id}_{date}_{time}_#{seq}_{label}.{ext}[.gz]
    """
    ts = timestamp or datetime.now()
    date_str = ts.strftime("%Y-%m-%d")
    time_str = ts.strftime("%H%M%S")
    seq_str = f"#{sequence:03d}"
    type_label = label or backup_type

    ext = extension or _DEFAULT_EXTENSIONS.get(backup_type, ".dat")
    if compressed and not ext.endswith(".gz"):
        ext += ".gz"

    return f"{device_id}_{date_str}_{time_str}_{seq_str}_{type_label}{ext}"


def generate_typed_relative_path(
    backup_type: str, filename: str, timestamp: datetime | None = None
) -> str:
    """Generate type-organized relative path for non-config backup types.

    Example: rrd/2025/07/06/device_2025-07-06_143022_#001_rrd.tar.gz
    """
    ts = timestamp or datetime.now()
    return f"{backup_type}/{ts.year}/{ts.month:02d}/{ts.day:02d}/{filename}"


def parse_filename(filename: str) -> dict | None:
    """Parse a backup filename into its components. Returns None if not matching."""
    m = _FILENAME_RE.match(filename)
    if not m:
        return None

    date_str = m.group("date")
    time_str = m.group("time")
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M%S")

    raw_changes = m.group("changes")
    changes = []
    for part in raw_changes.split("+"):
        try:
            changes.append(ChangeCategory(part))
        except ValueError:
            pass

    return {
        "device_id": m.group("device_id"),
        "timestamp": dt,
        "sequence": int(m.group("seq")),
        "changes": changes,
        "compressed": bool(m.group("gz")),
    }
