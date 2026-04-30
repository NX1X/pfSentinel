"""Backup record and index models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class ChangeCategory(StrEnum):
    INTERFACES = "interfaces"
    FIREWALL = "firewall"
    USERS = "users"
    SYSTEM = "system"
    PACKAGES = "packages"
    DHCP = "dhcp"
    VPN = "vpn"
    ROUTES = "routes"
    INITIAL = "initial"
    MINOR = "minor"


class BackupType(StrEnum):
    CONFIG = "config"
    RRD = "rrd"
    PACKAGE_CONFIGS = "pkg"
    DHCP_LEASES = "dhcp"
    ALIASES = "aliases"
    CERTIFICATES = "certs"
    LOGS = "logs"
    ZFS_SNAPSHOT = "zfs"
    FS_ARCHIVE = "archive"


class BackupRecord(BaseModel):
    """Persisted metadata for one backup file."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    device_id: str
    filename: str
    relative_path: str  # e.g. "2025/07/06/filename.xml.gz"

    @field_validator("relative_path")
    @classmethod
    def relative_path_safe(cls, v: str) -> str:
        """Reject path traversal attempts, absolute paths, and encoded tricks."""
        normalized = v.replace("\\", "/")
        parts = normalized.split("/")
        if ".." in parts:
            raise ValueError(f"relative_path must not contain '..': {v}")
        if "." in parts:
            raise ValueError(f"relative_path must not contain bare '.': {v}")
        if normalized.startswith("/"):
            raise ValueError(f"relative_path must not be absolute: {v}")
        if "%2e" in normalized.lower() or "%2f" in normalized.lower():
            raise ValueError(f"relative_path must not contain encoded traversal: {v}")
        return v

    created_at: datetime = Field(default_factory=datetime.now)
    size_bytes: int = 0
    sha256: str = ""
    connection_method: str = "ssh"
    pfsense_version: str | None = None
    device_hostname: str | None = None
    changes: list[ChangeCategory] = Field(default_factory=list)
    compressed: bool = False
    sequence: int = 1
    verified: bool = False
    description: str | None = None
    backup_type: BackupType = BackupType.CONFIG
    source_paths: list[str] = Field(default_factory=list)
    zfs_snapshot_name: str | None = None
    zfs_incremental: bool = False
    zfs_base_snapshot: str | None = None

    @property
    def size_human(self) -> str:
        """Human-readable file size."""
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @property
    def changes_label(self) -> str:
        """Short label of changes for display."""
        if not self.changes:
            return "unknown"
        return "+".join(c.value for c in self.changes[:3])

    @property
    def type_label(self) -> str:
        """Short display label for the backup type."""
        return self.backup_type.value


class BackupIndex(BaseModel):
    """Root of the backup_index.json per device."""

    device_id: str
    schema_version: int = 2
    records: list[BackupRecord] = Field(default_factory=list)

    @classmethod
    def migrate(cls, data: dict) -> dict:
        """Migrate older schema versions to current."""
        version = data.get("schema_version", 1)
        if version < 2:
            for record in data.get("records", []):
                if "backup_type" not in record:
                    record["backup_type"] = "config"
                if "source_paths" not in record:
                    record["source_paths"] = []
            data["schema_version"] = 2
        return data

    def add(self, record: BackupRecord) -> None:
        self.records.append(record)

    def remove(self, record_id: str) -> bool:
        original = len(self.records)
        self.records = [r for r in self.records if r.id != record_id]
        return len(self.records) < original

    def get(self, record_id: str) -> BackupRecord | None:
        for r in self.records:
            if r.id == record_id:
                return r
        return None

    def sorted_by_date(self, newest_first: bool = True) -> list[BackupRecord]:
        return sorted(self.records, key=lambda r: r.created_at, reverse=newest_first)

    def sorted_by_type(
        self, backup_type: BackupType, newest_first: bool = True
    ) -> list[BackupRecord]:
        """Return records of a specific type, sorted by date."""
        typed = [r for r in self.records if r.backup_type == backup_type]
        return sorted(typed, key=lambda r: r.created_at, reverse=newest_first)

    def latest(self, backup_type: BackupType | None = None) -> BackupRecord | None:
        if backup_type:
            records = self.sorted_by_type(backup_type)
        else:
            records = self.sorted_by_date()
        return records[0] if records else None

    def count_today(self, device_id: str) -> int:
        today = datetime.now().date()
        return sum(
            1 for r in self.records if r.created_at.date() == today and r.device_id == device_id
        )
