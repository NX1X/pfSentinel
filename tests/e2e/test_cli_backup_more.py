"""More `pfs backup` flows through the CLI and the fake SSH server.

Covers: ZFS snapshots and the snapshot list, filesystem archives, the
watch loop, the first-run backup location prompt and the interactive
"what to back up" menu. The fake server answers zfs and tar commands with
canned output; the SSH transport is real.
"""

from __future__ import annotations

import gzip
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.cli.commands import backup as backup_cmd
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.retention import RetentionService
from tests.conftest import SAMPLE_XML_MODIFIED

from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

STREAM = gzip.compress(b"stream bytes from the firewall\n" * 64)
# As on pfSense 2.8: the pool is "pfSense", "/" is ROOT/default, and an
# inactive boot environment also has the mountpoint "/" but is not mounted.
ZFS_FILESYSTEMS = (
    "pfSense\tnone\tno\n"
    "pfSense/ROOT\tnone\tno\n"
    "pfSense/ROOT/old-be\t/\tno\n"
    "pfSense/ROOT/default\t/\tyes\n"
    "pfSense/ROOT/default/cf\t/cf\tyes\n"
)


def _out(result) -> str:
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


def _records(device_id: str = "home-fw"):
    config = AppConfig.load()
    retention = RetentionService(config.backup_policy.resolved_root, config.backup_policy)
    return retention.load_index(device_id).records


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def zfs_server(ssh_server: FakeSSHServer) -> FakeSSHServer:
    # The more exact prefix first: the fake server takes the first match.
    ssh_server.set_exec_response(
        "zfs list -H -t filesystem -o name,mountpoint,mounted",
        stdout=ZFS_FILESYSTEMS,
    )
    ssh_server.set_exec_response("zfs list", stdout="pfSense/ROOT/default\n")
    ssh_server.set_exec_response("zfs snapshot", stdout="")
    ssh_server.set_exec_response("zfs send", stdout=STREAM)
    ssh_server.set_exec_response("zfs destroy", stdout="")
    return ssh_server


# ---------------------------------------------------------------------------
# backup snapshot / snapshot-list
# ---------------------------------------------------------------------------


def test_snapshot_unknown_device_fails(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "ghost"])

    assert result.exit_code == 1
    assert "not found" in _out(result)


def test_snapshot_without_zfs_on_the_device_fails(
    cli_runner: CliRunner, seeded_config: AppConfig
) -> None:
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"])

    assert result.exit_code == 1
    assert "ZFS not available" in _out(result)


