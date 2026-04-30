"""BackupService - orchestrates the full backup lifecycle."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from loguru import logger

from pfsentinel.models.backup import BackupRecord
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.connection import ConnectionManager
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.diff import DiffService
from pfsentinel.services.notifications import NotificationService
from pfsentinel.services.retention import RetentionService
from pfsentinel.utils import checksum, compression, naming, xml_parser

ProgressCallback = Callable[[str, int], None]


def _secure_wipe(path: Path, passes: int = 3) -> None:
    """Overwrite a file with multiple passes before deletion.

    Uses a 3-pass pattern (zeros, ones, zeros) with fsync to flush to disk.
    Note: on SSDs, wear-levelling may retain old data regardless.
    """
    try:
        size = path.stat().st_size
        patterns = [b"\x00", b"\xff", b"\x00"]
        for i in range(passes):
            with path.open("r+b") as f:
                f.write(patterns[i % len(patterns)] * size)
                f.flush()
                os.fsync(f.fileno())
        logger.debug(f"Secure-wiped {path.name} ({size} bytes, {passes} passes)")
    except Exception as e:
        logger.warning(f"Secure wipe failed for {path.name}: {e}")


class BackupError(Exception):
    """Raised when a backup operation fails."""


class BackupService:
    """Orchestrates backup lifecycle for all devices."""

    def __init__(self, config: AppConfig, credential_service: CredentialService) -> None:
        self._config = config
        self._creds = credential_service
        self._backup_root = config.backup_policy.resolved_root
        self._diff = DiffService(self._backup_root)
        self._notifications = NotificationService(config.notifications, credential_service)

    def _retention(self) -> RetentionService:
        return RetentionService(self._backup_root, self._config.backup_policy)

    def _connection_manager(self, device: DeviceConfig) -> ConnectionManager:
        return ConnectionManager(device, self._creds)

    def run_backup(
        self,
        device_id: str,
        description: str | None = None,
        progress: ProgressCallback | None = None,
        area: str = "",
        no_packages: bool = False,
    ) -> BackupRecord:
        """Run a full (or selective) backup for one device.

        Args:
            area: Specific config section (HTTPS only; empty = full backup).
            no_packages: Exclude package config from backup (HTTPS only).
        """

        def _p(msg: str, pct: int) -> None:
            if progress:
                progress(msg, pct)
            logger.debug(f"[{device_id}] {pct}% - {msg}")

        device = self._config.get_device(device_id)
        if not device:
            raise BackupError(f"Device '{device_id}' not found in config")
        if not device.enabled:
            raise BackupError(f"Device '{device_id}' is disabled")

        retention = self._retention()
        index = retention.load_index(device_id)

        try:
            # --- Connect and download ---
            _p("Connecting to pfSense...", 10)
            cm = self._connection_manager(device)
            xml_content, method_used = cm.download_config(_p, area=area, no_packages=no_packages)

            # --- Validate XML ---
            _p("Validating configuration XML...", 40)
            try:
                xml_parser.validate_xml(xml_content)
            except xml_parser.PfSenseXMLError as e:
                raise BackupError(f"Downloaded config is not valid pfSense XML: {e}") from e

            info = xml_parser.extract_info(xml_content)
            pfsense_version = info.get("pfsense_version")
            hostname = info.get("hostname")

            # --- Detect changes ---
            _p("Detecting changes...", 55)
            changes = self._diff.detect(device_id, xml_content, index)

            # --- Generate filename ---
            sequence = retention.next_sequence(device_id)
            timestamp = datetime.now()
            filename = naming.generate_filename(
                device_id=device_id,
                changes=changes,
                sequence=sequence,
                compressed=self._config.backup_policy.compress,
                timestamp=timestamp,
            )
            relative_path = naming.generate_relative_path(filename, timestamp)
            dest_path = self._backup_root / device_id / relative_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # --- Save backup ---
            _p("Saving backup...", 65)
            # Restrict file permissions on Unix (owner-only)
            old_umask = os.umask(0o077) if sys.platform != "win32" else None
            try:
                if self._config.backup_policy.compress:
                    dest_path.write_bytes(compression.compress_bytes(xml_content.encode("utf-8")))
                else:
                    dest_path.write_text(xml_content, encoding="utf-8")
            finally:
                if old_umask is not None:
                    os.umask(old_umask)

            file_size = dest_path.stat().st_size

            # --- Checksum ---
            _p("Calculating checksum...", 75)
            file_hash = checksum.sha256_file(dest_path)

            # --- Verify if requested ---
            if self._config.backup_policy.validate_after_backup:
                _p("Verifying backup...", 80)
                try:
                    read_back = compression.read_xml(dest_path)
                    xml_parser.validate_xml(read_back)
                    verified = True
                except Exception as e:
                    logger.warning(f"Post-backup validation failed: {e}")
                    verified = False
            else:
                verified = True

            # --- Create record ---
            record = BackupRecord(
                device_id=device_id,
                filename=filename,
                relative_path=relative_path,
                created_at=timestamp,
                size_bytes=file_size,
                sha256=file_hash,
                connection_method=method_used,
                pfsense_version=pfsense_version,
                device_hostname=hostname,
                changes=changes,
                compressed=self._config.backup_policy.compress,
                sequence=sequence,
                verified=verified,
                description=description,
            )

            # --- Update index ---
            _p("Updating backup index...", 85)
            index.add(record)
            retention.save_index(index)

            # --- Retention cleanup ---
            _p("Applying retention policy...", 90)
            retention.apply(device_id)

            # --- Notify ---
            _p("Sending notifications...", 95)
            self._notifications.notify_success(record)

            _p("Backup complete!", 100)
            logger.info(f"Backup complete: {filename}")
            return record

        except Exception as e:
            self._notifications.notify_failure(device_id, str(e))
            raise

    def run_all_backups(self, progress: ProgressCallback | None = None) -> list[BackupRecord]:
        """Run backups for all enabled devices."""
        results = []
        devices = self._config.enabled_devices()
        if not devices:
            raise BackupError("No enabled devices configured")

        for device in devices:
            try:
                record = self.run_backup(device.id, progress=progress)
                results.append(record)
            except Exception as e:
                logger.error(f"Backup failed for {device.id}: {e}")

        return results

    def list_backups(self, device_id: str | None = None) -> list[BackupRecord]:
        """List backups for one device or all devices."""
        retention = self._retention()
        if device_id:
            return retention.load_index(device_id).sorted_by_date()

        all_records = []
        for device in self._config.devices:
            index = retention.load_index(device.id)
            all_records.extend(index.records)

        return sorted(all_records, key=lambda r: r.created_at, reverse=True)

    def verify_backup(self, record: BackupRecord) -> bool:
        """Verify backup file integrity via checksum and type-specific validation.

        Raises BackupError with a human-readable reason on failure.
        Returns True on success.
        """
        from pfsentinel.models.backup import BackupType

        path = self._backup_root / record.device_id / record.relative_path
        if not path.exists():
            raise BackupError(f"Backup file not found: {path.name}")

        # Checksum check (all types)
        if record.sha256 and not checksum.verify_file(path, record.sha256):
            raise BackupError(
                f"Checksum mismatch for '{record.filename}'"
                " — file may be corrupted or tampered with"
            )

        # Type-specific validation
        if record.backup_type == BackupType.CONFIG:
            try:
                xml = compression.read_xml(path)
                xml_parser.validate_xml(xml)
            except Exception as e:
                raise BackupError(f"XML validation failed for '{record.filename}': {e}") from e
        elif record.backup_type in (
            BackupType.RRD,
            BackupType.PACKAGE_CONFIGS,
            BackupType.CERTIFICATES,
            BackupType.LOGS,
            BackupType.ALIASES,
            BackupType.FS_ARCHIVE,
        ):
            self._verify_tar(path, record.compressed)
        # ZFS_SNAPSHOT and DHCP_LEASES: checksum is sufficient

        return True

    def _verify_tar(self, path: Path, compressed: bool) -> None:
        """Verify a tar archive is readable."""
        import gzip
        import tarfile

        try:
            if compressed:
                with gzip.open(path, "rb") as gz:
                    with tarfile.open(fileobj=gz, mode="r:") as tar:
                        tar.getnames()
            else:
                with tarfile.open(path, "r") as tar:
                    tar.getnames()
        except Exception as e:
            raise BackupError(f"Tar archive verification failed for '{path.name}': {e}") from e

    def delete_backup(self, record: BackupRecord) -> None:
        """Delete a backup file and remove from index.

        If secure_delete is enabled in backup policy, the file is overwritten
        with zeros before deletion to prevent recovery.
        """
        retention = self._retention()
        index = retention.load_index(record.device_id)

        path = self._backup_root / record.device_id / record.relative_path
        if path.is_symlink():
            raise BackupError(f"Refusing to delete symlink (potential attack): {path.name}")
        if path.exists():
            if self._config.backup_policy.secure_delete:
                _secure_wipe(path)
            path.unlink()

        index.remove(record.id)
        retention.save_index(index)
        logger.info(f"Deleted backup: {record.filename}")

    def restore_backup(self, record: BackupRecord, target_path: Path) -> Path:
        """Restore a backup file to a target path (decompresses if needed)."""
        source = self._backup_root / record.device_id / record.relative_path
        if source.is_symlink():
            raise BackupError(f"Refusing to restore symlink (potential attack): {source.name}")
        if not source.exists():
            raise BackupError(f"Backup file not found: {source}")

        # Resolve target and guard against path traversal
        target_path = Path(target_path).expanduser().resolve()
        if target_path.is_symlink():
            raise BackupError("Target path is a symlink — refusing to overwrite")
        if target_path.is_dir():
            dest_name = record.filename.removesuffix(".gz")
            target_path = target_path / dest_name

        if record.compressed:
            compression.decompress_file(source, target_path)
        else:
            shutil.copy2(source, target_path)

        logger.info(f"Restored {record.filename} to {target_path}")
        return target_path

    def get_statistics(self, device_id: str | None = None) -> dict:
        """Return summary statistics for one device or all."""
        records = self.list_backups(device_id)
        if not records:
            return {"total": 0, "total_size_mb": 0}

        total_size = sum(r.size_bytes for r in records)
        return {
            "total": len(records),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "oldest": records[-1].created_at.isoformat() if records else None,
            "newest": records[0].created_at.isoformat() if records else None,
            "devices": len({r.device_id for r in records}),
        }
