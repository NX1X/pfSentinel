"""backup_index.json safety: an unreadable index stops everything, saves are atomic.

Before this fix, an index that could not be read was treated as empty, and
the next run saved that empty index over the real one (seen on Windows 11
with 1.0.0-rc.1: runs up to #003, then a new run #001 marked "initial").
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from pfsentinel.models.backup import BackupIndex, BackupIndexError, BackupRecord, BackupType
from pfsentinel.models.config import BackupPolicy
from pfsentinel.services import digest, reindex
from pfsentinel.services import retention as retention_module
from pfsentinel.services.retention import INDEX_FILE, RetentionService

DEVICE = "fw1"
SECRET = "s3cr3t-content-never-echoed"


@pytest.fixture
def retention(tmp_path: Path) -> RetentionService:
    return RetentionService(tmp_path, BackupPolicy(backup_root=tmp_path))


def _index_path(retention: RetentionService) -> Path:
    path = retention.index_path(DEVICE)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _record(name: str = "a.xml", run: int | None = 1) -> BackupRecord:
    return BackupRecord(device_id=DEVICE, filename=name, relative_path=name, run_number=run)


def _saved(retention: RetentionService, *records: BackupRecord, last: int = 3) -> bytes:
    index = BackupIndex(device_id=DEVICE, last_run_number=last, records=list(records))
    retention.save_index(index)
    return retention.index_path(DEVICE).read_bytes()


# ---------------------------------------------------------------------------
# load_index
# ---------------------------------------------------------------------------


class TestLoadIndex:
    def test_missing_file_is_a_new_empty_index(self, retention: RetentionService) -> None:
        index = retention.load_index(DEVICE)
        assert index.records == [] and index.last_run_number == 0
        assert not retention.index_path(DEVICE).exists(), "loading writes nothing"

    def test_valid_index_loads(self, retention: RetentionService) -> None:
        _saved(retention, _record())
        index = retention.load_index(DEVICE)
        assert [r.filename for r in index.records] == ["a.xml"]
        assert index.last_run_number == 3

    def test_old_schema_is_still_migrated(self, retention: RetentionService) -> None:
        old = {"device_id": DEVICE, "schema_version": 1, "records": [_record().model_dump()]}
        del old["records"][0]["backup_type"]
        _index_path(retention).write_text(json.dumps(old, default=str))
        index = retention.load_index(DEVICE)
        assert index.schema_version == 3
        assert index.records[0].backup_type == BackupType.CONFIG

    def test_permission_error_is_not_mistaken_for_a_missing_file(
        self, retention: RetentionService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _index_path(retention)
        path.write_text("{}")
        real = Path.read_bytes

        def denied(self: Path) -> bytes:
            if self.name == INDEX_FILE:
                raise PermissionError(13, "Permission denied", str(self))
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", denied)
        with pytest.raises(BackupIndexError) as exc:
            retention.load_index(DEVICE)
        err = exc.value
        assert err.device_id == DEVICE and err.path == path
        assert err.reason == "Permission denied"
        assert f"device '{DEVICE}'" in str(err) and str(path) in str(err)
        assert "pfs backup reindex -d fw1" in str(err)

    def test_other_os_error_without_strerror_names_its_type(
        self, retention: RetentionService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _index_path(retention).write_text("{}")

        def bare_error(self: Path) -> bytes:
            raise OSError()

        monkeypatch.setattr(Path, "read_bytes", bare_error)
        with pytest.raises(BackupIndexError, match=r"\(OSError\)"):
            retention.load_index(DEVICE)

    def test_a_folder_in_place_of_the_file_is_an_error(self, retention: RetentionService) -> None:
        _index_path(retention).mkdir()
        with pytest.raises(BackupIndexError):
            retention.load_index(DEVICE)

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            (f'{{"device_id": "{SECRET}", ', "not valid JSON at line 1"),
            (f'["{SECRET}"]', "not a JSON object"),
            (
                json.dumps({"device_id": DEVICE, "schema_version": 3, "last_run_number": SECRET}),
                "at last_run_number",
            ),
            (
                json.dumps(
                    {
                        "device_id": DEVICE,
                        "records": [
                            {"device_id": DEVICE, "filename": "x", "relative_path": f"../{SECRET}"}
                        ],
                    }
                ),
                "invalid value at records.0.relative_path",
            ),
            (
                json.dumps({"device_id": DEVICE, "schema_version": 1, "records": [SECRET]}),
                "unexpected structure (TypeError)",
            ),
        ],
        ids=["bad-json", "not-an-object", "bad-field", "traversal-path", "bad-structure"],
    )
    def test_invalid_content_raises_without_echoing_it(
        self, retention: RetentionService, content: str, reason: str
    ) -> None:
        _index_path(retention).write_text(content, encoding="utf-8")
        with pytest.raises(BackupIndexError) as exc:
            retention.load_index(DEVICE)
        assert reason in exc.value.reason
        assert SECRET not in str(exc.value)

    def test_not_utf8(self, retention: RetentionService) -> None:
        _index_path(retention).write_bytes(b'{"device_id": "\xff\xfe"}')
        with pytest.raises(BackupIndexError, match="not UTF-8 text"):
            retention.load_index(DEVICE)

    def test_start_run_never_overwrites_an_unreadable_index(
        self, retention: RetentionService
    ) -> None:
        """The field bug: the next run saved a fresh index holding only run #001."""
        path = _index_path(retention)
        path.write_text('{"device_id": "fw1", "last_run_number": 3, "records": [')
        before = path.read_bytes()

        with pytest.raises(BackupIndexError):
            retention.start_run(DEVICE)
        with pytest.raises(BackupIndexError):
            retention.apply(DEVICE)
        with pytest.raises(BackupIndexError):
            retention.next_sequence(DEVICE)

        assert path.read_bytes() == before
        assert sorted(p.name for p in path.parent.iterdir()) == [INDEX_FILE]


