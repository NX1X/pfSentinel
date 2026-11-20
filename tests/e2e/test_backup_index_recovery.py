"""An unreadable backup index stops every command, and `pfs backup reindex` recovers.

Field bug (Windows 11, 1.0.0-rc.1): an index that could not be read was
treated as empty, the next run saved it over the real one, and the old
records disappeared from `pfs backup list` while the files stayed on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from pfsentinel.cli import app as app_module
from pfsentinel.cli.app import app
from pfsentinel.models.backup import BackupIndex, BackupIndexError
from pfsentinel.models.config import AppConfig
from pfsentinel.services import reindex as reindex_module
from pfsentinel.services import retention as retention_module
from pfsentinel.services.backup_crypto import BackupCrypto
from pfsentinel.services.notifications import AlertKind, NotificationService
from pfsentinel.services.reindex import REINDEX_DESCRIPTION
from pfsentinel.services.retention import INDEX_FILE, RetentionService

pytestmark = pytest.mark.e2e

RUN = ["backup", "run", "-d", "home-fw", "--config-only", "--no-notify"]
BROKEN = b'{"device_id": "home-fw", "last_run_number": 3, "records": ['


def _output(result) -> str:
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


def _retention(config: AppConfig) -> RetentionService:
    return RetentionService(config.backup_policy.resolved_root, config.backup_policy)


def _device_dir(config: AppConfig) -> Path:
    return config.backup_policy.resolved_root / "home-fw"


def _index_path(config: AppConfig) -> Path:
    return _device_dir(config) / INDEX_FILE


def _backup_files(config: AppConfig) -> dict[str, bytes]:
    """Every backup file (not index files) under the device folder, by relative path."""
    root = _device_dir(config)
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(INDEX_FILE) and not p.name.startswith(".")
    }


def _run_backups(cli_runner: CliRunner, count: int) -> None:
    for _ in range(count):
        result = cli_runner.invoke(app, RUN)
        assert result.exit_code == 0, _output(result)


@pytest.fixture
def alerts():
    """Every alert sent, as (kind, title, message)."""
    sent: list[tuple[AlertKind, str, str]] = []

    def capture(self, kind, title, message):
        sent.append((kind, title, message))
        return {}

    with patch.object(NotificationService, "alert", capture):
        yield sent


@pytest.fixture
def broken_index(cli_runner: CliRunner, seeded_config: AppConfig) -> Path:
    """Three runs on disk, then an index that is not valid JSON."""
    _run_backups(cli_runner, 3)
    path = _index_path(seeded_config)
    path.write_bytes(BROKEN)
    return path


# ---------------------------------------------------------------------------
# Every command that reads the index stops
# ---------------------------------------------------------------------------


class TestUnreadableIndexStopsCommands:
    def test_backup_run_stops_with_one_line_and_writes_nothing(
        self, cli_runner: CliRunner, seeded_config: AppConfig, broken_index: Path, alerts
    ) -> None:
        files = _backup_files(seeded_config)

        result = cli_runner.invoke(app, ["backup", "run", "-d", "home-fw", "--config-only"])

        assert result.exit_code == 1
        out = _output(result)
        assert "Backup index of device 'home-fw' cannot be read" in out
        assert "not valid JSON" in out and "pfs backup reindex -d home-fw" in out
        assert "Unexpected error" not in out
        assert broken_index.read_bytes() == BROKEN, "the index was not overwritten"
        assert _backup_files(seeded_config) == files, "no new run"
        assert [k for k, _, _ in alerts] == [AlertKind.BACKUP_FAILED]

    def test_backup_run_for_all_devices_fails(
        self, cli_runner: CliRunner, seeded_config: AppConfig, broken_index: Path
    ) -> None:
        result = cli_runner.invoke(app, ["backup", "run", "--config-only", "--no-notify"])
        assert result.exit_code == 1
        assert "home-fw: Backup index of device 'home-fw' cannot be read" in _output(result)
        assert broken_index.read_bytes() == BROKEN

    @pytest.mark.parametrize(
        "argv",
        [
            ["backup", "list"],
            ["backup", "list", "-d", "home-fw"],
            ["list"],
            ["backup", "check", "-d", "home-fw"],
            ["backup", "verify", "x.xml.gz"],
            ["backup", "search", "-n", "initial"],
            ["encryption", "status"],
        ],
    )
    def test_reading_commands_raise_the_index_error(
        self, cli_runner: CliRunner, seeded_config: AppConfig, broken_index: Path, argv
    ) -> None:
        result = cli_runner.invoke(app, argv)
        assert result.exit_code == 1
        assert isinstance(result.exception, BackupIndexError), _output(result)
        assert broken_index.read_bytes() == BROKEN

    @pytest.mark.parametrize(
        "argv",
        [["backup", "snapshot", "-d", "home-fw"], ["backup", "archive", "-d", "home-fw"]],
        ids=["snapshot", "archive"],
    )
    def test_snapshot_and_archive_stop_before_connecting(
        self, cli_runner: CliRunner, seeded_config: AppConfig, broken_index: Path, argv
    ) -> None:
        files = _backup_files(seeded_config)
        result = cli_runner.invoke(app, argv)
        assert result.exit_code == 1
        out = _output(result)
        assert "Backup index of device 'home-fw' cannot be read" in out
        assert "Unexpected error" not in out
        assert broken_index.read_bytes() == BROKEN
        assert _backup_files(seeded_config) == files

    def test_second_copy_stops(
        self, cli_runner: CliRunner, seeded_config: AppConfig, broken_index: Path, tmp_path
    ) -> None:
        config = AppConfig.load()
        config.backup_policy.second_copy.enabled = True
        config.backup_policy.second_copy.path = str(tmp_path / "copy")
        config.save()

        result = cli_runner.invoke(app, ["copy", "now", "-d", "home-fw"])

        assert isinstance(result.exception, BackupIndexError)
        assert not (tmp_path / "copy").exists()

    def test_encrypt_existing_stops(
        self,
        cli_runner: CliRunner,
        seeded_config: AppConfig,
        broken_index: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        files = _backup_files(seeded_config)
        monkeypatch.setattr(BackupCrypto, "enabled", property(lambda self: True))

        result = cli_runner.invoke(app, ["encryption", "encrypt-existing", "-y"])

        assert isinstance(result.exception, BackupIndexError)
        assert _backup_files(seeded_config) == files

    def test_retention_stops(self, seeded_config: AppConfig, broken_index: Path) -> None:
        files = _backup_files(seeded_config)
        with pytest.raises(BackupIndexError):
            _retention(seeded_config).apply("home-fw")
        assert _backup_files(seeded_config) == files

    def test_entry_point_prints_one_line_and_exits_1(
        self,
        seeded_config: AppConfig,
        broken_index: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["pfs", "backup", "list"])
        monkeypatch.setattr(app_module, "_configure_logging_from_config", lambda: None)

        with pytest.raises(SystemExit) as exc:
            app_module.main_entry()

        assert exc.value.code == 1
        err = " ".join(capsys.readouterr().err.split())
        assert err.startswith("✗ Error: Backup index of device 'home-fw' cannot be read")
        assert "Traceback" not in err

    def test_check_recent_alerts_that_the_index_cannot_be_read(
        self, cli_runner: CliRunner, seeded_config: AppConfig, broken_index: Path, alerts
    ) -> None:
        result = cli_runner.invoke(app, ["backup", "check-recent", "--non-interactive"])

        assert result.exit_code == 1
        assert "cannot be read" in _output(result)
        [(kind, title, message)] = alerts
        assert kind is AlertKind.NO_RECENT_BACKUP
        assert title == "pfSentinel - Backup index of home-fw cannot be read"
        assert "No recent backup" not in title
        assert message.startswith("Device: home-fw\nThe backup index cannot be read")
        assert "pfs backup reindex -d home-fw" in message
        assert broken_index.read_bytes() == BROKEN

    def test_check_recent_still_sends_the_digest(
        self, cli_runner: CliRunner, seeded_config: AppConfig, broken_index: Path, alerts
    ) -> None:
        config = AppConfig.load()
        config.notifications.digest_enabled = True
        config.notifications.digest_time = "00:00"
        config.save()
        sent: list[tuple[str, str]] = []

        def capture(self, title, message, has_problems):
            sent.append((title, message))
            return {"Email": None}

        with patch.object(NotificationService, "send_digest", capture):
            result = cli_runner.invoke(app, ["backup", "check-recent"])

        assert result.exit_code == 1
        [(title, body)] = sent
        assert "problem" in title
        assert "Backup index cannot be read" in body


# ---------------------------------------------------------------------------
# pfs backup reindex
# ---------------------------------------------------------------------------


class TestReindex:
    def test_merges_missing_files_into_a_readable_index(
        self, cli_runner: CliRunner, seeded_config: AppConfig
    ) -> None:
        _run_backups(cli_runner, 3)
        retention = _retention(seeded_config)
        full = retention.load_index("home-fw")
        kept = full.sorted_by_date()[0]
        retention.save_index(BackupIndex(device_id="home-fw", last_run_number=3, records=[kept]))

        result = cli_runner.invoke(app, ["backup", "reindex", "-d", "home-fw"])

        assert result.exit_code == 0, _output(result)
        assert "2 added, 1 kept, next run #004" in _output(result)
        index = retention.load_index("home-fw")
        assert index.get(kept.id) == kept, "existing records are kept as they are"
        assert {r.relative_path for r in index.records} == {r.relative_path for r in full.records}
        root = _device_dir(seeded_config)
        for record in index.records:
            data = (root / record.relative_path).read_bytes()
            assert record.sha256 == hashlib.sha256(data).hexdigest()
            assert record.size_bytes == len(data)
        added = [r for r in index.records if r.id != kept.id]
        assert all(r.description == REINDEX_DESCRIPTION for r in added)

        verify = cli_runner.invoke(app, ["backup", "verify", added[0].filename])
        assert verify.exit_code == 0, _output(verify)

    def test_field_bug_run_numbers_continue_after_the_highest(
        self, cli_runner: CliRunner, seeded_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runs #001-#003 on disk, but the index was reset to one new run #001."""
        _run_backups(cli_runner, 3)
        retention = _retention(seeded_config)
        retention.index_path("home-fw").unlink()

        class NextDay(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=1)

        with monkeypatch.context() as m:  # a later run, so its folder differs
            m.setattr(retention_module, "datetime", NextDay)
            _run_backups(cli_runner, 1)  # the bug: a second run #001
        assert retention.load_index("home-fw").last_run_number == 1

        result = cli_runner.invoke(app, ["backup", "reindex", "-d", "home-fw"])
        assert result.exit_code == 0, _output(result)
        index = retention.load_index("home-fw")
        assert len(index.records) == 4 and index.last_run_number == 3

        _run_backups(cli_runner, 1)
        numbers = sorted(r.run_number or 0 for r in retention.load_index("home-fw").records)
        assert numbers == [1, 1, 2, 3, 4], "the next run is #004, not another #002"

    def test_broken_index_is_moved_aside_never_deleted(
        self, cli_runner: CliRunner, seeded_config: AppConfig, broken_index: Path
    ) -> None:
        files = _backup_files(seeded_config)

        result = cli_runner.invoke(app, ["backup", "reindex"])

        assert result.exit_code == 0, _output(result)
        out = _output(result)
        assert "the backup index cannot be read (not valid JSON at line 1)" in out
        [moved] = list(broken_index.parent.glob(f"{INDEX_FILE}.broken-*"))
        assert moved.read_bytes() == BROKEN
        assert f"Moveditasideto:{moved}" in "".join(out.split())  # Rich wraps long paths
        index = _retention(seeded_config).load_index("home-fw")
        assert len(index.records) == 3 and index.last_run_number == 3
        assert _backup_files(seeded_config) == files, "backup files are never touched"

        listing = cli_runner.invoke(app, ["backup", "list", "-d", "home-fw"])
        assert listing.exit_code == 0, _output(listing)

    def test_dry_run_changes_nothing(
        self, cli_runner: CliRunner, seeded_config: AppConfig, broken_index: Path
    ) -> None:
        folder = broken_index.parent
        before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        files = _backup_files(seeded_config)

        result = cli_runner.invoke(app, ["backup", "reindex", "-d", "home-fw", "--dry-run"])

        assert result.exit_code == 0, _output(result)
        out = _output(result)
        assert out.count("Would add: ") == 3
        assert "Would move it aside to:" in out
        assert "3 to add, 0 kept, next run #004. Dry run: nothing changed." in out
        assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == before
        assert _backup_files(seeded_config) == files

    def test_nothing_to_add_writes_nothing(
        self, cli_runner: CliRunner, seeded_config: AppConfig
    ) -> None:
        _run_backups(cli_runner, 1)
        path = _index_path(seeded_config)
        before = path.read_bytes()
        stat = path.stat()

        result = cli_runner.invoke(app, ["backup", "reindex", "-d", "home-fw"])

        assert result.exit_code == 0, _output(result)
        assert "nothing to add" in _output(result)
        assert path.read_bytes() == before and path.stat().st_mtime_ns == stat.st_mtime_ns

    def test_never_follows_links_out_of_the_device_folder(
        self, cli_runner: CliRunner, seeded_config: AppConfig, tmp_path: Path
    ) -> None:
        _run_backups(cli_runner, 1)
        outside = tmp_path / "outside"
        (outside / "2026").mkdir(parents=True)
        name = "home-fw_2026-01-01_000000_#099_initial.xml"
        (outside / name).write_text("<pfsense/>")
        (outside / "2026" / name).write_text("<pfsense/>")
        root = _device_dir(seeded_config)
        try:
            (root / "linked-dir").symlink_to(outside / "2026", target_is_directory=True)
            (root / name).symlink_to(outside / name)
        except OSError:
            pytest.skip("creating symlinks is not allowed here")

        result = cli_runner.invoke(app, ["backup", "reindex", "-d", "home-fw"])

        assert result.exit_code == 0, _output(result)
        out = _output(result)
        assert "Skipped (a link or outside the device folder): linked-dir" in out
        assert f"Skipped (a link or outside the device folder): {name}" in out
        index = _retention(seeded_config).load_index("home-fw")
        assert len(index.records) == 1 and index.last_run_number == 1, "#099 not taken"
        assert (outside / name).read_text() == "<pfsense/>"

    def test_unreadable_backup_file_fails_but_adds_the_rest(
        self, cli_runner: CliRunner, seeded_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _run_backups(cli_runner, 2)
        _index_path(seeded_config).unlink()
        real = reindex_module.checksum.sha256_file
        calls: list[Path] = []

        def first_fails(path: Path) -> str:
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        monkeypatch.setattr(reindex_module.checksum, "sha256_file", first_fails)
        result = cli_runner.invoke(app, ["backup", "reindex", "-d", "home-fw"])

        assert result.exit_code == 1
        assert "home-fw: could not read" in _output(result)
        assert len(_retention(seeded_config).load_index("home-fw").records) == 1

    def test_a_failed_move_changes_nothing(
        self,
        cli_runner: CliRunner,
        seeded_config: AppConfig,
        broken_index: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def refuse(self: Path, target: Path) -> Path:
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "rename", refuse)
        result = cli_runner.invoke(app, ["backup", "reindex", "-d", "home-fw"])

        assert result.exit_code == 1
        assert "home-fw: reindex failed, nothing changed" in _output(result)
        assert broken_index.read_bytes() == BROKEN
        assert not list(broken_index.parent.glob(f"{INDEX_FILE}.broken-*"))

    def test_unknown_device(self, cli_runner: CliRunner, seeded_config: AppConfig) -> None:
        result = cli_runner.invoke(app, ["backup", "reindex", "-d", "nope"])
        assert result.exit_code == 1
        assert "Device 'nope' not found" in _output(result)

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, not root"
    )
    def test_permission_denied_index_is_reported_not_reset(
        self, cli_runner: CliRunner, seeded_config: AppConfig
    ) -> None:
        """The real field case: Path.exists() said False, so the index was reset."""
        _run_backups(cli_runner, 2)
        path = _index_path(seeded_config)
        before = json.loads(path.read_text())
        path.chmod(0)
        try:
            result = cli_runner.invoke(app, RUN)
            assert result.exit_code == 1
            assert "Permission denied" in _output(result)
        finally:
            path.chmod(0o600)
        assert json.loads(path.read_text()) == before
