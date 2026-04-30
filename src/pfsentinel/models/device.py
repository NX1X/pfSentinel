"""Device configuration models."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

# Shared device ID pattern — used by models and CLI validation
DEVICE_ID_PATTERN = r"^[a-z0-9][a-z0-9\-]{0,62}$"


class ConnectionMethod(StrEnum):
    SSH = "ssh"
    HTTPS = "https"
    HTTP = "http"


class DeviceConfig(BaseModel):
    """One pfSense firewall instance."""

    id: str = Field(..., pattern=DEVICE_ID_PATTERN)
    label: str
    host: str
    primary_method: ConnectionMethod = ConnectionMethod.SSH
    fallback_method: ConnectionMethod | None = ConnectionMethod.HTTPS
    ssh_port: int = Field(default=22, ge=1, le=65535)
    http_port: int = Field(default=80, ge=1, le=65535)
    https_port: int = Field(default=443, ge=1, le=65535)
    username: str = "admin"
    # Password is NOT stored here - use CredentialService (keyring)
    # SSH key path for key-based auth (optional, overrides password for SSH)
    ssh_key_path: Path | None = None
    # SSL certificate verification for HTTPS connections.
    # Set to False only for self-signed certificates (use --no-verify-ssl when adding a device).
    verify_ssl: bool = True
    # Path to a CA certificate file for verifying self-signed HTTPS certs.
    # When set, this is used instead of disabling verify_ssl entirely.
    ca_cert_path: Path | None = None
    # Reject SSH connections to hosts not in known_hosts.
    # False (default) logs a warning but connects anyway (suitable for homelabs).
    # True uses RejectPolicy — the host must be in ~/.ssh/known_hosts.
    strict_host_keys: bool = False
    timeout: int = Field(default=30, ge=5, le=300)
    enabled: bool = True

    model_config = {"arbitrary_types_allowed": True}

    @field_validator("host")
    @classmethod
    def host_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("host cannot be empty")
        return v.strip()

    @field_validator("id", mode="before")
    @classmethod
    def id_lowercase(cls, v: str) -> str:
        return str(v).lower()

    @field_validator("ssh_key_path", mode="after")
    @classmethod
    def ssh_key_path_not_dir(cls, v: Path | None) -> Path | None:
        if v is not None and v.is_dir():
            raise ValueError(f"ssh_key_path must be a file, not a directory: {v}")
        return v

    def connection_url(self, method: ConnectionMethod | None = None) -> str:
        m = method or self.primary_method
        if m == ConnectionMethod.SSH:
            return f"ssh://{self.host}:{self.ssh_port}"
        elif m == ConnectionMethod.HTTPS:
            return f"https://{self.host}:{self.https_port}"
        else:
            return f"http://{self.host}:{self.http_port}"  # DevSkim: ignore DS137138  # noqa: E501


class DeviceStatus(BaseModel):
    """Runtime connection status (not persisted)."""

    device_id: str
    ssh_reachable: bool = False
    https_reachable: bool = False
    http_reachable: bool = False
    ssh_error: str | None = None
    https_error: str | None = None
    http_error: str | None = None
    last_backup_at: str | None = None
    last_backup_file: str | None = None
    pfsense_version: str | None = None
    hostname: str | None = None
    error: str | None = None

    @property
    def any_reachable(self) -> bool:
        return self.ssh_reachable or self.https_reachable or self.http_reachable

    @property
    def best_method(self) -> ConnectionMethod | None:
        if self.ssh_reachable:
            return ConnectionMethod.SSH
        if self.https_reachable:
            return ConnectionMethod.HTTPS
        if self.http_reachable:
            return ConnectionMethod.HTTP
        return None
