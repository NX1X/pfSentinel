"""Owner-only access to pfSentinel's folders on Windows.

On Linux pfSentinel writes files 0600 and folders 0700. On Windows a new
folder inherits its parent's access list, which under a user profile or a
data drive often lets every local user read it. ``restrict_to_owner`` gives a
folder an explicit list instead: full control for the current user, SYSTEM
and the Administrators group (as root keeps access on Linux), nothing for
anyone else, inherited by every file and subfolder below it.

Uses the built-in ``icacls`` and ``whoami``, called by absolute path. A
failure is logged once and never stops a backup.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from pfsentinel.utils.logging import get_logger
from pfsentinel.utils.platform import CONSOLE_ENCODING, is_windows

logger = get_logger(__name__)

# Well-known SIDs, so the command works on every Windows language.
_SYSTEM = "*S-1-5-18"
_ADMINISTRATORS = "*S-1-5-32-544"

_done: set[str] = set()


def _system32(exe: str) -> str:
    return os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", exe)


@lru_cache(maxsize=1)
def _current_user_sid() -> str:
    """The current user's SID from ``whoami /user`` (CSV: "domain\\user","S-1-5-...")."""
    out = subprocess.run(
        [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        encoding=CONSOLE_ENCODING,
        errors="replace",
        timeout=15,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    row = next(csv.reader(io.StringIO(out.strip())))
    sid = row[-1].strip()
    if not sid.startswith("S-1-"):
        raise RuntimeError(f"unexpected whoami output: {out[:80]!r}")
    return sid


def icacls_args(path: Path, user_sid: str) -> list[str]:
    """The icacls command that makes ``path`` owner-only (see the module docs)."""
    inherit = "(OI)(CI)" if path.is_dir() else ""
    return [
        _system32("icacls.exe"),
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"*{user_sid}:{inherit}F",
        f"{_SYSTEM}:{inherit}F",
        f"{_ADMINISTRATORS}:{inherit}F",
        "/q",
    ]


def restrict_to_owner(path: Path) -> bool:
    """Make ``path`` owner-only on Windows (no-op elsewhere). True on success.

    Each path is handled once per process; a failure is logged once.
    """
    if not is_windows():
        return True
    key = str(path.resolve()).lower()
    if key in _done or not path.exists():
        return key in _done
    _done.add(key)
    try:
        result = subprocess.run(
            icacls_args(path, _current_user_sid()),
            capture_output=True,
            text=True,
            encoding=CONSOLE_ENCODING,
            errors="replace",
            timeout=60,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as e:
        logger.warning(f"Could not make {path} owner-only: {e}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        logger.warning(f"Could not make {path} owner-only (icacls {result.returncode}): {detail}")
        return False
    logger.debug(f"Owner-only access set on {path}")
    return True
