"""Service for downloading additional files from pfSense via SSH."""

from __future__ import annotations

import shlex
import tarfile
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pfsentinel.models.backup import BackupRecord, BackupType
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.connection import SSHConnector
from pfsentinel.services.credentials import CredentialService
from pfsentinel.utils import checksum, compression, naming

ProgressCallback = Callable[[str, int], None]

# Remote paths on pfSense (FreeBSD)
RRD_DIR = "/var/db/rrd"
PKG_CONFIG_DIR = "/usr/local/etc"
DHCP_LEASES_PATH = "/var/dhcpd/var/db/dhcpd.leases"
CERT_DIRS = ["/etc/ssl", "/usr/local/etc/ssl"]
ALIAS_DIR = "/usr/local/share/pfSense/aliases"


class ExtraBackupError(Exception):
    """Raised when an extra backup target fails."""


class ExtraBackupService:
    """Download additional files from pfSense alongside config backups."""

    def __init__(self, config: AppConfig, credential_service: CredentialService) -> None:
        self._config = config
        self._creds = credential_service
        self._backup_root = config.backup_policy.resolved_root

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

            with tempfile.TemporaryDirectory() as tmpdir:
                local_dir = Path(tmpdir)
                downloaded = connector.download_files(rrd_files, local_dir)
                if not downloaded:
                    raise ExtraBackupError("Failed to download any RRD files")

                if progress:
                    progress(f"Downloaded {len(downloaded)} RRD files", 50)

                return self._create_tar_record(
                    device=device,
                    backup_type=BackupType.RRD,
                    local_files=downloaded,
                    source_paths=rrd_files,
                    timestamp=timestamp,
                    sequence=sequence,
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
            rel_path = naming.generate_typed_relative_path("pkg", filename, timestamp)
            dest = self._backup_root / device.id / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)

            cmd = f"tar czf - {shlex.quote(PKG_CONFIG_DIR)}"
            # tar exits 1 on non-fatal issues (permission denied on single files);
            # the archive is still valid with all readable files included.
            bytes_written = connector.stream_command_to_file(
                cmd, dest, timeout=120, warn_exit_codes={1}
            )

            if bytes_written == 0:
                raise ExtraBackupError("No package config data received")

            if progress:
                progress("Package configs downloaded", 80)

            file_hash = checksum.sha256_file(dest)
            return BackupRecord(
                device_id=device.id,
                filename=filename,
                relative_path=rel_path,
                created_at=timestamp,
                size_bytes=dest.stat().st_size,
                sha256=file_hash,
                connection_method="ssh",
                backup_type=BackupType.PACKAGE_CONFIGS,
                source_paths=[PKG_CONFIG_DIR],
                compressed=True,
                sequence=sequence,
                verified=True,
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

        connector = self._get_ssh_connector(device)
        with connector:
            filename = naming.generate_backup_filename(
                device_id=device.id,
                backup_type="dhcp",
                compressed=self._config.backup_policy.compress,
                timestamp=timestamp,
                sequence=sequence,
            )
            rel_path = naming.generate_typed_relative_path("dhcp", filename, timestamp)
            dest = self._backup_root / device.id / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory() as tmpdir:
                local_tmp = Path(tmpdir) / "dhcpd.leases"
                try:
                    connector.download_file(DHCP_LEASES_PATH, local_tmp)
                except Exception as e:
                    raise ExtraBackupError(f"Failed to download DHCP leases: {e}") from e

                if self._config.backup_policy.compress:
                    dest.write_bytes(compression.compress_bytes(local_tmp.read_bytes()))
                else:
                    dest.write_bytes(local_tmp.read_bytes())

            if progress:
                progress("DHCP leases downloaded", 80)

            file_hash = checksum.sha256_file(dest)
            return BackupRecord(
                device_id=device.id,
                filename=filename,
                relative_path=rel_path,
                created_at=timestamp,
                size_bytes=dest.stat().st_size,
                sha256=file_hash,
                connection_method="ssh",
                backup_type=BackupType.DHCP_LEASES,
                source_paths=[DHCP_LEASES_PATH],
                compressed=self._config.backup_policy.compress,
                sequence=sequence,
                verified=True,
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
            # Check common alias locations
            alias_files: list[str] = []
            for check_dir in [ALIAS_DIR, "/usr/local/etc/aliases"]:
                found = connector.list_remote_files(check_dir, "*")
                alias_files.extend(found)

            # Also check for urltable files
            urltable_files = connector.list_remote_files("/var/db/aliastables", "*")
            alias_files.extend(urltable_files)

            if not alias_files:
                raise ExtraBackupError("No alias files found on device")

            with tempfile.TemporaryDirectory() as tmpdir:
                local_dir = Path(tmpdir)
                downloaded = connector.download_files(alias_files, local_dir)
                if not downloaded:
                    raise ExtraBackupError("Failed to download any alias files")

                if progress:
                    progress(f"Downloaded {len(downloaded)} alias files", 50)

                return self._create_tar_record(
                    device=device,
                    backup_type=BackupType.ALIASES,
                    local_files=downloaded,
                    source_paths=alias_files,
                    timestamp=timestamp,
                    sequence=sequence,
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

            with tempfile.TemporaryDirectory() as tmpdir:
                local_dir = Path(tmpdir)
                downloaded = connector.download_files(cert_files, local_dir)
                if not downloaded:
                    raise ExtraBackupError("Failed to download any certificate files")

                if progress:
                    progress(f"Downloaded {len(downloaded)} certificates", 50)

                return self._create_tar_record(
                    device=device,
                    backup_type=BackupType.CERTIFICATES,
                    local_files=downloaded,
                    source_paths=cert_files,
                    timestamp=timestamp,
                    sequence=sequence,
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
        with connector, tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir)
            downloaded = connector.download_files(log_files, local_dir)
            if not downloaded:
                raise ExtraBackupError("Failed to download any log files")

            if progress:
                progress(f"Downloaded {len(downloaded)} log files", 50)

            return self._create_tar_record(
                device=device,
                backup_type=BackupType.LOGS,
                local_files=downloaded,
                source_paths=log_files,
                timestamp=timestamp,
                sequence=sequence,
            )

    def _create_tar_record(
        self,
        device: DeviceConfig,
        backup_type: BackupType,
        local_files: list[Path],
        source_paths: list[str],
        timestamp: datetime,
        sequence: int,
    ) -> BackupRecord:
        """Bundle files into tar.gz, create record, return it."""
        compress = self._config.backup_policy.compress
        filename = naming.generate_backup_filename(
            device_id=device.id,
            backup_type=backup_type.value,
            compressed=compress,
            timestamp=timestamp,
            sequence=sequence,
        )
        rel_path = naming.generate_typed_relative_path(backup_type.value, filename, timestamp)
        dest = self._backup_root / device.id / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Create tar archive (optionally compressed)
        mode = "w:gz" if compress else "w"
        with tarfile.open(dest, mode) as tar:
            for f in local_files:
                tar.add(str(f), arcname=f.name)

        file_hash = checksum.sha256_file(dest)
        return BackupRecord(
            device_id=device.id,
            filename=filename,
            relative_path=rel_path,
            created_at=timestamp,
            size_bytes=dest.stat().st_size,
            sha256=file_hash,
            connection_method="ssh",
            backup_type=backup_type,
            source_paths=source_paths,
            compressed=compress,
            sequence=sequence,
            verified=True,
        )
