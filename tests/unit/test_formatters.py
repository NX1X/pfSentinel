"""Tests for CLI formatters."""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from pfsentinel.cli import formatters
from pfsentinel.models.backup import BackupRecord, BackupType, ChangeCategory
from pfsentinel.models.device import DeviceConfig, DeviceStatus


def _capture_console():
    """Create a Console that writes to a StringIO buffer."""
    buf = io.StringIO()
    return Console(file=buf, force_terminal=True, width=120), buf


def _plain(buf) -> str:
    """Output without the ANSI color codes a terminal console adds."""
    return re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())


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
    LONG = "firewall-homelab-local_2026-11-27_180731_#015_minor.xml.gz.enc"

    def _narrow(self, monkeypatch, records, width=118) -> str:
        buf = io.StringIO()
        monkeypatch.setattr(
            formatters, "console", Console(file=buf, force_terminal=True, width=width)
        )
        formatters.print_backup_table(records)
        return _plain(buf)

    @pytest.mark.parametrize("width", [118, 80])
    def test_a_narrow_window_never_cuts_the_file_name(self, monkeypatch, width):
        """At 118 columns the name was cut to "firewall-homelab-l...": nothing to read.

        Now the name is folded over more lines, and every character is there.
        """
        record = _make_record(device_id="firewall-homelab-local", filename=self.LONG)
        out = self._narrow(monkeypatch, [record], width=width)
        assert "\u2026" not in out
        cells = [line.split("\u2502")[3].strip() for line in out.splitlines() if "\u2502" in line]
        assert "".join(cells) == self.LONG

    def test_one_device_has_no_device_column(self, monkeypatch):
        out = self._narrow(monkeypatch, [_make_record(), _make_record()])
        assert "Device" not in out

    def test_two_devices_have_a_device_column(self, monkeypatch):
        out = self._narrow(monkeypatch, [_make_record(), _make_record(device_id="fw2")])
        assert "Device" in out
        assert "fw2" in out

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

    def test_brackets_in_a_message_are_printed_as_text(self, monkeypatch):
        """'[/lan]' raised MarkupError and '[b]' vanished before rc.3."""
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "err_console", con)
        formatters.print_error("Cannot read F:\\backups\\[old]\\x: rule [/lan] [b]")
        assert "[old]" in _plain(buf)
        assert "[/lan] [b]" in _plain(buf)

    def test_markup_true_keeps_styles(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_info("Run [bold]pfs update[/] now", markup=True)
        assert "Run pfs update now" in _plain(buf)

    def test_record_detail_shows_a_description_with_brackets(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_record_detail(_make_record(description="before [b]upgrade[/b]"))
        assert "before [b]upgrade[/b]" in _plain(buf)


class TestPrintRunHistory:
    def _entries(self):
        from datetime import datetime

        from pfsentinel.services.run_history import RunEntry

        start = datetime(2026, 11, 28, 2, 0, 3)
        return [
            RunEntry(
                id="b",
                device="fw1",
                started=start,
                ended=datetime(2026, 11, 28, 2, 1, 8),
                result="failed",
                error="SSH connection refused [x]",
            ),
            RunEntry(
                id="a",
                device="fw1",
                started=datetime(2026, 11, 27, 2, 0, 3),
                ended=datetime(2026, 11, 27, 2, 0, 38),
                result="ok",
                run=15,
                files=6,
                size_bytes=4 * 1024 * 1024,
                changes="minor",
            ),
            RunEntry(id="c", device="fw1", started=datetime(2026, 11, 26, 2, 0, 3)),
        ]

    def test_each_run_is_one_row(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        formatters.print_run_history(self._entries())
        out = _plain(buf)
        assert "2026-11-28 02:00" in out and "Failed" in out
        assert "SSH connection refused [x]" in out  # brackets are text, not markup
        assert "#15" in out and "OK" in out and "4.0 MB" in out and "35s" in out
        assert "1m 05s" in out
        assert "No end" in out
        assert "Device" not in out

    def test_two_devices_have_a_device_column(self, monkeypatch):
        con, buf = _capture_console()
        monkeypatch.setattr(formatters, "console", con)
        entries = self._entries()
        entries[0].device = "fw2"
        formatters.print_run_history(entries)
        assert "Device" in _plain(buf)
