"""ZFS snapshot backup service."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from loguru import logger

from pfsentinel.models.backup import BackupRecord, BackupType
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.models.zfs import ZfsSnapshot, ZfsSnapshotIndex
from pfsentinel.services.connection import SSHConnector
from pfsentinel.services.credentials import CredentialService
from pfsentinel.utils import checksum, naming

ProgressCallback = Callable[[str, int], None]


class ZfsError(Exception):
    """Raised when ZFS operations fail."""


class ZfsBackupService:
    """Manages ZFS snapshot lifecycle and transfer."""

    def __init__(self, config: AppConfig, credential_service: CredentialService) -> None:
        self._config = config
        self._creds = credential_service
        self._backup_root = config.backup_policy.resolved_root
        self._policy = config.backup_policy.zfs

    def _get_ssh_connector(self, device: DeviceConfig) -> SSHConnector:
        password = self._creds.get(device.id)
        passphrase = self._creds.get_ssh_key_passphrase(device.id)
        return SSHConnector(device, password, ssh_key_passphrase=passphrase)

    def _snapshot_index_path(self, device_id: str) -> Path:
        return self._backup_root / device_id / "zfs_snapshots.json"

    def load_snapshot_index(self, device_id: str) -> ZfsSnapshotIndex:
        path = self._snapshot_index_path(device_id)
        if not path.exists():
            return ZfsSnapshotIndex(device_id=device_id)
        try:
            return ZfsSnapshotIndex.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return ZfsSnapshotIndex(device_id=device_id)

    def save_snapshot_index(self, index: ZfsSnapshotIndex) -> None:
        path = self._snapshot_index_path(index.device_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(index.model_dump_json(indent=2), encoding="utf-8")

    def detect_zfs(self, connector: SSHConnector) -> bool:
        """Check if ZFS is available on remote device."""
        stdout, _, code = connector.exec_command("zfs list -H -o name 2>/dev/null", timeout=10)
        return code == 0 and bool(stdout.strip())

    def create_snapshot(self, connector: SSHConnector, dataset: str, tag: str) -> ZfsSnapshot:
        """Create a ZFS snapshot on the remote device."""
        snap_name = f"{dataset}@{tag}"
        _, stderr, code = connector.exec_command(
            f"zfs snapshot {shlex.quote(snap_name)}", timeout=30
        )
        if code != 0:
            raise ZfsError(f"Failed to create snapshot '{snap_name}': {stderr.strip()}")
        logger.info(f"Created ZFS snapshot: {snap_name}")
        return ZfsSnapshot(name=snap_name, dataset=dataset, tag=tag)

    def transfer_full(
        self,
        connector: SSHConnector,
        snapshot: ZfsSnapshot,
        device: DeviceConfig,
        timestamp: datetime,
        sequence: int,
    ) -> BackupRecord:
        """Full ZFS send: stream entire snapshot to local file."""
        filename = naming.generate_backup_filename(
            device_id=device.id,
            backup_type="zfs",
            compressed=True,
            timestamp=timestamp,
            sequence=sequence,
            label="zfs-full",
            extension=".zfs",
        )
        rel_path = naming.generate_typed_relative_path("zfs", filename, timestamp)
        dest = self._backup_root / device.id / rel_path

        cmd = f"zfs send {shlex.quote(snapshot.name)} | gzip"
        bytes_written = connector.stream_command_to_file(cmd, dest, timeout=1800)

        file_hash = checksum.sha256_file(dest)
        logger.info(f"ZFS full send complete: {bytes_written} bytes")
        return BackupRecord(
            device_id=device.id,
            filename=filename,
            relative_path=rel_path,
            created_at=timestamp,
            size_bytes=dest.stat().st_size,
            sha256=file_hash,
            connection_method="ssh",
            backup_type=BackupType.ZFS_SNAPSHOT,
            source_paths=[snapshot.name],
            compressed=True,
            sequence=sequence,
            verified=True,
            zfs_snapshot_name=snapshot.name,
            zfs_incremental=False,
        )

    def transfer_incremental(
        self,
        connector: SSHConnector,
        base_snapshot: ZfsSnapshot,
        current_snapshot: ZfsSnapshot,
        device: DeviceConfig,
        timestamp: datetime,
        sequence: int,
    ) -> BackupRecord:
        """Incremental ZFS send (delta between two snapshots)."""
        filename = naming.generate_backup_filename(
            device_id=device.id,
            backup_type="zfs",
            compressed=True,
            timestamp=timestamp,
            sequence=sequence,
            label="zfs-incr",
            extension=".zfs",
        )
        rel_path = naming.generate_typed_relative_path("zfs", filename, timestamp)
        dest = self._backup_root / device.id / rel_path

        cmd = (
            f"zfs send -i {shlex.quote(base_snapshot.name)}"
            f" {shlex.quote(current_snapshot.name)} | gzip"
        )
        bytes_written = connector.stream_command_to_file(cmd, dest, timeout=1800)

        file_hash = checksum.sha256_file(dest)
        logger.info(f"ZFS incremental send complete: {bytes_written} bytes")
        return BackupRecord(
            device_id=device.id,
            filename=filename,
            relative_path=rel_path,
            created_at=timestamp,
            size_bytes=dest.stat().st_size,
            sha256=file_hash,
            connection_method="ssh",
            backup_type=BackupType.ZFS_SNAPSHOT,
            source_paths=[current_snapshot.name],
            compressed=True,
            sequence=sequence,
            verified=True,
            zfs_snapshot_name=current_snapshot.name,
            zfs_incremental=True,
            zfs_base_snapshot=base_snapshot.name,
        )

    def cleanup_remote(
        self, connector: SSHConnector, snapshots_to_remove: list[ZfsSnapshot]
    ) -> None:
        """Destroy old snapshots on the remote device."""
        for snap in snapshots_to_remove:
            _, stderr, code = connector.exec_command(
                f"zfs destroy {shlex.quote(snap.name)}", timeout=30
            )
            if code != 0:
                logger.warning(f"Failed to destroy remote snapshot {snap.name}: {stderr.strip()}")
            else:
                logger.info(f"Destroyed remote snapshot: {snap.name}")

    def run_snapshot_backup(
        self,
        device_id: str,
        progress: ProgressCallback | None = None,
        force_full: bool = False,
    ) -> BackupRecord:
        """Full ZFS snapshot backup workflow.

        1. Connect to device
        2. Detect ZFS
        3. Create snapshot
        4. Transfer (full or incremental)
        5. Update snapshot index
        6. Cleanup old remote snapshots
        """
        device = self._config.get_device(device_id)
        if not device:
            raise ZfsError(f"Device '{device_id}' not found")

        connector = self._get_ssh_connector(device)
        timestamp = datetime.now()
        tag = f"pfsentinel-{timestamp.strftime('%Y%m%d-%H%M%S')}"
        snap_index = self.load_snapshot_index(device_id)

        with connector:
            # Detect ZFS
            if progress:
                progress("Detecting ZFS...", 5)
            if not self.detect_zfs(connector):
                raise ZfsError("ZFS not available on device")

            # Create snapshot
            if progress:
                progress("Creating ZFS snapshot...", 15)
            snapshot = self.create_snapshot(connector, self._policy.dataset, tag)

            # Determine full or incremental
            base = snap_index.latest_transferred()
            incremental = self._policy.incremental and base is not None and not force_full

            # Transfer
            if incremental and base:
                if progress:
                    progress("Streaming incremental ZFS snapshot...", 30)
                record = self.transfer_incremental(
                    connector, base, snapshot, device, timestamp, sequence=1
                )
            else:
                if progress:
                    progress("Streaming full ZFS snapshot...", 30)
                record = self.transfer_full(connector, snapshot, device, timestamp, sequence=1)

            # Update snapshot index
            snapshot.transferred = True
            snapshot.local_record_id = record.id
            snapshot.size_bytes = record.size_bytes
            snap_index.add(snapshot)

            # Cleanup old remote snapshots
            if self._policy.cleanup_remote:
                if progress:
                    progress("Cleaning up old remote snapshots...", 90)
                stale = snap_index.stale_snapshots(keep=self._policy.max_snapshots_remote)
                self.cleanup_remote(connector, stale)
                for s in stale:
                    snap_index.remove(s.name)

            self.save_snapshot_index(snap_index)

            if progress:
                progress("ZFS snapshot backup complete!", 100)

        return record
