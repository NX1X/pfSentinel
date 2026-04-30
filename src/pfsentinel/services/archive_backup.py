"""Filesystem tar archive backup service (non-ZFS fallback)."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from datetime import datetime

from loguru import logger

from pfsentinel.models.backup import BackupRecord, BackupType
from pfsentinel.models.config import AppConfig
from pfsentinel.services.connection import SSHConnector
from pfsentinel.services.credentials import CredentialService
from pfsentinel.utils import checksum, naming

ProgressCallback = Callable[[str, int], None]


class ArchiveBackupError(Exception):
    """Raised when archive backup operations fail."""


class ArchiveBackupService:
    """Create tar archives of critical pfSense directories via SSH."""

    def __init__(self, config: AppConfig, credential_service: CredentialService) -> None:
        self._config = config
        self._creds = credential_service
        self._backup_root = config.backup_policy.resolved_root
        self._policy = config.backup_policy.archive

    def _get_ssh_connector(self, device_id: str) -> SSHConnector:
        device = self._config.get_device(device_id)
        if not device:
            raise ArchiveBackupError(f"Device '{device_id}' not found")
        password = self._creds.get(device.id)
        passphrase = self._creds.get_ssh_key_passphrase(device.id)
        return SSHConnector(device, password, ssh_key_passphrase=passphrase)

    def run_archive_backup(
        self,
        device_id: str,
        directories: list[str] | None = None,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord:
        """Create tar archive on remote device and stream to local storage.

        Uses: tar czf - {dirs} piped over SSH to local file.
        """
        device = self._config.get_device(device_id)
        if not device:
            raise ArchiveBackupError(f"Device '{device_id}' not found")

        dirs = directories or self._policy.directories
        if not dirs:
            raise ArchiveBackupError("No directories configured for archive backup")

        timestamp = datetime.now()
        filename = naming.generate_backup_filename(
            device_id=device.id,
            backup_type="archive",
            compressed=True,
            timestamp=timestamp,
            sequence=1,
        )
        rel_path = naming.generate_typed_relative_path("archive", filename, timestamp)
        dest = self._backup_root / device.id / rel_path

        # Build tar command with exclude patterns (shell-escaped)
        excludes = " ".join(f"--exclude={shlex.quote(p)}" for p in self._policy.exclude_patterns)
        dirs_str = " ".join(shlex.quote(d) for d in dirs)
        cmd = f"tar czf - {excludes} {dirs_str}"

        connector = self._get_ssh_connector(device_id)
        with connector:
            if progress:
                progress("Creating filesystem archive...", 10)

            # tar exits 1 on non-fatal issues (permission denied, missing files);
            # the archive is still valid with all readable files included.
            bytes_written = connector.stream_command_to_file(
                cmd, dest, timeout=600, warn_exit_codes={1}
            )
            if bytes_written == 0:
                raise ArchiveBackupError("No archive data received from device")

        if progress:
            progress("Calculating checksum...", 85)

        file_hash = checksum.sha256_file(dest)

        if progress:
            progress("Archive backup complete!", 100)

        logger.info(f"Archive backup complete: {filename} ({bytes_written} bytes)")
        return BackupRecord(
            device_id=device.id,
            filename=filename,
            relative_path=rel_path,
            created_at=timestamp,
            size_bytes=dest.stat().st_size,
            sha256=file_hash,
            connection_method="ssh",
            backup_type=BackupType.FS_ARCHIVE,
            source_paths=dirs,
            compressed=True,
            sequence=1,
            verified=True,
        )
