"""End-to-end tests for `pfs backup` management commands.

These tests drive the Typer CLI in-process via CliRunner. The SSH
transport is real (paramiko against the FakeSSHServer fixture); the
management commands themselves do not touch the network - they read
the on-disk backup index and files produced by an earlier `backup run`.

Mirrors the structure of test_backup_via_ssh.py - same fixtures,
same shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.backup import BackupRecord
from pfsentinel.models.config import AppConfig
from pfsentinel.services.retention import RetentionService

from tests.e2e.fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e


def _seed_one(cli_runner: CliRunner) -> None:
    """Run a single backup via the real CLI/SSH path."""
    result = cli_runner.invoke(
        app,
        ["backup", "run", "-d", "home-fw", "--config-only", "--no-notify"],
    )
    assert result.exit_code == 0, (
        f"Seed backup failed: exit={result.exit_code}\n"
        f"--- stdout ---\n{result.stdout}\n"
    )


def _load_records(config: AppConfig) -> list[BackupRecord]:
    """Read backup records from the on-disk index for home-fw."""
    backup_root = config.backup_policy.backup_root
    assert backup_root is not None
    retention = RetentionService(backup_root, config.backup_policy)
    return retention.load_index("home-fw").records


@pytest.fixture
def seeded_backup(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> BackupRecord:
    """Produce exactly one backup and return its record."""
    _seed_one(cli_runner)
    records = _load_records(seeded_config)
    assert len(records) == 1, f"Expected 1 record after seed, got {len(records)}"
    return records[0]


# ---------------------------------------------------------------------------
# backup list
# ---------------------------------------------------------------------------


def test_backup_list_reports_empty_when_no_records(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """With no backups on disk, `backup list` prints an empty-state message."""
    result = cli_runner.invoke(app, ["backup", "list"])

    assert result.exit_code == 0, result.stdout
    assert "No backups found" in result.stdout


def test_backup_list_shows_record_after_seed(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """After one seed, `backup list` shows the device id in a table.

    Rich truncates the filename column, so assert on the device id which
    has its own dedicated (non-truncated) column.
    """
    result = cli_runner.invoke(app, ["backup", "list"])

    assert result.exit_code == 0, result.stdout
    assert "home-fw" in result.stdout
    assert "Backups" in result.stdout


def test_backup_list_filtered_by_device(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """`-d home-fw` filters to that device's records."""
    result = cli_runner.invoke(app, ["backup", "list", "-d", "home-fw"])

    assert result.exit_code == 0, result.stdout
    assert "home-fw" in result.stdout
    # Title line includes the device id when filtering.
    assert "Backups for home-fw" in result.stdout


