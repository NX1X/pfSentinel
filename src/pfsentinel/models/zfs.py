"""ZFS snapshot tracking models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ZfsSnapshot(BaseModel):
    """Represents a ZFS snapshot on a remote pfSense device."""

    name: str  # e.g. "zroot/ROOT@pfsentinel-20250706-143022"
    dataset: str  # e.g. "zroot/ROOT"
    tag: str  # e.g. "pfsentinel-20250706-143022"
    created_at: datetime = Field(default_factory=datetime.now)
    transferred: bool = False
    local_record_id: str | None = None  # links to BackupRecord.id
    size_bytes: int = 0


class ZfsSnapshotIndex(BaseModel):
    """Per-device ZFS snapshot tracking (stored as zfs_snapshots.json)."""

    device_id: str
    snapshots: list[ZfsSnapshot] = Field(default_factory=list)

    def add(self, snapshot: ZfsSnapshot) -> None:
        self.snapshots.append(snapshot)

    def latest_transferred(self) -> ZfsSnapshot | None:
        """Return the most recently transferred snapshot."""
        transferred = [s for s in self.snapshots if s.transferred]
        if not transferred:
            return None
        return max(transferred, key=lambda s: s.created_at)

    def stale_snapshots(self, keep: int = 3) -> list[ZfsSnapshot]:
        """Return transferred snapshots that exceed the keep limit (oldest first)."""
        transferred = sorted(
            [s for s in self.snapshots if s.transferred],
            key=lambda s: s.created_at,
            reverse=True,
        )
        return transferred[keep:]

    def remove(self, snapshot_name: str) -> bool:
        original = len(self.snapshots)
        self.snapshots = [s for s in self.snapshots if s.name != snapshot_name]
        return len(self.snapshots) < original
