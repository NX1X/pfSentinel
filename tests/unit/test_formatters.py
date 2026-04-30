"""Tests for CLI formatters."""

from __future__ import annotations

import io

from rich.console import Console

from pfsentinel.cli import formatters
from pfsentinel.models.backup import BackupRecord, BackupType, ChangeCategory
from pfsentinel.models.device import DeviceConfig, DeviceStatus


def _capture_console():
    """Create a Console that writes to a StringIO buffer."""
    buf = io.StringIO()
    return Console(file=buf, force_terminal=True, width=120), buf


def _make_record(**overrides):
    defaults = {
        "device_id": "fw1",
        "filename": "fw1_backup.xml.gz",
        "relative_path": "2025/03/05/fw1_backup.xml.gz",
        "size_bytes": 5120,
        "sha256": "abc123def",
        "changes": [ChangeCategory.MINOR],
        "backup_type": BackupType.CONFIG,
        "compressed": True,
        "verified": True,
    }
    defaults.update(overrides)
    return BackupRecord(**defaults)


def _make_device(**overrides):
    defaults = {"id": "fw1", "label": "Firewall 1", "host": "192.168.1.1"}
    defaults.update(overrides)
    return DeviceConfig(**defaults)


class TestPrintBackupTable:
    def test_empty_list_no_error(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_backup_table([])

    def test_single_record(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_backup_table([_make_record()])
        output = buf.getvalue()
        assert "fw1" in output

    def test_size_formatting_kb(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_backup_table([_make_record(size_bytes=500000)])
        output = buf.getvalue()
        assert "KB" in output

    def test_size_formatting_mb(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_backup_table([_make_record(size_bytes=2_000_000)])
        output = buf.getvalue()
        assert "MB" in output


class TestPrintDeviceTable:
    def test_single_device_no_status(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_device_table([_make_device()])
        output = buf.getvalue()
        assert "fw1" in output

    def test_device_reachable(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        status = DeviceStatus(device_id="fw1", ssh_reachable=True)
        formatters.print_device_table([_make_device()], {"fw1": status})
        output = buf.getvalue()
        assert "Reachable" in output

    def test_device_unreachable(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        status = DeviceStatus(device_id="fw1", error="timeout")
        formatters.print_device_table([_make_device()], {"fw1": status})
        output = buf.getvalue()
        assert "Unreachable" in output


class TestPrintRecordDetail:
    def test_prints_panel(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_record_detail(_make_record())
        output = buf.getvalue()
        assert "fw1" in output
        assert "abc123def" in output

    def test_source_paths_shown(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_record_detail(_make_record(source_paths=["/var/db/rrd"]))
        output = buf.getvalue()
        assert "/var/db/rrd" in output

    def test_zfs_info_shown(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_record_detail(
            _make_record(
                zfs_snapshot_name="zroot@snap1",
                zfs_incremental=True,
                zfs_base_snapshot="zroot@snap0",
            )
        )
        output = buf.getvalue()
        assert "zroot@snap1" in output


class TestPrintHelpers:
    def test_print_success(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_success("all good")
        assert "all good" in buf.getvalue()

    def test_print_error(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "err_console", con)
        formatters.print_error("something broke")
        assert "something broke" in buf.getvalue()

    def test_print_warning(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_warning("careful")
        assert "careful" in buf.getvalue()

    def test_print_info(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_info("FYI")
        assert "FYI" in buf.getvalue()
