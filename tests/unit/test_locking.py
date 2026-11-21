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


@pytest.mark.skipif(sys.platform == "win32", reason="uses fcntl")
class TestLockErrors:
    """Found in the review of the lock before rc.3."""

    def test_a_file_system_without_locks_runs_unlocked(self, tmp_path: Path, caplog) -> None:
        import errno
        from unittest.mock import patch

        def no_locks(fd, op):
            raise OSError(errno.ENOLCK, "No locks available")

        with patch("fcntl.flock", no_locks):
            with file_lock(tmp_path / ".lock", timeout=0.2):
                ran = True
        assert ran
        assert "running without the lock" in caplog.text

    def test_another_error_is_raised_at_once(self, tmp_path: Path) -> None:
        import errno
        from unittest.mock import patch

        def broken(fd, op):
            raise OSError(errno.EIO, "I/O error")

        start = time.monotonic()
        with patch("fcntl.flock", broken):
            with pytest.raises(OSError, match="I/O error"):
                with file_lock(tmp_path / ".lock", timeout=5):
                    pass
        assert time.monotonic() - start < 2, "an I/O error is not a busy lock"

    def test_an_unlock_error_does_not_hide_the_real_error(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        with patch("pfsentinel.utils.locking._unlock", side_effect=OSError("share gone")):
            with pytest.raises(ValueError, match="the real error"):
                with file_lock(tmp_path / ".lock"):
                    raise ValueError("the real error")