def test_backup_list_unknown_device_returns_empty(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """Filtering by a device that has no backups prints the empty message."""
    result = cli_runner.invoke(app, ["backup", "list", "-d", "no-such-device"])

    assert result.exit_code == 0, result.stdout
    assert "No backups found" in result.stdout


def test_backup_list_json_emits_valid_json_array(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """`--json` emits a parseable JSON array containing the record."""
    result = cli_runner.invoke(app, ["backup", "list", "--json"])

    assert result.exit_code == 0, result.stdout
    # Find the JSON array in the output (Rich may add surrounding whitespace).
    start = result.stdout.find("[")
    end = result.stdout.rfind("]")
    assert start != -1 and end != -1, f"No JSON array in stdout:\n{result.stdout}"
    payload = json.loads(result.stdout[start : end + 1])
    assert isinstance(payload, list)
    assert any(item.get("filename") == seeded_backup.filename for item in payload)


# ---------------------------------------------------------------------------
# backup info
# ---------------------------------------------------------------------------


def test_backup_info_shows_record_details(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """`backup info <filename>` renders a panel with the filename and SHA256."""
    result = cli_runner.invoke(app, ["backup", "info", seeded_backup.filename])

    assert result.exit_code == 0, result.stdout
    assert seeded_backup.filename in result.stdout
    # SHA is rendered in the panel; check at least a prefix shows up.
    assert seeded_backup.sha256[:12] in result.stdout


def test_backup_info_missing_returns_error(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """Unknown filename exits non-zero with a 'not found' message."""
    result = cli_runner.invoke(app, ["backup", "info", "does-not-exist.xml.gz"])

    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "not found" in combined


# ---------------------------------------------------------------------------
# backup verify
# ---------------------------------------------------------------------------


def test_backup_verify_passes_for_good_record(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """A freshly-produced backup verifies successfully."""
    result = cli_runner.invoke(app, ["backup", "verify", seeded_backup.filename])

    assert result.exit_code == 0, result.stdout
    assert "verified OK" in result.stdout or "OK" in result.stdout


def test_backup_verify_fails_when_file_deleted_off_disk(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
    seeded_backup: BackupRecord,
) -> None:
    """Deleting the on-disk file while leaving the index intact triggers a verify failure."""
    backup_root = seeded_config.backup_policy.backup_root
    assert backup_root is not None
    file_path = backup_root / "home-fw" / seeded_backup.relative_path
    assert file_path.exists()
    file_path.unlink()

    result = cli_runner.invoke(app, ["backup", "verify", seeded_backup.filename])

    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "failed" in combined or "not found" in combined


def test_backup_verify_missing_record_errors(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """Verifying a filename that is not in the index errors out cleanly."""
    result = cli_runner.invoke(app, ["backup", "verify", "missing.xml.gz"])

    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "not found" in combined


# ---------------------------------------------------------------------------
# backup delete
# ---------------------------------------------------------------------------


def test_backup_delete_removes_file_and_record(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
    seeded_backup: BackupRecord,
) -> None:
    """`backup delete -y <file>` removes both the file and the index entry."""
    backup_root = seeded_config.backup_policy.backup_root
    assert backup_root is not None
    file_path = backup_root / "home-fw" / seeded_backup.relative_path
    assert file_path.exists()

    result = cli_runner.invoke(app, ["backup", "delete", seeded_backup.filename, "-y"])

    assert result.exit_code == 0, result.stdout
    assert "Deleted" in result.stdout
    assert not file_path.exists()
    assert _load_records(seeded_config) == []


def test_backup_delete_missing_errors(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
) -> None:
    """Deleting an unknown filename exits non-zero with 'not found'."""
    result = cli_runner.invoke(app, ["backup", "delete", "nope.xml.gz", "-y"])

    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "not found" in combined


# ---------------------------------------------------------------------------
# backup restore
# ---------------------------------------------------------------------------


def test_backup_restore_writes_xml_to_target_dir(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
    tmp_path: Path,
) -> None:
    """Restore decompresses the backup into a target directory."""
    target = tmp_path / "restored"
    target.mkdir()

    result = cli_runner.invoke(
        app,
        ["backup", "restore", seeded_backup.filename, "--target", str(target)],
    )

    assert result.exit_code == 0, result.stdout
    # Restored file is the .xml without the .gz suffix.
    restored_files = list(target.iterdir())
    assert restored_files, f"No file restored into {target}"
    restored_path = restored_files[0]
    content = restored_path.read_text(encoding="utf-8")
    assert "<pfsense" in content


def test_backup_restore_missing_errors(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Restore of an unknown filename exits non-zero."""
    target = tmp_path / "restored"
    target.mkdir()

    result = cli_runner.invoke(
        app,
        ["backup", "restore", "ghost.xml.gz", "--target", str(target)],
    )

    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "not found" in combined


# ---------------------------------------------------------------------------
# backup diff
# ---------------------------------------------------------------------------


def test_backup_diff_between_two_records_shows_changes(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
    ssh_server: FakeSSHServer,
    sample_xml_modified: str,
) -> None:
    """Two distinct backups produce a non-empty unified diff."""
    _seed_one(cli_runner)
    # Change what the fake firewall serves, then seed a second backup.
    ssh_server.set_file("/cf/conf/config.xml", sample_xml_modified)
    _seed_one(cli_runner)

    records = _load_records(seeded_config)
    assert len(records) == 2, (
        f"Expected 2 records after two seeds, got {len(records)}: "
        f"{[r.filename for r in records]}"
    )
    # sorted newest-first by retention.sorted_by_date(), but the index
    # records list itself is the in-order list. Sort by created_at so
    # we know which is older.
    older, newer = sorted(records, key=lambda r: r.created_at)

    result = cli_runner.invoke(app, ["backup", "diff", older.filename, newer.filename])

    assert result.exit_code == 0, result.stdout
    # The modified XML adds an <opt1> interface that was not in the original.
    assert "opt1" in result.stdout or "DMZ" in result.stdout


def test_backup_diff_missing_file_a_errors(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """Diff with an unknown 'file_a' exits non-zero."""
    result = cli_runner.invoke(
        app,
        ["backup", "diff", "ghost.xml.gz", seeded_backup.filename],
    )

    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "not found" in combined


# ---------------------------------------------------------------------------
# backup search
# ---------------------------------------------------------------------------


def test_backup_search_by_name_substring_matches(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """`--name` matches a substring of the filename.

    Use --json to avoid Rich's table truncation when asserting on the
    full filename.
    """
    # Filenames typically start with the device id.
    needle = seeded_backup.filename.split("-")[0]
    assert needle, "filename has no '-' to slice"

    result = cli_runner.invoke(app, ["backup", "search", "--name", needle, "--json"])

    assert result.exit_code == 0, result.stdout
    start = result.stdout.find("[")
    end = result.stdout.rfind("]")
    assert start != -1 and end != -1, f"No JSON array in stdout:\n{result.stdout}"
    payload = json.loads(result.stdout[start : end + 1])
    assert any(item.get("filename") == seeded_backup.filename for item in payload)


def test_backup_search_by_name_no_match_reports_empty(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """A name that matches nothing prints the empty-results message."""
    result = cli_runner.invoke(
        app,
        ["backup", "search", "--name", "zzz-no-such-name"],
    )

    assert result.exit_code == 0, result.stdout
    assert "No matching backups" in result.stdout


def test_backup_search_by_date_today_matches(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """Filtering by today's date hits the just-created record."""
    date_str = seeded_backup.created_at.strftime("%Y-%m-%d")

    result = cli_runner.invoke(app, ["backup", "search", "--date", date_str, "--json"])

    assert result.exit_code == 0, result.stdout
    start = result.stdout.find("[")
    end = result.stdout.rfind("]")
    assert start != -1 and end != -1, f"No JSON array in stdout:\n{result.stdout}"
    payload = json.loads(result.stdout[start : end + 1])
    assert any(item.get("filename") == seeded_backup.filename for item in payload)


def test_backup_search_by_date_other_day_empty(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """An unrelated date returns the empty-results message."""
    result = cli_runner.invoke(app, ["backup", "search", "--date", "1999-01-01"])

    assert result.exit_code == 0, result.stdout
    assert "No matching backups" in result.stdout


def test_backup_search_min_size_zero_includes_all(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """`--min-size 0` matches every record (size always >= 0)."""
    result = cli_runner.invoke(app, ["backup", "search", "--min-size", "0", "--json"])

    assert result.exit_code == 0, result.stdout
    start = result.stdout.find("[")
    end = result.stdout.rfind("]")
    assert start != -1 and end != -1, f"No JSON array in stdout:\n{result.stdout}"
    payload = json.loads(result.stdout[start : end + 1])
    assert any(item.get("filename") == seeded_backup.filename for item in payload)


def test_backup_search_min_size_huge_excludes_all(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """An impossibly large `--min-size` filters every record out."""
    result = cli_runner.invoke(
        app,
        ["backup", "search", "--min-size", "1000000"],  # 1 GB
    )

    assert result.exit_code == 0, result.stdout
    assert "No matching backups" in result.stdout


def test_backup_search_max_size_zero_excludes_all(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """`--max-size 0` excludes any record that has any bytes."""
    assert seeded_backup.size_bytes > 0
    result = cli_runner.invoke(app, ["backup", "search", "--max-size", "0"])

    assert result.exit_code == 0, result.stdout
    assert "No matching backups" in result.stdout


def test_backup_search_by_changes_label_matches_initial(
    cli_runner: CliRunner,
    seeded_backup: BackupRecord,
) -> None:
    """The first backup carries the INITIAL change category; search hits it."""
    # changes_label looks like 'initial' or 'initial+...' for the first backup.
    assert "initial" in seeded_backup.changes_label, (
        f"Expected 'initial' in changes_label, got {seeded_backup.changes_label!r}"
    )

    result = cli_runner.invoke(app, ["backup", "search", "--changes", "initial", "--json"])

    assert result.exit_code == 0, result.stdout
    start = result.stdout.find("[")
    end = result.stdout.rfind("]")
    assert start != -1 and end != -1, f"No JSON array in stdout:\n{result.stdout}"
    payload = json.loads(result.stdout[start : end + 1])
    assert any(item.get("filename") == seeded_backup.filename for item in payload)
