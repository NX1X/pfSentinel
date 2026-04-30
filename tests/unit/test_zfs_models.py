"""Tests for ZFS snapshot models."""

from __future__ import annotations

from datetime import datetime, timedelta

from pfsentinel.models.zfs import ZfsSnapshot, ZfsSnapshotIndex


class TestZfsSnapshot:
    def test_create_snapshot(self):
        snap = ZfsSnapshot(
            name="zroot/ROOT@pfsentinel-20250706-143022",
            dataset="zroot/ROOT",
            tag="pfsentinel-20250706-143022",
        )
        assert snap.name == "zroot/ROOT@pfsentinel-20250706-143022"
        assert snap.dataset == "zroot/ROOT"
        assert snap.transferred is False
        assert snap.local_record_id is None
        assert snap.size_bytes == 0

    def test_snapshot_transferred(self):
        snap = ZfsSnapshot(
            name="zroot/ROOT@test",
            dataset="zroot/ROOT",
            tag="test",
            transferred=True,
            local_record_id="abc-123",
            size_bytes=1024000,
        )
        assert snap.transferred is True
        assert snap.local_record_id == "abc-123"
        assert snap.size_bytes == 1024000


class TestZfsSnapshotIndex:
    def _make_snap(self, tag: str, transferred: bool = True, age_days: int = 0) -> ZfsSnapshot:
        return ZfsSnapshot(
            name=f"zroot/ROOT@{tag}",
            dataset="zroot/ROOT",
            tag=tag,
            transferred=transferred,
            created_at=datetime.now() - timedelta(days=age_days),
        )

    def test_add_snapshot(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        snap = self._make_snap("test1")
        index.add(snap)
        assert len(index.snapshots) == 1

    def test_latest_transferred_none_when_empty(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        assert index.latest_transferred() is None

    def test_latest_transferred_skips_untransferred(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        index.add(self._make_snap("old", transferred=True, age_days=2))
        index.add(self._make_snap("pending", transferred=False, age_days=0))
        latest = index.latest_transferred()
        assert latest is not None
        assert latest.tag == "old"

    def test_latest_transferred_returns_newest(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        index.add(self._make_snap("old", transferred=True, age_days=5))
        index.add(self._make_snap("new", transferred=True, age_days=0))
        latest = index.latest_transferred()
        assert latest.tag == "new"

    def test_stale_snapshots_keeps_newest(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        for i in range(5):
            index.add(self._make_snap(f"snap-{i}", transferred=True, age_days=5 - i))

        stale = index.stale_snapshots(keep=3)
        assert len(stale) == 2
        # Stale should be the oldest ones
        stale_tags = {s.tag for s in stale}
        assert "snap-0" in stale_tags
        assert "snap-1" in stale_tags

    def test_stale_snapshots_none_when_under_limit(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        index.add(self._make_snap("snap-1", transferred=True))
        assert index.stale_snapshots(keep=3) == []

    def test_stale_snapshots_ignores_untransferred(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        for i in range(5):
            index.add(self._make_snap(f"snap-{i}", transferred=False))
        # All untransferred — none stale
        assert index.stale_snapshots(keep=3) == []

    def test_remove_snapshot(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        index.add(self._make_snap("keep"))
        index.add(self._make_snap("remove"))
        assert index.remove("zroot/ROOT@remove") is True
        assert len(index.snapshots) == 1
        assert index.snapshots[0].tag == "keep"

    def test_remove_nonexistent(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        assert index.remove("nonexistent") is False

    def test_serialization_roundtrip(self):
        index = ZfsSnapshotIndex(device_id="fw1")
        index.add(self._make_snap("test", transferred=True))
        json_str = index.model_dump_json()
        loaded = ZfsSnapshotIndex.model_validate_json(json_str)
        assert len(loaded.snapshots) == 1
        assert loaded.snapshots[0].tag == "test"
        assert loaded.device_id == "fw1"