# ---------------------------------------------------------------------------
# save_index
# ---------------------------------------------------------------------------


class TestSaveIndex:
    def test_first_save_has_no_spare(self, retention: RetentionService) -> None:
        _saved(retention, _record())
        assert sorted(p.name for p in retention.device_dir(DEVICE).iterdir()) == [INDEX_FILE]

    def test_keeps_the_previous_version_as_bak(self, retention: RetentionService) -> None:
        first = _saved(retention, _record("a.xml"))
        second = _saved(retention, _record("a.xml"), _record("b.xml", 2))
        _saved(retention, _record("c.xml"))

        spare = retention.index_path(DEVICE).with_name(INDEX_FILE + ".bak")
        assert spare.read_bytes() == second, "one previous version only"
        assert spare.read_bytes() != first
        names = sorted(p.name for p in retention.device_dir(DEVICE).iterdir())
        assert names == [INDEX_FILE, INDEX_FILE + ".bak"], "no temp files left"

    def test_writes_a_temp_file_in_the_same_folder_then_replaces(
        self, retention: RetentionService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Path, Path]] = []
        synced: list[int] = []
        real_replace, real_fsync = os.replace, os.fsync

        def spy_replace(src: str, dst: str) -> None:
            calls.append((Path(src), Path(dst)))
            real_replace(src, dst)

        def spy_fsync(fd: int) -> None:
            synced.append(fd)
            real_fsync(fd)

        monkeypatch.setattr(retention_module.os, "replace", spy_replace)
        monkeypatch.setattr(retention_module.os, "fsync", spy_fsync)
        _saved(retention, _record())

        path = retention.index_path(DEVICE)
        src, dst = calls[-1]
        assert dst == path and src.parent == path.parent and src != path
        assert synced, "flushed to disk before the replace"

    def test_a_failed_replace_leaves_the_old_index_and_no_temp_file(
        self, retention: RetentionService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _saved(retention, _record("a.xml"))
        real_replace = os.replace

        def fail_on_index(src: str, dst: str) -> None:
            if Path(dst).name == INDEX_FILE:
                raise OSError(28, "No space left on device")
            real_replace(src, dst)

        monkeypatch.setattr(retention_module.os, "replace", fail_on_index)
        with pytest.raises(OSError):
            _saved(retention, _record("b.xml"))

        assert retention.index_path(DEVICE).read_bytes() == before
        assert not [p for p in retention.device_dir(DEVICE).iterdir() if p.suffix == ".tmp"]

    def test_a_failed_spare_copy_only_warns(
        self, retention: RetentionService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _saved(retention, _record("a.xml"))

        def locked(src: object, dst: object) -> None:
            raise PermissionError(13, "locked")

        monkeypatch.setattr(retention_module.shutil, "copyfile", locked)
        _saved(retention, _record("b.xml"))

        assert [r.filename for r in retention.load_index(DEVICE).records] == ["b.xml"]
        assert not retention.index_path(DEVICE).with_name(INDEX_FILE + ".bak").exists()
        assert not [p for p in retention.device_dir(DEVICE).iterdir() if p.suffix == ".tmp"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_owner_only_on_posix(self, retention: RetentionService) -> None:
        _saved(retention, _record())
        assert retention.index_path(DEVICE).stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# Daily digest with an unreadable index
# ---------------------------------------------------------------------------


class TestDigest:
    def test_digest_names_the_unreadable_index(self, tmp_path: Path) -> None:
        def list_backups(device_id: str) -> list[BackupRecord]:
            if device_id == "bad":
                raise BackupIndexError("bad", tmp_path / INDEX_FILE, "not valid JSON at line 1")
            return []

        now = datetime(2026, 11, 20, 8, 30)
        summaries = digest.summarize_devices(["bad", "good"], list_backups, [], now)
        assert summaries[0].index_error == "not valid JSON at line 1"
        assert summaries[0].problem_count == 1 and summaries[1].problem_count == 0

        result = digest.render_digest(summaries, [], now)
        assert result.problem_count == 1
        assert "bad: 1 problem(s)" in result.body
        assert "Backup index cannot be read (not valid JSON at line 1)." in result.body
        assert "pfs backup reindex -d bad" in result.body
        assert "good: OK" in result.body


# ---------------------------------------------------------------------------
# reindex: file names and layouts
# ---------------------------------------------------------------------------


def _touch(device_dir: Path, rel: str, data: bytes = b"data") -> Path:
    path = device_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestReindexNames:
    def test_every_writer_layout_and_type_is_recognized(self, retention: RetentionService) -> None:
        d = retention.device_dir(DEVICE)
        run = "2026/11/20/101010_#005"
        files = {
            f"{run}/fw1_2026-11-20_101010_#005_interfaces+system.xml.gz.enc": (
                BackupType.CONFIG,
                5,
            ),
            f"{run}/fw1_2026-11-20_101010_#005_rrd.tar.gz": (BackupType.RRD, 5),
            f"{run}/fw1_2026-11-20_101010_#005_pkg.tar.gz": (BackupType.PACKAGE_CONFIGS, 5),
            f"{run}/fw1_2026-11-20_101010_#005_dhcp.txt": (BackupType.DHCP_LEASES, 5),
            f"{run}/fw1_2026-11-20_101010_#005_aliases.tar.gz": (BackupType.ALIASES, 5),
            f"{run}/fw1_2026-11-20_101010_#005_certs.tar.gz.enc": (BackupType.CERTIFICATES, 5),
            f"{run}/fw1_2026-11-20_101010_#005_logs.tar.gz": (BackupType.LOGS, 5),
            f"{run}/fw1_2026-11-20_101010_#005_archive.tar.gz": (BackupType.FS_ARCHIVE, 5),
            f"{run}/fw1_2026-11-20_101010_#005_zfs-incr.zfs.gz": (BackupType.ZFS_SNAPSHOT, 5),
            # Before 0.3.0: date folders, extras under a type folder, daily sequence.
            "2026/11/19/fw1_2026-11-19_020000_#001_initial.xml": (BackupType.CONFIG, None),
            "zfs/2026/11/19/fw1_2026-11-19_020000_#001_zfs-full.zfs.gz": (
                BackupType.ZFS_SNAPSHOT,
                None,
            ),
        }
        for rel in files:
            _touch(d, rel)
        for ignored in [
            "notes.txt",
            "zfs_snapshots.json",
            f"{INDEX_FILE}.bak",
            f"{run}/other-fw_2026-11-20_101010_#005_rrd.tar.gz",  # another device
            f"{run}/fw1_2026-11-20_101010_#005_unknown.tar.gz",  # unknown type
            f"{run}/fw1_2026-13-45_101010_#005_rrd.tar.gz",  # impossible date
            f"{run}/fw1_2026-11-20_101010_#005_rrd.tar.gz.tmp",
        ]:
            _touch(d, ignored)

        result = reindex.reindex_device(retention, DEVICE)

        added = {r.relative_path: r for r in result.added}
        assert set(added) == set(files)
        for rel, (backup_type, run_number) in files.items():
            assert (added[rel].backup_type, added[rel].run_number) == (backup_type, run_number)
        cfg = added[f"{run}/fw1_2026-11-20_101010_#005_interfaces+system.xml.gz.enc"]
        assert cfg.encrypted and cfg.compressed
        assert [c.value for c in cfg.changes] == ["interfaces", "system"]
        assert cfg.created_at == datetime(2026, 11, 20, 10, 10, 10)
        assert cfg.description == reindex.REINDEX_DESCRIPTION and not cfg.verified
        assert added[f"{run}/fw1_2026-11-20_101010_#005_zfs-incr.zfs.gz"].zfs_incremental
        assert not added[f"{run}/fw1_2026-11-20_101010_#005_dhcp.txt"].compressed
        assert result.last_run_number == 5
        assert retention.load_index(DEVICE).last_run_number == 5

    def test_a_number_folder_that_does_not_match_means_no_run_number(
        self, retention: RetentionService
    ) -> None:
        d = retention.device_dir(DEVICE)
        _touch(d, "2026/11/20/101010_#007/fw1_2026-11-20_101010_#005_initial.xml")
        [record] = reindex.reindex_device(retention, DEVICE).added
        assert record.run_number is None

    def test_old_runs_count_like_the_schema_migration(self, retention: RetentionService) -> None:
        d = retention.device_dir(DEVICE)
        for day in ("17", "18", "19"):
            _touch(d, f"2026/11/{day}/fw1_2026-11-{day}_020000_#001_initial.xml")
        _touch(d, "dhcp/2026/11/19/fw1_2026-11-19_020000_#001_dhcp.txt")
        result = reindex.reindex_device(retention, DEVICE)
        assert result.last_run_number == 3, "three old runs, so the next is #004"

    def test_no_device_folder_changes_nothing(self, retention: RetentionService) -> None:
        result = reindex.reindex_device(retention, DEVICE)
        assert result.added == [] and not result.saved
        assert not retention.device_dir(DEVICE).exists()

    def test_broken_name_never_reuses_an_existing_file(self, tmp_path: Path) -> None:
        index_path = tmp_path / INDEX_FILE
        first = reindex._broken_name(index_path)
        first.write_text("old broken copy")
        second = reindex._broken_name(index_path)
        assert second != first and second.name.startswith(f"{INDEX_FILE}.broken-")
        assert not second.exists()

    def test_walk_errors_are_reported(
        self, retention: RetentionService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = retention.device_dir(DEVICE)
        _touch(d, "2026/11/19/fw1_2026-11-19_020000_#001_initial.xml")
        real_walk = os.walk

        def walk(top, onerror, followlinks):
            onerror(PermissionError(13, "Permission denied", str(d / "2026" / "locked")))
            yield from real_walk(top, onerror=onerror, followlinks=followlinks)

        monkeypatch.setattr(reindex.os, "walk", walk)
        result = reindex.reindex_device(retention, DEVICE)
        assert result.unreadable == ["2026/locked"]
        assert len(result.added) == 1
