"""Windows Task Scheduler wrappers in utils.platform, with schtasks faked.

`run_command` (the subprocess edge) is replaced; the argument building,
CSV parsing and error handling run for real on any OS.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from pfsentinel.utils import platform as platform_mod
from pfsentinel.utils.platform import (
    create_windows_task,
    delete_windows_task,
    query_windows_task,
)

# One row of `schtasks /Query /FO CSV /NH /V`. Columns: 0 host, 1 task name,
# 2 next run, 3 status, 4 logon mode, 5 last run, 6 last result, 7 author,
# 8 task to run.
ROW = (
    '"PC01","\\pfSentinel\\DailyBackup","21/11/2026 02:00:00","Ready","Interactive only",'
    '"20/11/2026 02:00:01","0","PC01\\user","C:\\pfs\\pfs.exe backup run --non-interactive"'
)


class _Calls:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.args: list[list[str]] = []

    def __call__(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        self.args.append(args)
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_mod, "is_windows", lambda: True)


def _fake_schtasks(monkeypatch: pytest.MonkeyPatch, **result) -> _Calls:
    calls = _Calls(**result)
    monkeypatch.setattr(platform_mod, "run_command", calls)
    return calls


def _missing_schtasks(args, check=True):
    raise FileNotFoundError("schtasks")


class TestCreate:
    def test_check_task_runs_at_sign_in_and_daily(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str] = {}

        def capture(args, check=True):
            with open(args[args.index("/XML") + 1], "rb") as f:
                seen["xml"] = f.read().decode("utf-16")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(platform_mod, "run_command", capture)
        ok = create_windows_task(
            "pfSentinel\\Check", "pfs.exe", "backup check-recent", "CHECK", "09:00"
        )

        assert ok is True
        assert "<LogonTrigger>" in seen["xml"]
        assert "no recent backup" in seen["xml"]

    def test_a_failing_schtasks_returns_false(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_schtasks(monkeypatch, returncode=1, stderr="ERROR: Access is denied.")

        assert create_windows_task("t", "pfs.exe", "backup run", "DAILY", "02:00") is False

    def test_missing_schtasks_returns_false(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_mod, "run_command", _missing_schtasks)

        assert create_windows_task("t", "pfs.exe", "backup run", "DAILY", "02:00") is False

    def test_the_temporary_task_file_is_removed(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_schtasks(monkeypatch)
        create_windows_task("t", "pfs.exe", "backup run", "DAILY", "02:00")

        xml_path = calls.args[0][calls.args[0].index("/XML") + 1]
        assert not os.path.exists(xml_path)

    def test_a_task_file_already_gone_is_not_an_error(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def delete_then_succeed(args, check=True):
            os.unlink(args[args.index("/XML") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(platform_mod, "run_command", delete_then_succeed)

        assert create_windows_task("t", "pfs.exe", "backup run", "DAILY", "02:00") is True


class TestDelete:
    def test_deletes_by_name(self, on_windows: None, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _fake_schtasks(monkeypatch)

        assert delete_windows_task("pfSentinel\\DailyBackup") is True
        assert calls.args == [["schtasks", "/Delete", "/TN", "pfSentinel\\DailyBackup", "/F"]]

    def test_unknown_task_returns_false(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_schtasks(monkeypatch, returncode=1)

        assert delete_windows_task("nope") is False

    def test_missing_schtasks_returns_false(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_mod, "run_command", _missing_schtasks)

        assert delete_windows_task("t") is False


class TestQuery:
    def test_parses_the_verbose_csv_row(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_schtasks(monkeypatch, stdout=f"\n{ROW}\n")

        task = query_windows_task("pfSentinel\\DailyBackup")

        assert task == {
            "exists": True,
            "name": "\\pfSentinel\\DailyBackup",
            "next_run": "21/11/2026 02:00:00",
            "status": "Ready",
            "last_run": "20/11/2026 02:00:01",
            "last_result": 0,
            "task_to_run": "C:\\pfs\\pfs.exe backup run --non-interactive",
        }

    @pytest.mark.parametrize(("raw", "expected"), [("-2147024809", -2147024809), ("N/A", None)])
    def test_last_result_codes(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int | None
    ) -> None:
        _fake_schtasks(monkeypatch, stdout=ROW.replace('"0","PC01', f'"{raw}","PC01'))

        assert query_windows_task("t")["last_result"] == expected

    def test_a_non_numeric_last_result_is_unknown(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_schtasks(monkeypatch, stdout=ROW.replace('"0","PC01', '"0x1","PC01'))

        assert query_windows_task("t")["last_result"] is None

    def test_a_short_row_keeps_what_it_has(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_schtasks(monkeypatch, stdout='"PC01","\\pfSentinel\\X","N/A","Ready"')

        task = query_windows_task("pfSentinel\\X")

        assert task["exists"] is True
        assert task["status"] == "Ready"
        assert task["last_result"] is None
        assert task["task_to_run"] is None

    @pytest.mark.parametrize(
        "result",
        [
            {"returncode": 1, "stderr": "ERROR: The system cannot find the file specified."},
            {"returncode": 0, "stdout": "\n  \n"},
        ],
    )
    def test_missing_task_does_not_exist(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch, result: dict
    ) -> None:
        _fake_schtasks(monkeypatch, **result)

        assert query_windows_task("t") == {"exists": False}

    def test_missing_schtasks_does_not_exist(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_mod, "run_command", _missing_schtasks)

        assert query_windows_task("t") == {"exists": False}
