"""Application configuration model."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Literal

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


class LongTermRetention(BaseModel):
    """Keep some older backups beyond the normal limits (grandfather-father-son).

    For each listed type, the newest backup of every ISO week in the last
    ``weekly_weeks`` weeks and of every month in the last ``monthly_months``
    months is kept, on top of the normal per-type count and ``keep_days``.
    """

    enabled: bool = True
    weekly_weeks: int = Field(default=12, ge=0, le=520)  # about 3 months
    monthly_months: int = Field(default=12, ge=0, le=120)  # 1 year
    types: list[str] = Field(default_factory=lambda: ["config"])


class SecondCopyPolicy(BaseModel):
    """Second copy of the encrypted backups on a self-hosted target.

    See services/second_copy.py and services/cloud_targets.py. Only encrypted
    files are copied. The s3, azure and webdav kinds are EXPERIMENTAL. Their
    secrets (S3 secret key, Azure SAS token or account key, WebDAV password)
    live in the credential store, never here.
    """

    enabled: bool = False
    kind: Literal["folder", "sftp", "s3", "azure", "webdav"] = "folder"
    # folder: local path. sftp: remote directory.
    path: str | None = None
    host: str | None = None
    port: int = Field(default=22, ge=1, le=65535)
    # sftp and webdav login name.
    username: str | None = None
    key_path: str | None = None
    keep_days: int = Field(default=90, ge=1, le=3650)
    # s3: https://host[:port] of any S3-compatible service.
    endpoint: str | None = None
    region: str | None = None
    bucket: str | None = None
    path_style: bool = False
    access_key_id: str | None = None
    # azure: storage account, container, and "sas" or "key" (Shared Key).
    account: str | None = None
    container: str | None = None
    azure_auth: Literal["sas", "key"] = "sas"
    # Default https://<account>.blob.core.windows.net
    azure_endpoint: str | None = None
    # s3 and azure: folder inside the bucket or container.
    prefix: str | None = None
    # webdav: base URL of the target folder.
    url: str | None = None
    # webdav only: plain http for a server on the LAN.
    allow_http: bool = False
    # s3, azure, webdav: CA file for a server with a private certificate.
    ca_cert_path: str | None = None


class EncryptionPolicy(BaseModel):
    """Encryption at rest for backup files. See services/backup_crypto.py.

    Only the key ID and the scrypt salt live here; the key or passphrase is
    in the OS keystore.
    """

    mode: Literal["off", "keystore", "passphrase"] = "off"
    key_id: str | None = None  # hex, identifies the current key
    salt: str | None = None  # hex, passphrase mode only


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
    encryption: EncryptionPolicy = Field(default_factory=EncryptionPolicy)
    long_term: LongTermRetention = Field(default_factory=LongTermRetention)
    second_copy: SecondCopyPolicy = Field(default_factory=SecondCopyPolicy)
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
        """Return the configured root, or the platform default if not set."""
        if self.backup_root is not None:
            return self.backup_root.expanduser()
        from pfsentinel.utils.platform import default_backup_dir

        return default_backup_dir()


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
    # Windows Event Log (Application, source "pfSentinel") or syslog/journald
    # on Linux. Kept under its first name for existing configs.
    windows_event_log_enabled: bool = False
    notify_on_success: bool = True
    # Failures and alerts: failed backup, risky change, failed restore check,
    # no recent backup, failed second copy.
    notify_on_failure: bool = True
    # Alert when a device has had no successful backup for this many days.
    stale_after_days: int = Field(default=3, ge=1, le=365)
    # Alert once when a new pfSentinel release is out (checked by the daily task).
    update_alerts: bool = True
    # Linux desktop notifications through notify-send (freedesktop). Like the
    # Windows toast: on by default, and a silent no-op without a desktop session.
    linux_desktop_enabled: bool = True
    # Email (SMTP) to any mail server. The password is in the OS keystore
    # (CredentialService secret "__smtp_password__"), never here.
    email_enabled: bool = False
    email_host: str | None = None
    email_port: int = Field(default=587, ge=1, le=65535)
    # starttls: plain connection upgraded to TLS (usually port 587).
    # tls: TLS from the first byte (usually port 465). none: no encryption,
    # only for a local server such as Mailpit, and never with a login.
    email_security: Literal["starttls", "tls", "none"] = "starttls"
    email_username: str | None = None
    email_from: str | None = None
    email_to: list[str] = Field(default_factory=list)
    email_timeout: int = Field(default=20, ge=1, le=300)
    # Only for a lab server with a self-signed certificate.
    email_skip_tls_verify: bool = False
    # One summary a day instead of a message per successful backup. Failures
    # and risky changes are still sent at once. Sent by the daily check task
    # on its first run at or after digest_time (HH:MM, local time).
    digest_enabled: bool = False
    digest_time: str = "08:00"


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
            # Windows: the folder holds config, pinned host keys and the secret
            # store; make it owner-only like 0700 on Linux.
            from pfsentinel.utils.permissions import restrict_to_owner

            restrict_to_owner(p.parent)
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
