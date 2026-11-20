"""Filesystem tar archive backup service (non-ZFS fallback)."""

from __future__ import annotations

import shlex
from collections.abc import Callable

from pfsentinel.models.backup import BackupRecord, BackupRun, BackupType
from pfsentinel.models.config import AppConfig
from pfsentinel.services import backup_crypto
from pfsentinel.services.backup_crypto import BackupCrypto
from pfsentinel.services.connection import SSHConnector
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.retention import RetentionService
from pfsentinel.utils import checksum, naming
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

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
        self._crypto = BackupCrypto(config.backup_policy.encryption, credential_service)

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
        run: BackupRun | None = None,
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

        if run is None:
            run = RetentionService(self._backup_root, self._config.backup_policy).start_run(
                device.id
            )
        timestamp = run.started_at
        filename = naming.generate_backup_filename(
            device_id=device.id,
            backup_type="archive",
            compressed=True,
            timestamp=timestamp,
            sequence=run.number,
        )
        dest = self._backup_root / device.id / run.relative_path(filename)

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
            with self._crypto.create(dest) as (sink, dest):
                bytes_written = connector.stream_command_to_file(
                    cmd, dest, timeout=600, warn_exit_codes={1}, sink=sink
                )
                if bytes_written == 0:
                    raise ArchiveBackupError("No archive data received from device")

        if progress:
            progress("Calculating checksum...", 85)

        file_hash = checksum.sha256_file(dest)

        if progress:
            progress("Archive backup complete!", 100)

        logger.info(f"Archive backup complete: {dest.name} ({bytes_written} bytes)")
        return BackupRecord(
            device_id=device.id,
            filename=dest.name,
            relative_path=run.relative_path(dest.name),
            created_at=timestamp,
            size_bytes=dest.stat().st_size,
            sha256=file_hash,
            connection_method="ssh",
            backup_type=BackupType.FS_ARCHIVE,
            source_paths=dirs,
            compressed=True,
            sequence=run.number,
            verified=True,
            run_number=run.number,
            encrypted=backup_crypto.is_encrypted(dest),
        )
