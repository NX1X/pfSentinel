"""A cross-process lock file, so two runs never change one backup index at once.

The daily and the weekly task are separate tasks. When the PC is off at
both times, Task Scheduler starts both at the next sign-in. Without a lock,
both runs read backup_index.json, and the later save dropped the records
of the other run.

The lock is held on an open file (``fcntl.flock`` on Linux and macOS,
``msvcrt.locking`` on Windows). The operating system releases it when the
process ends, also after a crash, so a stale lock file never blocks a run.
"""

from __future__ import annotations

import contextlib
import errno
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

# Locks this thread holds already: a nested call does not wait for itself.
_local = threading.local()


def _held() -> set[Path]:
    if not hasattr(_local, "held"):
        _local.held = set()
    held: set[Path] = _local.held
    return held


class LockTimeoutError(RuntimeError):
    """Another process held the lock for longer than the timeout."""


# The lock is taken by another process: wait. Any other error is real.
_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLK", -1)}
# The file system cannot lock (some NFS and SMB mounts): run without a lock,
# as before 1.0.0, rather than fail every run.
_UNSUPPORTED = {errno.ENOLCK, errno.EINVAL, getattr(errno, "EOPNOTSUPP", -1)}
_UNSUPPORTED.add(getattr(errno, "ENOTSUP", -1))


class LockUnsupportedError(OSError):
    """The file system of the lock file does not support locks."""


def _try_lock(fh: IO[bytes]) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in _BUSY:
            return False
        if e.errno in _UNSUPPORTED:
            raise LockUnsupportedError(e.errno, str(e)) from e
        raise
    return True


def _unlock(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, timeout: float = 900.0, poll: float = 1.0) -> Iterator[None]:
    """Hold an exclusive lock on ``path``, waiting up to ``timeout`` seconds.

    The folder of ``path`` must exist. Raises LockTimeoutError when the
    lock is still taken after the timeout.
    """
    key = path.resolve()
    if key in _held():
        yield
        return
    fh = path.open("a+b")
    try:
        deadline = time.monotonic() + timeout
        waited = False
        while True:
            try:
                if _try_lock(fh):
                    break
            except LockUnsupportedError as e:
                logger.warning(f"Cannot lock {path} ({e}); running without the lock")
                yield
                return
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    f"Another pfSentinel process held {path} for more than {int(timeout)} seconds"
                )
            if not waited:
                logger.info(f"Waiting for another pfSentinel run to finish ({path.name})")
                waited = True
            time.sleep(poll)
        _held().add(key)
        try:
            yield
        finally:
            _held().discard(key)
            # Closing the file releases the lock too: an unlock error (the
            # share dropped) must not hide the error of the locked work.
            with contextlib.suppress(OSError):
                _unlock(fh)
    finally:
        fh.close()
