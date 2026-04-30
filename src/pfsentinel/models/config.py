"""Application configuration model."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from .device import DeviceConfig


class ExtraBackupTargets(BaseModel):
    """Which additional files to back up via SSH alongside config."""

    rrd: bool = False
    package_configs: bool = False
    dhcp_leases: bool = False
    aliases: bool = False
    certificates: bool = False
    logs: bool = False
    log_files: list[str] = Field(
        default_factory=lambda: ["/var/log/filter.log", "/var/log/system.log"]
    )
    custom_paths: list[str] = Field(default_factory=list)

    def enabled_targets(self) -> list[str]:
        """Return list of enabled target names."""
        targets = []
        if self.rrd:
            targets.append("rrd")
        if self.package_configs:
            targets.append("pkg")
        if self.dhcp_leases:
            targets.append("dhcp")
        if self.aliases:
            targets.append("aliases")
        if self.certificates:
            targets.append("certs")
        if self.logs:
            targets.append("logs")
        return targets


class ZfsPolicy(BaseModel):
    """ZFS snapshot backup settings."""

    enabled: bool = False
    dataset: str = "zroot/ROOT"
    incremental: bool = True
    cleanup_remote: bool = True
    max_snapshots_remote: int = Field(default=3, ge=1, le=50)


class ArchivePolicy(BaseModel):
    """Filesystem tar archive settings (non-ZFS fallback)."""

    enabled: bool = False
    directories: list[str] = Field(
        default_factory=lambda: [
            "/cf/conf",
            "/usr/local/etc",
            "/var/db/rrd",
            "/boot/loader.conf",
            "/boot/loader.conf.local",
        ]
    )
    exclude_patterns: list[str] = Field(default_factory=lambda: ["*.core", "*.tmp"])


class BackupPolicy(BaseModel):
    backup_root: Path | None = Field(default=None)
    max_backups_per_device: int = Field(default=30, ge=1, le=1000)
    compress: bool = True
    validate_after_backup: bool = True
    keep_days: int = Field(default=30, ge=1)
    # Overwrite file with zeros before deletion (slow but prevents recovery)
    secure_delete: bool = False
    extras: ExtraBackupTargets = Field(default_factory=ExtraBackupTargets)
    zfs: ZfsPolicy = Field(default_factory=ZfsPolicy)
    archive: ArchivePolicy = Field(default_factory=ArchivePolicy)
    max_backups_per_type: dict[str, int] = Field(
        default_factory=lambda: {
            "config": 30,
            "rrd": 10,
            "pkg": 10,
            "dhcp": 10,
            "aliases": 10,
            "certs": 10,
            "logs": 7,
            "zfs": 5,
            "archive": 5,
        }
    )

    model_config = {"arbitrary_types_allowed": True}

    @property
    def resolved_root(self) -> Path:
        """Return the configured root, or the default if not set."""
        if self.backup_root is not None:
            return self.backup_root.expanduser()
        return Path.home() / "Documents" / "pfSentinel"


class ScheduleConfig(BaseModel):
    enabled: bool = False
    daily_enabled: bool = True
    daily_time: str = "02:00"  # HH:MM
    weekly_enabled: bool = False
    weekly_day: str = "sunday"
    weekly_time: str = "03:00"
    use_windows_task_scheduler: bool = True


class NotificationConfig(BaseModel):
    telegram_enabled: bool = False
    telegram_chat_id: str | None = None
    # telegram_bot_token stored in keyring, not here
    slack_enabled: bool = False
    # slack_webhook_url stored in keyring, not here
    windows_toast_enabled: bool = True
    windows_event_log_enabled: bool = False
    notify_on_success: bool = True
    notify_on_failure: bool = True


class SyslogConfig(BaseModel):
    """Forward log output to a remote syslog server (UDP)."""

    enabled: bool = False
    host: str = "localhost"
    port: int = Field(default=514, ge=1, le=65535)
    # syslog facility: 1=user, 16=local0 … 23=local7
    facility: int = Field(default=1, ge=0, le=23)


class AppConfig(BaseModel):
    """Root application config - written to ~/.pfsentinel/config.json"""

    schema_version: int = 1
    devices: list[DeviceConfig] = Field(default_factory=list)
    backup_policy: BackupPolicy = Field(default_factory=BackupPolicy)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    syslog: SyslogConfig = Field(default_factory=SyslogConfig)
    log_level: str = "INFO"
    debug: bool = False

    model_config = {"arbitrary_types_allowed": True}

    @staticmethod
    def config_path() -> Path:
        return Path.home() / ".pfsentinel" / "config.json"

    @classmethod
    def load(cls) -> AppConfig:
        p = cls.config_path()
        try:
            return cls.model_validate_json(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except Exception:
            return cls()

    def save(self) -> None:
        p = self.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # model_dump_json() serializes Path → string and None → null correctly
        data = json.loads(self.model_dump_json())
        # Ensure backup_root is null (not "None") when not configured
        data["backup_policy"]["backup_root"] = (
            str(self.backup_policy.backup_root)
            if self.backup_policy.backup_root is not None
            else None
        )
        content = json.dumps(data, indent=2)

        # Atomic write: write to temp file then replace to prevent corruption
        fd, tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            # Set restrictive permissions BEFORE writing content (Unix only)
            if sys.platform != "win32":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, p)
        except BaseException:
            # Clean up temp file on failure
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def get_device(self, device_id: str) -> DeviceConfig | None:
        for d in self.devices:
            if d.id == device_id:
                return d
        return None

    def add_device(self, device: DeviceConfig) -> None:
        if self.get_device(device.id):
            raise ValueError(f"Device '{device.id}' already exists")
        self.devices.append(device)

    def remove_device(self, device_id: str) -> bool:
        original = len(self.devices)
        self.devices = [d for d in self.devices if d.id != device_id]
        return len(self.devices) < original

    def enabled_devices(self) -> list[DeviceConfig]:
        return [d for d in self.devices if d.enabled]
