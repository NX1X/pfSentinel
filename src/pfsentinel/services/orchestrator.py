"""Orchestrates multi-type backup runs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from loguru import logger

from pfsentinel.models.backup import BackupRecord
from pfsentinel.models.config import AppConfig
from pfsentinel.services.backup import BackupService
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.extra_backup import ExtraBackupService
from pfsentinel.services.retention import RetentionService

ProgressCallback = Callable[[str, int], None]
WarningCallback = Callable[[str], None]


class BackupOrchestrator:
    """Coordinates config + extra + snapshot/archive backup runs for a device."""

    def __init__(self, config: AppConfig, credential_service: CredentialService) -> None:
        self._config = config
        self._creds = credential_service
        self._backup_svc = BackupService(config, credential_service)
        self._extra_svc = ExtraBackupService(config, credential_service)
        self._retention = RetentionService(config.backup_policy.resolved_root, config.backup_policy)

    def run(
        self,
        device_id: str,
        include_extras: list[str] | None = None,
        all_extras: bool = False,
        config_only: bool = False,
        description: str | None = None,
        progress: ProgressCallback | None = None,
        on_warning: WarningCallback | None = None,
        area: str = "",
        no_packages: bool = False,
    ) -> list[BackupRecord]:
        """Run a complete backup session for a device.

        Returns list of all BackupRecords created.
        """
        records: list[BackupRecord] = []
        device = self._config.get_device(device_id)
        if not device:
            from pfsentinel.services.backup import BackupError

            raise BackupError(f"Device '{device_id}' not found in config")

        timestamp = datetime.now()
        sequence = self._retention.next_sequence(device_id)

        # 1. Config backup (always, unless area/no_packages are specific)
        config_record = self._backup_svc.run_backup(
            device_id,
            description=description,
            progress=progress,
            area=area,
            no_packages=no_packages,
        )
        records.append(config_record)

        if config_only:
            return records

        # 2. Determine which extras to run
        targets = self._resolve_targets(include_extras, all_extras)
        if not targets:
            return records

        # 3. Run each enabled extra
        for target in targets:
            try:
                if progress:
                    progress(f"Backing up extra: {target}...", 0)
                record = self._extra_svc.backup_target(
                    target, device, timestamp, sequence, progress
                )
                # Add to index
                index = self._retention.load_index(device_id)
                index.add(record)
                self._retention.save_index(index)
                records.append(record)
                logger.info(f"Extra backup '{target}' complete: {record.filename}")
            except Exception as e:
                msg = f"Extra backup '{target}' failed: {e}"
                logger.error(f"{msg} (device: {device_id})")
                if on_warning:
                    on_warning(msg)

        # 4. ZFS snapshot (if enabled)
        if self._config.backup_policy.zfs.enabled:
            try:
                zfs_record = self._run_zfs_snapshot(device_id, timestamp, sequence, progress)
                if zfs_record:
                    records.append(zfs_record)
            except Exception as e:
                logger.error(f"ZFS snapshot failed for {device_id}: {e}")
                # Fall back to archive if configured
                if self._config.backup_policy.archive.enabled:
                    try:
                        archive_record = self._run_archive(device_id, timestamp, sequence, progress)
                        if archive_record:
                            records.append(archive_record)
                    except Exception as e2:
                        logger.error(f"Archive fallback also failed for {device_id}: {e2}")
        elif self._config.backup_policy.archive.enabled:
            try:
                archive_record = self._run_archive(device_id, timestamp, sequence, progress)
                if archive_record:
                    records.append(archive_record)
            except Exception as e:
                logger.error(f"Archive backup failed for {device_id}: {e}")

        # 5. Apply retention across all types
        self._retention.apply(device_id)

        return records

    def run_all(
        self,
        include_extras: list[str] | None = None,
        all_extras: bool = False,
        config_only: bool = False,
        description: str | None = None,
        progress: ProgressCallback | None = None,
        on_warning: WarningCallback | None = None,
    ) -> list[BackupRecord]:
        """Run backups for all enabled devices."""
        from pfsentinel.services.backup import BackupError

        all_records: list[BackupRecord] = []
        devices = self._config.enabled_devices()
        if not devices:
            raise BackupError("No enabled devices configured")

        for device in devices:
            try:
                records = self.run(
                    device.id,
                    include_extras=include_extras,
                    all_extras=all_extras,
                    config_only=config_only,
                    description=description,
                    progress=progress,
                    on_warning=on_warning,
                )
                all_records.extend(records)
            except Exception as e:
                logger.error(f"Backup failed for {device.id}: {e}")

        return all_records

    def _resolve_targets(self, include_extras: list[str] | None, all_extras: bool) -> list[str]:
        """Determine which extra targets to run."""
        extras_config = self._config.backup_policy.extras

        if all_extras:
            return ["rrd", "pkg", "dhcp", "aliases", "certs", "logs"]

        if include_extras:
            return include_extras

        # Use configured defaults
        return extras_config.enabled_targets()

    def _run_zfs_snapshot(
        self,
        device_id: str,
        timestamp: datetime,
        sequence: int,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord | None:
        """Run a ZFS snapshot backup."""
        from pfsentinel.services.zfs_backup import ZfsBackupService

        zfs_svc = ZfsBackupService(self._config, self._creds)
        record = zfs_svc.run_snapshot_backup(device_id, progress=progress)

        # Add to backup index
        index = self._retention.load_index(device_id)
        index.add(record)
        self._retention.save_index(index)
        return record

    def _run_archive(
        self,
        device_id: str,
        timestamp: datetime,
        sequence: int,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord | None:
        """Run a filesystem archive backup."""
        from pfsentinel.services.archive_backup import ArchiveBackupService

        archive_svc = ArchiveBackupService(self._config, self._creds)
        record = archive_svc.run_archive_backup(device_id, progress=progress)

        # Add to backup index
        index = self._retention.load_index(device_id)
        index.add(record)
        self._retention.save_index(index)
        return record
