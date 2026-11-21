"""ZFS snapshot backup service."""

from __future__ import annotations

import gzip
import shlex
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import IO, cast

from pfsentinel.models.backup import BackupIndex, BackupRecord, BackupRun, BackupType
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.models.zfs import ZfsSnapshot, ZfsSnapshotIndex
from pfsentinel.services import backup_crypto
from pfsentinel.services.backup_crypto import BackupCrypto
from pfsentinel.services.connection import SSHConnector
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.retention import RetentionService
from pfsentinel.utils import checksum, naming
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

ProgressCallback = Callable[[str, int], None]


class ZfsError(Exception):
    """Raised when ZFS operations fail."""


def current_chain(index: BackupIndex) -> list[BackupRecord]:
    """The local ZFS records since the newest full send, oldest first.

    An incremental send restores only on top of every send before it, back
    to a full one. Empty when there is no full send locally.
    """
    records = sorted(
        (r for r in index.records if r.backup_type == BackupType.ZFS_SNAPSHOT),
        key=lambda r: r.created_at,
    )
    for i in range(len(records) - 1, -1, -1):
        if not records[i].zfs_incremental:
            return records[i:]
    return []


class ZfsBackupService:
    """Manages ZFS snapshot lifecycle and transfer."""

    def __init__(self, config: AppConfig, credential_service: CredentialService) -> None:
        self._config = config
        self._creds = credential_service
        self._backup_root = config.backup_policy.resolved_root
        self._policy = config.backup_policy.zfs
        self._crypto = BackupCrypto(config.backup_policy.encryption, credential_service)

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
        dest = (
            self._backup_root / device.id / naming.run_relative_path(filename, timestamp, sequence)
        )

        cmd = f"zfs send {shlex.quote(snapshot.name)}"
        bytes_written, dest = self._stream_gzipped(connector, cmd, dest)

        file_hash = checksum.sha256_file(dest)
        logger.info(f"ZFS full send complete: {bytes_written} bytes")
        return BackupRecord(
            device_id=device.id,
            filename=dest.name,
            relative_path=naming.run_relative_path(dest.name, timestamp, sequence),
            created_at=timestamp,
            size_bytes=dest.stat().st_size,
            sha256=file_hash,
            connection_method="ssh",
            backup_type=BackupType.ZFS_SNAPSHOT,
            source_paths=[snapshot.name],
            compressed=True,
            sequence=sequence,
            verified=True,
            run_number=sequence,
            encrypted=backup_crypto.is_encrypted(dest),
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
        dest = (
            self._backup_root / device.id / naming.run_relative_path(filename, timestamp, sequence)
        )

        cmd = f"zfs send -i {shlex.quote(base_snapshot.name)} {shlex.quote(current_snapshot.name)}"
        bytes_written, dest = self._stream_gzipped(connector, cmd, dest)

        file_hash = checksum.sha256_file(dest)
        logger.info(f"ZFS incremental send complete: {bytes_written} bytes")
        return BackupRecord(
            device_id=device.id,
            filename=dest.name,
            relative_path=naming.run_relative_path(dest.name, timestamp, sequence),
            created_at=timestamp,
            size_bytes=dest.stat().st_size,
            sha256=file_hash,
            connection_method="ssh",
            backup_type=BackupType.ZFS_SNAPSHOT,
            source_paths=[current_snapshot.name],
            compressed=True,
            sequence=sequence,
            verified=True,
            run_number=sequence,
            encrypted=backup_crypto.is_encrypted(dest),
            zfs_snapshot_name=current_snapshot.name,
            zfs_incremental=True,
            zfs_base_snapshot=base_snapshot.name,
        )

    def _stream_gzipped(self, connector: SSHConnector, cmd: str, dest: Path) -> tuple[int, Path]:
        """Stream the raw ``zfs send`` output and gzip it here.

        Before rc.3 the device ran ``zfs send ... | gzip``. A pipeline exits
        with gzip's status, so a failed send still gave a small valid file
        that was recorded as a good backup. Without the pipe, the exit code
        is zfs's own. The file format is the same. Returns the bytes
        received and the final path (``.enc`` added when encrypted).
        """
        with self._crypto.create(dest) as (sink, final):
            with gzip.GzipFile(fileobj=sink, mode="wb", mtime=0) as gz:
                written = connector.stream_command_to_file(
                    cmd, final, timeout=1800, sink=cast(IO[bytes], gz)
                )
            if written == 0:
                # Raised inside create(): it deletes the partial file.
                raise ZfsError(f"'{cmd}' sent no data")
        return written, final

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
        run: BackupRun | None = None,
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
        if run is None:
            run = RetentionService(self._backup_root, self._config.backup_policy).start_run(
                device.id
            )
        timestamp = run.started_at
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

            # Full or incremental. An incremental needs the newest local send
            # as its base, and a chain may be at most as long as retention
            # keeps ZFS records: retention removes whole chains only, so a
            # new full send starts a new chain that the old one can age out.
            base = snap_index.latest_transferred()
            chain = current_chain(
                RetentionService(self._backup_root, self._config.backup_policy).load_index(
                    device_id
                )
            )
            policy = self._config.backup_policy
            max_chain = policy.max_backups_per_type.get("zfs", policy.max_backups_per_device)
            incremental = (
                self._policy.incremental
                and not force_full
                and base is not None
                and bool(chain)
                and chain[-1].zfs_snapshot_name == base.name
                and len(chain) < max_chain
            )

            # Transfer. On failure, destroy the new snapshot: it is in no
            # index, so no cleanup would ever remove it from the device.
            try:
                if incremental and base:
                    if progress:
                        progress("Streaming incremental ZFS snapshot...", 30)
                    record = self.transfer_incremental(
                        connector, base, snapshot, device, timestamp, sequence=run.number
                    )
                else:
                    if progress:
                        progress("Streaming full ZFS snapshot...", 30)
                    record = self.transfer_full(
                        connector, snapshot, device, timestamp, sequence=run.number
                    )
            except Exception:
                self.cleanup_remote(connector, [snapshot])
                raise

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
