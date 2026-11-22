"""Orchestrates multi-type backup runs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timedelta

from pfsentinel.models.backup import BackupIndexError, BackupRecord, BackupRun
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.backup import BackupService
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.extra_backup import ExtraBackupService, NeedsRootError
from pfsentinel.services.retention import RetentionService
from pfsentinel.utils.locking import LockTimeoutError
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

# The "backup folder cannot be locked" alert: at most once a week per device.
LOCK_WARN_EVERY = timedelta(days=7)
LOCK_WARN_STATE = "lock_warned.json"

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
        from pfsentinel.services.backup import BackupError

        device = self._config.get_device(device_id)
        if not device:
            raise BackupError(f"Device '{device_id}' not found in config")

        # One run at a time per device: the daily and the weekly task can
        # start together after the PC was off. A scheduled run has nobody
        # watching the console, so every way to fail here sends an alert.
        notify = self._backup_svc.notifications.notify_failure
        with ExitStack() as stack:
            try:
                stack.enter_context(self._retention.device_lock(device_id))
            except LockTimeoutError as e:
                notify(device_id, str(e))
                raise BackupError(str(e)) from e
            except OSError as e:
                root = self._config.backup_policy.resolved_root
                msg = f"Cannot use the backup folder {root}: {e}"
                notify(device_id, msg)
                raise BackupError(msg) from e
            records, failures = self._run_locked(
                device,
                include_extras,
                all_extras,
                config_only,
                description,
                progress,
                on_warning,
                area,
                no_packages,
            )

        # After the lock: the second copy only reads the index, and an upload
        # can take long. Holding the lock made a waiting run time out.
        # 6. Second copy of the encrypted files (never fails the backup)
        self._second_copy(device_id)

        if self._retention.lock_unsupported(device_id):
            self._warn_unlocked(device_id, on_warning)

        # One success alert for the run, with every file, not only the config.
        self._backup_svc.notifications.notify_success(records, label=device.label)

        # The config is saved, so the run counts as done, but a part that
        # failed every night went unnoticed: say so once per run.
        if failures:
            from pfsentinel.services.notifications import AlertKind

            self._backup_svc.notifications.alert(
                AlertKind.BACKUP_PARTIAL,
                f"pfSentinel - Part of the backup failed for {device_id}",
                f"Device: {device_id}\nThe config was saved. These parts failed:\n"
                + "\n".join(f"- {f}" for f in failures[:10]),
            )
        return records

    def _warn_unlocked(self, device_id: str, on_warning: WarningCallback | None) -> None:
        """The backup folder cannot lock (some SMB and NFS shares): say so.

        Two runs at once can then lose records of the backup index. The run
        itself went on, as before 1.0.0. The alert goes out at most once in
        LOCK_WARN_EVERY per device, so a share that never locks is not
        reported every night.
        """
        from pfsentinel.services import alert_history
        from pfsentinel.services.notifications import AlertKind

        root = self._config.backup_policy.resolved_root
        msg = (
            f"Device: {device_id}\nThe backup folder {root} cannot be locked, so two "
            "pfSentinel runs at once can lose records of the backup index. Keep the "
            "backup folder on a local disk, or make sure only one run uses it at a time."
        )
        if on_warning:
            on_warning(msg.split("\n", 1)[1])
        state = alert_history.state_dir() / LOCK_WARN_STATE
        sent = alert_history.read_json(state)
        sent = sent if isinstance(sent, dict) else {}
        now = datetime.now()
        try:
            last = datetime.fromisoformat(str(sent.get(device_id, "")))
        except ValueError:
            last = None
        if last is not None and now - last < LOCK_WARN_EVERY:
            return
        self._backup_svc.notifications.alert(
            AlertKind.LOCK_UNSUPPORTED,
            f"pfSentinel - Backup folder cannot be locked for {device_id}",
            msg,
        )
        sent[device_id] = now.isoformat()
        try:
            alert_history.write_private_json(state, sent)
        except OSError as e:
            logger.warning(f"Could not save {state.name}: {e}")

    def _run_locked(
        self,
        device: DeviceConfig,
        include_extras: list[str] | None,
        all_extras: bool,
        config_only: bool,
        description: str | None,
        progress: ProgressCallback | None,
        on_warning: WarningCallback | None,
        area: str,
        no_packages: bool,
    ) -> tuple[list[BackupRecord], list[str]]:
        """The steps that change the index. Returns the records and the failed parts."""
        from pfsentinel.services.backup import BackupError

        device_id = device.id
        records: list[BackupRecord] = []
        failures: list[str] = []

        def warn(msg: str) -> None:
            failures.append(msg)
            if on_warning:
                on_warning(msg)

        # One run: the config and every extra share its folder and number.
        try:
            run = self._retention.start_run(device_id)
        except BackupIndexError as e:
            self._backup_svc.notifications.notify_failure(device_id, str(e))
            raise
        except OSError as e:
            # For example the backup drive is gone: F: unplugged at 02:00.
            msg = f"Cannot use the backup folder {self._config.backup_policy.resolved_root}: {e}"
            self._backup_svc.notifications.notify_failure(device_id, msg)
            raise BackupError(msg) from e
        timestamp = run.started_at
        sequence = run.number

        # 1. Config backup (always, unless area/no_packages are specific)
        config_record = self._backup_svc.run_backup(
            device_id,
            description=description,
            progress=progress,
            area=area,
            no_packages=no_packages,
            run=run,
            notify=False,
        )
        records.append(config_record)

        # 2. Determine which extras to run. No early return: retention and
        # the second copy below run for every backup, config-only included.
        targets = [] if config_only else self._resolve_targets(include_extras, all_extras)

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
            except NeedsRootError as e:
                # Expected with a non-root backup user: one clear line, no ERROR.
                if on_warning:
                    on_warning(str(e))
                else:
                    logger.info(str(e))
            except Exception as e:
                msg = f"Extra backup '{target}' failed: {e}"
                logger.error(f"{msg} (device: {device_id})")
                warn(msg)

        # 4. ZFS snapshot (if enabled)
        if not config_only and self._config.backup_policy.zfs.enabled:
            try:
                zfs_record = self._run_zfs_snapshot(device_id, run, progress)
                if zfs_record:
                    records.append(zfs_record)
            except Exception as e:
                logger.error(f"ZFS snapshot failed for {device_id}: {e}")
                # Fall back to archive if configured
                if self._config.backup_policy.archive.enabled:
                    try:
                        archive_record = self._run_archive(device_id, run, progress)
                        if archive_record:
                            records.append(archive_record)
                    except Exception as e2:
                        logger.error(f"Archive fallback also failed for {device_id}: {e2}")
                        warn(f"ZFS snapshot failed ({e}), and the archive fallback too: {e2}")
                else:
                    warn(f"ZFS snapshot failed: {e}")
        elif not config_only and self._config.backup_policy.archive.enabled:
            try:
                archive_record = self._run_archive(device_id, run, progress)
                if archive_record:
                    records.append(archive_record)
            except Exception as e:
                logger.error(f"Archive backup failed for {device_id}: {e}")
                warn(f"Filesystem archive failed: {e}")

        # 5. Apply retention across all types
        self._retention.apply(device_id)
        return records, failures

    def _second_copy(self, device_id: str) -> None:
        if not self._config.backup_policy.second_copy.enabled:
            return
        from pfsentinel.services.notifications import AlertKind
        from pfsentinel.services.second_copy import SecondCopyService

        problems: list[str] = []
        try:
            result = SecondCopyService(self._config, self._creds).sync(device_id)
        except Exception as e:
            problems.append(str(e))
        else:
            problems.extend(result.errors)
            if result.skipped_plain:
                count = len(result.skipped_plain)
                if self._config.backup_policy.encryption.mode == "off":
                    problems.append(
                        f"{count} plain file(s) not copied: turn on encryption "
                        "(pfs encryption setup)"
                    )
                else:
                    # Older plain files from before encryption was on: new
                    # backups are encrypted, so this is no copy failure.
                    logger.warning(
                        f"Second copy: {count} older plain file(s) of {device_id} not "
                        "copied. Encrypt them: pfs encryption encrypt-existing"
                    )
            if result.copied:
                logger.info(f"Second copy: {len(result.copied)} file(s) copied for {device_id}")
        if problems:
            logger.error(f"Second copy failed for {device_id}: {'; '.join(problems)}")
            self._backup_svc.notifications.alert(
                AlertKind.SECOND_COPY_FAILED,
                f"pfSentinel - Second copy failed for {device_id}",
                f"Device: {device_id}\n" + "\n".join(problems[:5]),
            )

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
        run: BackupRun,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord | None:
        """Run a ZFS snapshot backup."""
        from pfsentinel.services.zfs_backup import ZfsBackupService

        zfs_svc = ZfsBackupService(self._config, self._creds)
        record = zfs_svc.run_snapshot_backup(device_id, progress=progress, run=run)

        # Add to backup index
        index = self._retention.load_index(device_id)
        index.add(record)
        self._retention.save_index(index)
        return record

    def _run_archive(
        self,
        device_id: str,
        run: BackupRun,
        progress: ProgressCallback | None = None,
    ) -> BackupRecord | None:
        """Run a filesystem archive backup."""
        from pfsentinel.services.archive_backup import ArchiveBackupService

        archive_svc = ArchiveBackupService(self._config, self._creds)
        record = archive_svc.run_archive_backup(device_id, progress=progress, run=run)

        # Add to backup index
        index = self._retention.load_index(device_id)
        index.add(record)
        self._retention.save_index(index)
        return record
