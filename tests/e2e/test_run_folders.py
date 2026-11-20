"""One folder per backup run (0.3.0): <device>/YYYY/MM/DD/<HHMMSS>_#NNN/.

The config and every extra of a run share that folder, and the run number
counts the device's runs without ever resetting. Files written by earlier
versions keep their old paths and stay usable.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.backup import BackupIndex
from pfsentinel.models.config import AppConfig
from pfsentinel.services.retention import RetentionService, prune_empty_dirs

from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

RUN = ["backup", "run", "-d", "home-fw", "--include", "dhcp", "--no-notify"]
RUN_FOLDER = re.compile(r"^\d{4}/\d{2}/\d{2}/\d{6}_#(\d{3,})$")


def _output(result) -> str:
    return (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def with_leases(ssh_server: FakeSSHServer) -> FakeSSHServer:
    ssh_server.set_file("/var/dhcpd/var/db/dhcpd.leases", b"lease 192.0.2.50 { }\n")
    return ssh_server


def _index(config: AppConfig) -> BackupIndex:
    root = config.backup_policy.resolved_root
    return RetentionService(root, config.backup_policy).load_index("home-fw")


def test_config_and_extras_share_one_run_folder(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases
) -> None:
    result = cli_runner.invoke(app, RUN)
    assert result.exit_code == 0, _output(result)

    records = _index(seeded_config).records
    assert sorted(r.backup_type.value for r in records) == ["config", "dhcp"]
    folders = {str(Path(r.relative_path).parent).replace("\\", "/") for r in records}
    assert len(folders) == 1, folders
    folder = folders.pop()
    match = RUN_FOLDER.match(folder)
    assert match, folder
    assert {r.run_number for r in records} == {int(match.group(1))}

    root = seeded_config.backup_policy.resolved_root / "home-fw" / folder
    assert sorted(p.name.split("_")[-1] for p in root.iterdir()) == [
        "dhcp.txt.gz",
        "initial.xml.gz",
    ]


def test_run_numbers_count_up_and_never_reset(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases
) -> None:
    for _ in range(3):
        assert cli_runner.invoke(app, RUN).exit_code == 0

    numbers = sorted({r.run_number for r in _index(seeded_config).records})
    assert numbers == [1, 2, 3]
    assert _index(seeded_config).last_run_number == 3


def test_old_layout_index_continues_numbering_and_old_files_stay_usable(
    cli_runner: CliRunner, seeded_config: AppConfig, sample_xml: str, with_leases
) -> None:
    """A 0.2.x index: daily sequences, files under <device>/YYYY/MM/DD/."""
    root = seeded_config.backup_policy.resolved_root / "home-fw"
    old_rel = "2026/11/19/home-fw_2026-11-19_020000_#001_initial.xml.gz"
    (root / old_rel).parent.mkdir(parents=True)
    (root / old_rel).write_bytes(gzip.compress(sample_xml.encode()))
    v2 = {
        "device_id": "home-fw",
        "schema_version": 2,
        "records": [
            {
                "device_id": "home-fw",
                "filename": Path(old_rel).name,
                "relative_path": old_rel,
                "created_at": "2026-11-19T02:00:00",
                "compressed": True,
                "sequence": 1,
                "backup_type": "config",
            },
            {
                "device_id": "home-fw",
                "filename": "home-fw_2026-11-19_020000_#001_dhcp.txt.gz",
                "relative_path": "dhcp/2026/11/19/home-fw_2026-11-19_020000_#001_dhcp.txt.gz",
                "created_at": "2026-11-19T02:00:00",
                "sequence": 1,
                "backup_type": "dhcp",
            },
        ],
    }
    (root / "backup_index.json").write_text(json.dumps(v2))

    assert cli_runner.invoke(app, RUN).exit_code == 0
    index = _index(seeded_config)
    assert index.schema_version == 3
    new = [r for r in index.records if r.run_number is not None]
    assert {r.run_number for r in new} == {2}, "one old run, so the next is #002"

    # The old file is still listed, verified and restorable at its old path.
    verify = cli_runner.invoke(app, ["backup", "verify", Path(old_rel).name])
    assert verify.exit_code == 0, _output(verify)
    assert (root / old_rel).is_file()


def test_retention_removes_empty_run_folders(tmp_path: Path) -> None:
    device_dir = tmp_path / "home-fw"
    run_dir = device_dir / "2026" / "11" / "20" / "085016_#142"
    run_dir.mkdir(parents=True)
    keep = device_dir / "2026" / "11" / "19" / "020000_#141"
    keep.mkdir(parents=True)
    (keep / "file").write_text("x")

    prune_empty_dirs(run_dir, device_dir)

    assert not (device_dir / "2026" / "11" / "20").exists()
    assert keep.is_dir(), "a folder with files stays"
    assert device_dir.is_dir(), "never above the device folder"


def test_run_folder_name_matches_the_requested_layout() -> None:
    from pfsentinel.models.backup import BackupRun

    run = BackupRun(device_id="fw", number=7, started_at=datetime(2026, 11, 20, 8, 50, 16))
    assert run.folder == "2026/11/20/085016_#007"
    assert BackupRun(device_id="fw", number=1234, started_at=run.started_at).folder.endswith(
        "_#1234"
    )
