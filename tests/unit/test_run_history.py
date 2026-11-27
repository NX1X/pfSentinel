"""The run history: one entry per backup run, failed runs included."""

from __future__ import annotations

import json
import stat
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from pfsentinel.services import run_history

T0 = datetime(2026, 11, 28, 2, 0, 3)


def _lines() -> list[dict]:
    text = run_history.history_path().read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


class TestBeginAndFinish:
    def test_a_clean_run_is_ok(self) -> None:
        entry = run_history.begin("fw1", now=T0)
        run_history.finish(
            entry,
            run=12,
            files=6,
            size_bytes=4_300_000,
            changes="firewall",
            now=T0 + timedelta(seconds=35),
        )
        (run,) = run_history.load()
        assert (run.device, run.result, run.run, run.files) == ("fw1", "ok", 12, 6)
        assert run.changes == "firewall"
        assert run.seconds == 35
        assert run.detail == ""

    def test_an_error_makes_the_run_failed(self) -> None:
        entry = run_history.begin("fw1", now=T0)
        run_history.finish(entry, error="SSH connection refused", now=T0)
        (run,) = run_history.load()
        assert run.result == "failed"
        assert run.files == 0 and run.run is None
        assert run.detail == "SSH connection refused"

    def test_failed_parts_make_the_run_partial(self) -> None:
        entry = run_history.begin("fw1", now=T0)
        run_history.finish(entry, run=3, files=1, problems=["Extra backup 'rrd' failed: x"])
        (run,) = run_history.load()
        assert run.result == "partial"
        assert "rrd" in run.detail

    def test_a_run_with_no_end_line_is_unfinished(self) -> None:
        """The PC shut down, or the task hit its time limit: no end was written."""
        run_history.begin("fw1", now=T0)
        (run,) = run_history.load()
        assert run.result == "unfinished"
        assert run.ended is None and run.seconds is None
        assert "No end recorded" in run.detail

    def test_start_and_end_are_two_lines_for_one_run(self) -> None:
        entry = run_history.begin("fw1", now=T0)
        run_history.finish(entry, run=1, files=1, now=T0)
        lines = _lines()
        assert len(lines) == 2
        assert lines[0]["id"] == lines[1]["id"]
        assert lines[0]["result"] == "unfinished" and lines[1]["result"] == "ok"
        assert len(run_history.load()) == 1

    def test_the_version_is_recorded(self) -> None:
        from pfsentinel import __version__

        run_history.finish(run_history.begin("fw1"))
        assert run_history.load()[0].version == __version__

    def test_secrets_and_line_breaks_are_removed_from_the_error(self) -> None:
        token = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
        entry = run_history.begin("fw1")
        run_history.finish(entry, error=f"POST https://api.telegram.org/bot{token}/send\nfailed")
        detail = run_history.load()[0].detail
        assert token not in detail
        assert "\n" not in detail

    def test_a_long_error_is_cut(self) -> None:
        entry = run_history.begin("fw1")
        run_history.finish(entry, error="x" * 1000)
        assert len(run_history.load()[0].error) == 300


class TestLoad:
    def _three(self) -> None:
        for day, device, error in ((1, "fw1", ""), (2, "fw2", "boom"), (3, "fw1", "")):
            when = datetime(2026, 11, day, 2, 0)
            run_history.finish(run_history.begin(device, now=when), files=1, error=error, now=when)

    def test_newest_first(self) -> None:
        self._three()
        assert [e.started.day for e in run_history.load()] == [3, 2, 1]

    def test_filter_by_device(self) -> None:
        self._three()
        assert [e.started.day for e in run_history.load(device="fw1")] == [3, 1]

    def test_limit(self) -> None:
        self._three()
        assert [e.started.day for e in run_history.load(limit=2)] == [3, 2]

    def test_problems_only(self) -> None:
        self._three()
        assert [e.device for e in run_history.load(problems_only=True)] == ["fw2"]

    def test_no_file_is_an_empty_history(self) -> None:
        assert run_history.load() == []

    def test_a_broken_line_is_skipped(self) -> None:
        """A crash can cut the last line short: the other runs still show."""
        run_history.finish(run_history.begin("fw1", now=T0), files=1, now=T0)
        with run_history.history_path().open("a", encoding="utf-8") as f:
            f.write('{"id": "abc", "device": "fw1", "star\n')
            f.write("[1, 2]\n")
        assert len(run_history.load()) == 1

    def test_a_line_cut_inside_a_character_hides_nothing_else(self) -> None:
        """A short write can end in the middle of a UTF-8 character."""
        run_history.finish(run_history.begin("fw1", now=T0), files=1, now=T0)
        with run_history.history_path().open("ab") as f:
            f.write(b'{"id": "x", "error": "caf' + b"\xc3\n")
        later = T0 + timedelta(days=1)
        run_history.finish(run_history.begin("fw1", now=later), files=1, now=later)
        assert [e.started.day for e in run_history.load()] == [29, 28]


class TestFile:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_the_file_is_readable_by_the_owner_only(self) -> None:
        run_history.begin("fw1")
        mode = stat.S_IMODE(run_history.history_path().stat().st_mode)
        assert mode == 0o600

    def test_the_file_is_cut_back_when_it_grows(self) -> None:
        with patch.object(run_history, "MAX_RUNS", 5), patch.object(run_history, "KEEP_RUNS", 3):
            for minute in range(7):
                when = T0 + timedelta(minutes=minute)
                run_history.finish(run_history.begin("fw1", now=when), files=1, now=when)
        runs = run_history.load()
        # Cut to 3 at the 6th run, then the 7th was added.
        assert [e.started.minute for e in runs] == [6, 5, 4, 3]
        assert len(_lines()) == 5  # 3 kept runs, then a start and an end line

    def test_a_write_error_never_raises(self) -> None:
        with patch.object(run_history, "_append", side_effect=OSError("disk full")):
            entry = run_history.begin("fw1")
            run_history.finish(entry, files=1)
        assert run_history.load() == []

    def test_a_value_that_is_not_json_never_raises(self) -> None:
        entry = run_history.begin("fw1")
        run_history.finish(entry, run=object(), files=1)  # type: ignore[arg-type]
        assert run_history.load()[0].run is None