def test_first_snapshot_is_full_and_indexed(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"])

    assert result.exit_code == 0, _out(result)
    assert "ZFS snapshot (full)" in _out(result)
    records = _records()
    assert len(records) == 1
    assert records[0].zfs_incremental is False


def test_second_snapshot_is_incremental_on_the_first(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    assert cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"]).exit_code == 0
    time.sleep(1.1)  # snapshot tags have one-second resolution
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"])

    assert result.exit_code == 0, _out(result)
    assert "incremental" in _out(result)
    assert "Base:" in _out(result)


def test_full_flag_forces_a_full_snapshot(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    assert cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"]).exit_code == 0
    time.sleep(1.1)
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw", "--full"])

    assert result.exit_code == 0, _out(result)
    assert "ZFS snapshot (full)" in _out(result)


def test_a_failed_zfs_send_is_no_backup_and_its_snapshot_is_destroyed(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    """Before rc.3, `zfs send | gzip` exited 0 on a failed send: a small file passed as a backup."""
    zfs_server.set_exec_response("zfs send", stderr="cannot send: no such snapshot", exit_code=1)
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"])

    assert result.exit_code == 1
    assert "no such snapshot" in _out(result)
    assert _records() == []
    assert not list(AppConfig.load().backup_policy.resolved_root.rglob("*.zfs*"))
    assert any(cmd.startswith("zfs destroy") for cmd in zfs_server.exec_log)


def test_an_empty_zfs_send_is_no_backup(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    zfs_server.set_exec_response("zfs send", stdout=b"")
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"])

    assert result.exit_code == 1
    assert "sent no data" in _out(result)
    assert _records() == []


def test_the_stream_is_gzipped_here(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    raw = b"raw zfs stream\n" * 64
    zfs_server.set_exec_response("zfs send", stdout=raw)
    assert cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"]).exit_code == 0
    assert not any("| gzip" in cmd for cmd in zfs_server.exec_log)
    (record,) = _records()
    path = AppConfig.load().backup_policy.resolved_root / "home-fw" / record.relative_path
    assert gzip.decompress(path.read_bytes()) == raw


def test_a_chain_at_the_zfs_limit_starts_with_a_new_full_send(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    """Retention removes whole chains, so a chain may not outgrow the ZFS limit."""
    config = AppConfig.load()
    config.backup_policy.max_backups_per_type["zfs"] = 2
    config.save()
    labels = []
    for _ in range(3):
        result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"])
        assert result.exit_code == 0, _out(result)
        labels.append("incremental" if "incremental" in _out(result) else "full")
        time.sleep(1.1)  # snapshot tags have one-second resolution
    assert labels == ["full", "incremental", "full"]


def test_an_incremental_needs_its_base_in_the_local_index(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    """The base snapshot exists on the device, but its local file is gone: send full."""
    assert cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"]).exit_code == 0
    config = AppConfig.load()
    retention = RetentionService(config.backup_policy.resolved_root, config.backup_policy)
    index = retention.load_index("home-fw")
    index.records = []
    retention.save_index(index)
    time.sleep(1.1)
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"])

    assert result.exit_code == 0, _out(result)
    assert "ZFS snapshot (full)" in _out(result)


def test_a_base_destroyed_on_the_device_gives_a_full_send(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    """Before, every later incremental failed until a manual --full."""
    assert cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"]).exit_code == 0
    detect = zfs_server.exec_responses.pop("zfs list")
    zfs_server.set_exec_response(
        "zfs list -H -t snapshot", stderr="dataset does not exist", exit_code=1
    )
    zfs_server.exec_responses["zfs list"] = detect
    time.sleep(1.1)
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"])

    assert result.exit_code == 0, _out(result)
    assert "ZFS snapshot (full)" in _out(result)


def test_a_changed_dataset_gives_a_full_send(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    assert cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"]).exit_code == 0
    config = AppConfig.load()
    config.backup_policy.zfs.dataset = "zroot/ROOT/default"
    config.save()
    time.sleep(1.1)
    result = cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"])

    assert result.exit_code == 0, _out(result)
    assert "ZFS snapshot (full)" in _out(result)


def test_snapshot_list_is_empty_before_any_snapshot(
    cli_runner: CliRunner, seeded_config: AppConfig
) -> None:
    result = cli_runner.invoke(app, ["backup", "snapshot-list", "-d", "home-fw"])

    assert result.exit_code == 0
    assert "No ZFS snapshots found" in _out(result)


def test_snapshot_list_shows_transferred_snapshots(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer
) -> None:
    assert cli_runner.invoke(app, ["backup", "snapshot", "-d", "home-fw"]).exit_code == 0
    result = cli_runner.invoke(app, ["backup", "snapshot-list", "-d", "home-fw"])

    assert result.exit_code == 0, _out(result)
    assert "pfSense/ROOT/default@pfsentinel-" in _out(result)
    assert "Yes" in _out(result)


# ---------------------------------------------------------------------------
# backup archive
# ---------------------------------------------------------------------------


def test_archive_streams_a_tarball_into_the_index(
    cli_runner: CliRunner, seeded_config: AppConfig, ssh_server: FakeSSHServer
) -> None:
    ssh_server.set_exec_response("tar czf", stdout=STREAM)
    result = cli_runner.invoke(
        app, ["backup", "archive", "-d", "home-fw", "--dirs", "/cf/conf,/var/db/rrd"]
    )

    assert result.exit_code == 0, _out(result)
    assert "/cf/conf, /var/db/rrd" in _out(result)
    records = _records()
    assert len(records) == 1
    assert records[0].source_paths == ["/cf/conf", "/var/db/rrd"]


def test_archive_with_no_output_from_tar_fails(
    cli_runner: CliRunner, seeded_config: AppConfig, ssh_server: FakeSSHServer
) -> None:
    ssh_server.set_exec_response("tar czf", stdout=b"")
    result = cli_runner.invoke(app, ["backup", "archive", "-d", "home-fw"])

    assert result.exit_code == 1
    assert "Archive backup failed" in _out(result)
    assert _records() == []


def test_archive_unknown_device_fails(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["backup", "archive", "-d", "ghost"])

    assert result.exit_code == 1
    assert "not found" in _out(result)


@pytest.mark.parametrize(
    ("dirs", "message"),
    [
        ("cf/conf", "absolute path"),
        ("/cf/../etc", "must not contain '..'"),
    ],
)
def test_archive_refuses_unsafe_directories(
    cli_runner: CliRunner, seeded_config: AppConfig, dirs: str, message: str
) -> None:
    result = cli_runner.invoke(app, ["backup", "archive", "-d", "home-fw", "--dirs", dirs])

    assert result.exit_code == 1
    assert message in _out(result)


# ---------------------------------------------------------------------------
# backup watch
# ---------------------------------------------------------------------------

WATCH_INTERVAL = 4242  # unique, so only the watch loop's sleep is intercepted


@pytest.fixture
def watch_ticks(monkeypatch: pytest.MonkeyPatch):
    """Replace the watch loop's sleep; run a callback per tick, stop after the last."""
    real_sleep = time.sleep
    actions: list = []

    def fake_sleep(seconds: float) -> None:
        if seconds != WATCH_INTERVAL:
            real_sleep(seconds)
            return
        if not actions:
            raise KeyboardInterrupt
        actions.pop(0)()

    monkeypatch.setattr(time, "sleep", fake_sleep)
    return actions


def test_watch_unknown_device_fails(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["backup", "watch", "-d", "ghost"])

    assert result.exit_code == 1
    assert "not found" in _out(result)


def test_watch_without_devices_fails(cli_runner: CliRunner, e2e_home: Path) -> None:
    AppConfig().save()
    result = cli_runner.invoke(app, ["backup", "watch"])

    assert result.exit_code == 1
    assert "No enabled devices" in _out(result)


def test_watch_backs_up_when_the_config_changes(
    cli_runner: CliRunner, seeded_config: AppConfig, ssh_server: FakeSSHServer, watch_ticks
) -> None:
    watch_ticks.append(lambda: ssh_server.set_file("/cf/conf/config.xml", SAMPLE_XML_MODIFIED))
    watch_ticks.append(lambda: None)
    result = cli_runner.invoke(
        app, ["backup", "watch", "-i", str(WATCH_INTERVAL), "--desc", "changed-by-test"]
    )

    output = _out(result)
    assert result.exit_code == 0, output
    assert "Initial hash captured" in output
    assert "Config changed" in output
    assert "No change" in output
    assert "Watch stopped" in output
    records = _records()
    assert len(records) == 1
    assert records[0].description == "changed-by-test"


def test_watch_keeps_going_after_a_poll_error(
    cli_runner: CliRunner, e2e_home: Path, tmp_path: Path, watch_ticks
) -> None:
    config = AppConfig()
    config.backup_policy.backup_root = tmp_path / "b"
    config.add_device(
        DeviceConfig(
            id="down-fw",
            label="Down",
            host="127.0.0.1",
            ssh_port=_closed_port(),
            fallback_method=None,
            timeout=5,
        )
    )
    config.save()
    watch_ticks.append(lambda: None)
    result = cli_runner.invoke(app, ["backup", "watch", "-i", str(WATCH_INTERVAL)])

    output = _out(result)
    assert result.exit_code == 0, output
    assert output.count("Poll error") == 2
    assert "Watch stopped" in output


# ---------------------------------------------------------------------------
# First run: where should backups be saved?
# ---------------------------------------------------------------------------

RUN = ["backup", "run", "-d", "home-fw", "--config-only", "--no-notify"]


@pytest.fixture
def unset_root(seeded_config: AppConfig) -> None:
    config = AppConfig.load()
    config.backup_policy.backup_root = None
    config.save()


def test_first_run_asks_for_the_location_and_saves_it(
    cli_runner: CliRunner, unset_root: None, tmp_path: Path
) -> None:
    chosen = tmp_path / "chosen"
    result = cli_runner.invoke(app, RUN, input=f"{chosen}\ny\n")

    assert result.exit_code == 0, _out(result)
    assert "not configured" in _out(result)
    assert AppConfig.load().backup_policy.backup_root == chosen.resolve()
    assert list(chosen.rglob("*.xml*"))


def test_first_run_location_can_be_used_once_without_saving(
    cli_runner: CliRunner, unset_root: None, tmp_path: Path
) -> None:
    chosen = tmp_path / "once"
    result = cli_runner.invoke(app, RUN, input=f"{chosen}\nn\n")

    assert result.exit_code == 0, _out(result)
    assert AppConfig.load().backup_policy.backup_root is None
    assert list(chosen.rglob("*.xml*"))


def test_non_interactive_run_never_asks_for_the_location(
    cli_runner: CliRunner, unset_root: None, tmp_path: Path
) -> None:
    """A scheduled run waited at this prompt (or aborted) before rc.3."""
    result = cli_runner.invoke(app, [*RUN, "--non-interactive"], input="")

    assert result.exit_code == 0, _out(result)
    assert "not configured" not in _out(result)
    root = AppConfig.load().backup_policy.resolved_root
    assert list(root.rglob("*.xml*"))


def test_output_dir_override_is_where_the_files_go(
    cli_runner: CliRunner, seeded_config: AppConfig, tmp_path: Path
) -> None:
    """Before rc.3, -o was printed but the files went to the configured folder."""
    other = tmp_path / "offsite"
    configured = seeded_config.backup_policy.resolved_root
    result = cli_runner.invoke(app, [*RUN, "-o", str(other)])

    assert result.exit_code == 0, _out(result)
    assert list(other.rglob("*.xml*"))
    assert not configured.exists() or not list(configured.rglob("*.xml*"))
    assert AppConfig.load().backup_policy.backup_root == configured


# ---------------------------------------------------------------------------
# Interactive "what would you like to back up?" menu
# ---------------------------------------------------------------------------


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `backup run` see a terminal on stdin, so it shows the menu.

    Only the isatty() check is replaced; the prompts still read the
    CliRunner input.
    """
    import sys

    class _TTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(backup_cmd, "sys", SimpleNamespace(stdin=_TTY(), modules=sys.modules))


RUN_ASK = ["backup", "run", "-d", "home-fw", "--no-notify"]


def test_menu_enter_backs_up_the_config_only(
    cli_runner: CliRunner, seeded_config: AppConfig, terminal: None
) -> None:
    result = cli_runner.invoke(app, RUN_ASK, input="\n")

    assert result.exit_code == 0, _out(result)
    assert "What would you like to back up?" in _out(result)
    assert [r.backup_type.value for r in _records()] == ["config"]


def test_menu_numbers_select_extras(
    cli_runner: CliRunner, seeded_config: AppConfig, ssh_server: FakeSSHServer, terminal: None
) -> None:
    ssh_server.set_file("/var/dhcpd/var/db/dhcpd.leases", b"lease 192.0.2.50 { }\n")
    result = cli_runner.invoke(app, RUN_ASK, input="3\n")

    assert result.exit_code == 0, _out(result)
    assert "Extras: dhcp" in _out(result)
    assert len(_records()) == 2


def test_menu_skips_unknown_choices(
    cli_runner: CliRunner, seeded_config: AppConfig, terminal: None
) -> None:
    result = cli_runner.invoke(app, RUN_ASK, input="99\n")

    assert result.exit_code == 0, _out(result)
    assert "Unknown selection '99'" in _out(result)
    assert len(_records()) == 1


def test_menu_advanced_numbers_turn_on_zfs_and_archive(
    cli_runner: CliRunner, seeded_config: AppConfig, zfs_server: FakeSSHServer, terminal: None
) -> None:
    zfs_server.set_exec_response("tar czf", stdout=STREAM)
    result = cli_runner.invoke(app, RUN_ASK, input="7,8\n")

    output = _out(result)
    assert result.exit_code == 0, output
    assert "ZFS snapshot: enabled" in output
    assert "Filesystem archive: enabled" in output
    # The menu choice is for this run only.
    saved = json.loads(AppConfig.config_path().read_text(encoding="utf-8"))
    assert saved["backup_policy"]["zfs"]["enabled"] is False


@pytest.mark.parametrize("choice", ["A", "B"])
def test_menu_all_choices_include_every_extra(
    cli_runner: CliRunner,
    seeded_config: AppConfig,
    zfs_server: FakeSSHServer,
    terminal: None,
    choice: str,
) -> None:
    zfs_server.set_exec_response("tar czf", stdout=STREAM)
    result = cli_runner.invoke(app, RUN_ASK, input=f"{choice}\n")

    assert "Extras: rrd, pkg, dhcp, aliases, certs, logs" in _out(result)
