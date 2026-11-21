"""The cross-process lock that keeps two runs from changing one index at once."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from pfsentinel.utils.locking import LockTimeoutError, file_lock


def _hold_in_child(path: Path, seconds: float) -> subprocess.Popen[str]:
    code = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from pfsentinel.utils.locking import file_lock
        with file_lock(Path({str(path)!r})):
            print("held", flush=True)
            time.sleep({seconds})
        """
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "held"
    return child


def test_a_second_process_waits_and_then_times_out(tmp_path: Path) -> None:
    lock = tmp_path / ".pfsentinel.lock"
    child = _hold_in_child(lock, 5)
    try:
        with pytest.raises(LockTimeoutError, match="Another pfSentinel process"):
            with file_lock(lock, timeout=0.5, poll=0.1):
                pass
    finally:
        child.kill()
        child.wait()


def test_the_lock_is_free_once_the_holder_ends(tmp_path: Path) -> None:
    lock = tmp_path / ".pfsentinel.lock"
    child = _hold_in_child(lock, 0.3)
    child.wait()
    with file_lock(lock, timeout=2, poll=0.1):
        pass


def test_a_killed_holder_leaves_no_stale_lock(tmp_path: Path) -> None:
    lock = tmp_path / ".pfsentinel.lock"
    child = _hold_in_child(lock, 60)
    child.kill()
    child.wait()
    with file_lock(lock, timeout=2, poll=0.1):
        pass


def test_a_nested_call_in_one_thread_does_not_wait_for_itself(tmp_path: Path) -> None:
    lock = tmp_path / ".pfsentinel.lock"
    with file_lock(lock, timeout=0.5):
        with file_lock(lock, timeout=0.5):
            pass


def test_another_thread_waits_its_turn(tmp_path: Path) -> None:
    lock = tmp_path / ".pfsentinel.lock"
    order: list[str] = []

    def second() -> None:
        with file_lock(lock, timeout=5, poll=0.05):
            order.append("second")

    with file_lock(lock):
        worker = threading.Thread(target=second)
        worker.start()
        time.sleep(0.3)
        order.append("first done")
    worker.join(5)
    assert order == ["first done", "second"]
