"""BackupService - orchestrates the full backup lifecycle."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from pfsentinel.models.backup import BackupRecord, BackupRun
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services import backup_crypto
from pfsentinel.services.backup_crypto import BackupCrypto
from pfsentinel.services.connection import ConnectionManager
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.diff import DiffService
from pfsentinel.services.notifications import NotificationService
from pfsentinel.services.retention import RetentionService
from pfsentinel.utils import checksum, compression, naming, xml_parser
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

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
        self._crypto = BackupCrypto(config.backup_policy.encryption, credential_service)
        self._diff = DiffService(self._backup_root, self._crypto)
        self._notifications = NotificationService(config.notifications, credential_service)

    @property
    def diff_service(self) -> DiffService:
        """Diff service that can read this config's encrypted backups."""
        return self._diff

    @property
    def crypto(self) -> BackupCrypto:
        return self._crypto

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
        run: BackupRun | None = None,
    ) -> BackupRecord:
        """Run a full (or selective) backup for one device.

        Args:
            run: The run this backup belongs to (the orchestrator passes it,
                so config and extras share one folder). None starts a new run.
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
            if run is None:
                run = retention.start_run(device_id)
                index = retention.load_index(device_id)
            sequence = run.number
            timestamp = run.started_at
            filename = naming.generate_filename(
                device_id=device_id,
                changes=changes,
                sequence=sequence,
                compressed=self._config.backup_policy.compress,
                timestamp=timestamp,
            )
            relative_path = run.relative_path(filename)
            dest_path = self._backup_root / device_id / relative_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # --- Save backup ---
            _p("Saving backup...", 65)
            data = xml_content.encode("utf-8")
            if self._config.backup_policy.compress:
                data = compression.compress_bytes(data)
            # Owner-only file; encrypted as <name>.enc when encryption is on,
            # so the plain config never touches the disk.
            dest_path = self._crypto.write_bytes(dest_path, data)
            filename = dest_path.name
            relative_path = run.relative_path(filename)

            file_size = dest_path.stat().st_size

            # --- Checksum ---
            _p("Calculating checksum...", 75)
            file_hash = checksum.sha256_file(dest_path)

            # --- Verify if requested ---
            if self._config.backup_policy.validate_after_backup:
                _p("Verifying backup...", 80)
                try:
                    read_back = self.read_xml(dest_path)
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
                run_number=run.number,
                encrypted=backup_crypto.is_encrypted(dest_path),
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
                xml = self.read_xml(path)
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
        """Verify a tar archive is readable (decrypting it on the fly if needed)."""
        import tarfile

        try:
            with self._crypto.open_read(path) as raw:
                # Stream mode reads front to back, which an encrypted file needs.
                with tarfile.open(fileobj=raw, mode="r|gz" if compressed else "r|") as tar:
                    for _ in tar:
                        pass
        except Exception as e:
            raise BackupError(f"Tar archive verification failed for '{path.name}': {e}") from e

    def read_xml(self, path: Path) -> str:
        """The config XML of a backup file: decrypted and decompressed as needed."""
        data = self._crypto.read_bytes(path)
        if backup_crypto.plain_name(path.name).endswith(".gz"):
            data = compression.decompress_bytes(data)
        return data.decode("utf-8")

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
            dest_name = backup_crypto.plain_name(record.filename).removesuffix(".gz")
            target_path = target_path / dest_name

        if record.compressed or backup_crypto.is_encrypted(source):
            data = self._crypto.read_bytes(source)
            if record.compressed:
                data = compression.decompress_bytes(data)
            target_path.write_bytes(data)
        else:
            shutil.copy2(source, target_path)

        logger.info(f"Restored {record.filename} to {target_path}")
        return target_path

    def encrypt_existing(
        self,
        device_id: str,
        progress: Callable[[str], None] | None = None,
        wipe: bool = False,
    ) -> int:
        """Encrypt this device's plain backup files in place. Returns how many.

        Each file is encrypted to ``<name>.enc`` and read back; only when the
        decrypted bytes match the original is the index updated and the plain
        file deleted (wiped first when secure_delete is on). The index is saved
        after every file, so an interruption loses nothing.
        """
        import hashlib

        if not self._crypto.enabled:
            raise BackupError("Encryption is off. Run: pfs encryption setup")
        retention = self._retention()
        index = retention.load_index(device_id)
        done = 0
        for record in list(index.records):
            path = self._backup_root / device_id / record.relative_path
            if record.encrypted or backup_crypto.is_encrypted(path) or not path.is_file():
                continue
            if path.is_symlink():
                logger.error(f"Refusing to encrypt symlink: {path}")
                continue
            plain_hash = hashlib.sha256()
            with path.open("rb") as src, self._crypto.create(path) as (sink, final):
                while chunk := src.read(1024 * 1024):
                    plain_hash.update(chunk)
                    sink.write(chunk)
            check = hashlib.sha256()
            with self._crypto.open_read(final) as reader:
                while chunk := reader.read(1024 * 1024):
                    check.update(chunk)
            if check.digest() != plain_hash.digest():
                final.unlink(missing_ok=True)
                raise BackupError(f"Read-back check failed for {record.filename}; left unchanged")

            record.filename = final.name
            record.relative_path = str(Path(record.relative_path).parent / final.name).replace(
                "\\", "/"
            )
            record.sha256 = checksum.sha256_file(final)
            record.size_bytes = final.stat().st_size
            record.encrypted = True
            retention.save_index(index)
            if wipe or self._config.backup_policy.secure_delete:
                _secure_wipe(path)
            path.unlink()
            done += 1
            if progress:
                progress(record.filename)
        return done

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
