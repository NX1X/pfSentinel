"""Service for downloading additional files from pfSense via SSH."""

from __future__ import annotations

import io
import shlex
import tarfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pfsentinel.models.backup import BackupRecord, BackupType
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services import backup_crypto
from pfsentinel.services.backup_crypto import BackupCrypto
from pfsentinel.services.connection import SSHConnector
from pfsentinel.services.credentials import CredentialService
from pfsentinel.utils import checksum, compression, naming
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

ProgressCallback = Callable[[str, int], None]

# Remote paths on pfSense (FreeBSD)
RRD_DIR = "/var/db/rrd"
PKG_CONFIG_DIR = "/usr/local/etc"
DHCP_LEASES_PATH = "/var/dhcpd/var/db/dhcpd.leases"
CERT_DIRS = ["/etc/ssl", "/usr/local/etc/ssl"]
ALIAS_DIR = "/usr/local/share/pfSense/aliases"


class ExtraBackupError(Exception):
    """Raised when an extra backup target fails."""


class NeedsRootError(ExtraBackupError):
    """The SSH user cannot read any file of this target (they are root only).

    Expected with a non-root backup user, so it is reported as a skip, not
    as a failure.
    """


class ExtraBackupService:
    """Download additional files from pfSense alongside config backups."""

    def __init__(self, config: AppConfig, credential_service: CredentialService) -> None:
        self._config = config
        self._creds = credential_service
        self._backup_root = config.backup_policy.resolved_root
        self._crypto = BackupCrypto(config.backup_policy.encryption, credential_service)

    def _get_ssh_connector(self, device: DeviceConfig) -> SSHConnector:
        password = self._creds.get(device.id)
        passphrase = self._creds.get_ssh_key_passphrase(device.id)
        return SSHConnector(device, password, ssh_key_passphrase=passphrase)

    def backup_target(
        self,
        target: str,
        device: DeviceConfig,
        timestamp: datetime,
        sequence: int,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord:
        """Run backup for a specific extra target by name."""
        dispatch = {
            "rrd": self.backup_rrd,
            "pkg": self.backup_package_configs,
            "dhcp": self.backup_dhcp_leases,
            "aliases": self.backup_aliases,
            "certs": self.backup_certificates,
            "logs": self.backup_logs,
        }
        handler = dispatch.get(target)
        if not handler:
            raise ExtraBackupError(f"Unknown extra backup target: {target}")
        return handler(device, timestamp, sequence, progress)

    def backup_rrd(
        self,
        device: DeviceConfig,
        timestamp: datetime,
        sequence: int,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord:
        """Download all RRD files and bundle into tar.gz."""
        if progress:
            progress("Downloading RRD data...", 0)

        connector = self._get_ssh_connector(device)
        with connector:
            rrd_files = connector.list_remote_files(RRD_DIR, "*.rrd")
            if not rrd_files:
                raise ExtraBackupError("No RRD files found on device")
            files = connector.read_files(rrd_files)
            if not files:
                raise ExtraBackupError("Failed to download any RRD files")
            if progress:
                progress(f"Downloaded {len(files)} RRD files", 50)
            return self._create_tar_record(
                device, BackupType.RRD, files, rrd_files, timestamp, sequence
            )

    def backup_package_configs(
        self,
        device: DeviceConfig,
        timestamp: datetime,
        sequence: int,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord:
        """Download package configuration files from /usr/local/etc/."""
        if progress:
            progress("Downloading package configs...", 0)

        connector = self._get_ssh_connector(device)
        with connector:
            # Use tar on the remote side to capture the directory tree
            filename = naming.generate_backup_filename(
                device_id=device.id,
                backup_type="pkg",
                compressed=True,
                timestamp=timestamp,
                sequence=sequence,
            )
            dest = (
                self._backup_root
                / device.id
                / naming.run_relative_path(filename, timestamp, sequence)
            )
            cmd = f"tar czf - {shlex.quote(PKG_CONFIG_DIR)}"
            with self._crypto.create(dest) as (sink, final):
                # tar exits 1 on non-fatal issues (permission denied on single
                # files); the archive still holds every readable file.
                bytes_written = connector.stream_command_to_file(
                    cmd, final, timeout=120, warn_exit_codes={1}, sink=sink
                )
                if bytes_written == 0:
                    raise ExtraBackupError("No package config data received")

            if progress:
                progress("Package configs downloaded", 80)
            return self._record(
                device,
                BackupType.PACKAGE_CONFIGS,
                final,
                [PKG_CONFIG_DIR],
                True,
                timestamp,
                sequence,
            )

    def backup_dhcp_leases(
        self,
        device: DeviceConfig,
        timestamp: datetime,
        sequence: int,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord:
        """Download DHCP lease file."""
        if progress:
            progress("Downloading DHCP leases...", 0)

        compress = self._config.backup_policy.compress
        connector = self._get_ssh_connector(device)
        with connector:
            filename = naming.generate_backup_filename(
                device_id=device.id,
                backup_type="dhcp",
                compressed=compress,
                timestamp=timestamp,
                sequence=sequence,
            )
            dest = (
                self._backup_root
                / device.id
                / naming.run_relative_path(filename, timestamp, sequence)
            )
            try:
                data = connector.read_file(DHCP_LEASES_PATH)
            except Exception as e:
                raise ExtraBackupError(f"Failed to download DHCP leases: {e}") from e
            final = self._crypto.write_bytes(
                dest, compression.compress_bytes(data) if compress else data
            )

        if progress:
            progress("DHCP leases downloaded", 80)
        return self._record(
            device, BackupType.DHCP_LEASES, final, [DHCP_LEASES_PATH], compress, timestamp, sequence
        )

    def backup_aliases(
        self,
        device: DeviceConfig,
        timestamp: datetime,
        sequence: int,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord:
        """Download alias / URL table files."""
        if progress:
            progress("Downloading alias files...", 0)

        connector = self._get_ssh_connector(device)
        with connector:
            alias_files: list[str] = []
            # Which of these exist depends on the pfSense version (2.8.1 has
            # only /var/db/aliastables), so a missing one is not a warning.
            for check_dir in [ALIAS_DIR, "/usr/local/etc/aliases", "/var/db/aliastables"]:
                alias_files.extend(connector.list_remote_files(check_dir, "*", missing_ok=True))
            if not alias_files:
                raise ExtraBackupError("No alias files found on device")

            files = connector.read_files(alias_files)
            if not files:
                raise ExtraBackupError("Failed to download any alias files")
            if progress:
                progress(f"Downloaded {len(files)} alias files", 50)
            return self._create_tar_record(
                device, BackupType.ALIASES, files, alias_files, timestamp, sequence
            )

    def backup_certificates(
        self,
        device: DeviceConfig,
        timestamp: datetime,
        sequence: int,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord:
        """Download SSL/TLS certificates."""
        if progress:
            progress("Downloading certificates...", 0)

        connector = self._get_ssh_connector(device)
        with connector:
            cert_files: list[str] = []
            for cert_dir in CERT_DIRS:
                for pattern in ["*.pem", "*.crt", "*.key", "*.csr"]:
                    cert_files.extend(connector.list_remote_files(cert_dir, pattern))
            if not cert_files:
                raise ExtraBackupError("No certificate files found on device")

            # In memory only: these include private keys.
            files = connector.read_files(cert_files)
            if not files:
                raise ExtraBackupError("Failed to download any certificate files")
            if progress:
                progress(f"Downloaded {len(files)} certificates", 50)
            return self._create_tar_record(
                device, BackupType.CERTIFICATES, files, cert_files, timestamp, sequence
            )

    def backup_logs(
        self,
        device: DeviceConfig,
        timestamp: datetime,
        sequence: int,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord:
        """Download specified log files."""
        if progress:
            progress("Downloading log files...", 0)

        log_files = self._config.backup_policy.extras.log_files
        if not log_files:
            raise ExtraBackupError("No log files configured for backup")

        connector = self._get_ssh_connector(device)
        with connector:
            files = connector.read_files(log_files)
            denied = list(connector.permission_denied)
            if not files and denied:
                raise NeedsRootError(
                    f"logs need root: {', '.join(denied)} "
                    f"cannot be read by SSH user '{device.username}'. Skipped."
                )
            if not files:
                raise ExtraBackupError("Failed to download any log files")
            if denied:
                logger.info("Skipped log files that need root: " + ", ".join(denied))
            if progress:
                progress(f"Downloaded {len(files)} log files", 50)
            return self._create_tar_record(
                device, BackupType.LOGS, files, log_files, timestamp, sequence
            )

    def _create_tar_record(
        self,
        device: DeviceConfig,
        backup_type: BackupType,
        files: list[tuple[str, bytes]],
        source_paths: list[str],
        timestamp: datetime,
        sequence: int,
    ) -> BackupRecord:
        """Bundle in-memory files into a tar (.gz), written (and encrypted) in one pass."""
        compress = self._config.backup_policy.compress
        filename = naming.generate_backup_filename(
            device_id=device.id,
            backup_type=backup_type.value,
            compressed=compress,
            timestamp=timestamp,
            sequence=sequence,
        )
        dest = (
            self._backup_root / device.id / naming.run_relative_path(filename, timestamp, sequence)
        )
        with self._crypto.create(dest) as (sink, final):
            # Stream mode: writes front to back, which an encrypting sink needs.
            with tarfile.open(fileobj=sink, mode="w|gz" if compress else "w|") as tar:
                for name, data in files:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o600
                    info.mtime = int(timestamp.timestamp())
                    tar.addfile(info, io.BytesIO(data))
        return self._record(device, backup_type, final, source_paths, compress, timestamp, sequence)

    def _record(
        self,
        device: DeviceConfig,
        backup_type: BackupType,
        path: Path,
        source_paths: list[str],
        compressed: bool,
        timestamp: datetime,
        sequence: int,
    ) -> BackupRecord:
        return BackupRecord(
            device_id=device.id,
            filename=path.name,
            relative_path=naming.run_relative_path(path.name, timestamp, sequence),
            created_at=timestamp,
            size_bytes=path.stat().st_size,
            sha256=checksum.sha256_file(path),
            connection_method="ssh",
            backup_type=backup_type,
            source_paths=source_paths,
            compressed=compressed,
            sequence=sequence,
            verified=True,
            run_number=sequence,
            encrypted=backup_crypto.is_encrypted(path),
        )
