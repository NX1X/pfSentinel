"""Owner-only folders on Windows (0.4.0)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pfsentinel.utils import permissions


def test_command_removes_inheritance_and_grants_only_owner_system_admins(tmp_path: Path) -> None:
    args = permissions.icacls_args(tmp_path, "S-1-5-21-1-2-3-1001")
    assert args[0].lower().endswith("icacls.exe")
    assert args[1:4] == [str(tmp_path), "/inheritance:r", "/grant:r"]
    assert args[4:7] == [
        "*S-1-5-21-1-2-3-1001:(OI)(CI)F",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F",
    ]


def test_a_file_gets_no_inheritance_flags(tmp_path: Path) -> None:
    f = tmp_path / "config.json"
    f.write_text("{}")
    assert permissions.icacls_args(f, "S-1-5-21-9")[4] == "*S-1-5-21-9:F"


def test_no_op_on_linux(tmp_path: Path) -> None:
    with (
        patch.object(permissions, "is_windows", return_value=False),
        patch.object(permissions.subprocess, "run") as run,
    ):
        assert permissions.restrict_to_owner(tmp_path)
    run.assert_not_called()


def test_a_failure_is_a_warning_not_an_error(tmp_path: Path, caplog) -> None:
    with (
        patch.object(permissions, "is_windows", return_value=True),
        patch.object(permissions, "_current_user_sid", return_value="S-1-5-21-9"),
        patch.object(
            permissions.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 5, "", "Access is denied."),
        ),
    ):
        assert permissions.restrict_to_owner(tmp_path / "missing") is False
        (tmp_path / "y").mkdir()
        assert permissions.restrict_to_owner(tmp_path / "y") is False
    assert "Access is denied" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="real icacls on Windows")
def test_real_windows_acl_is_owner_only(tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "inside.txt").write_text("secret")
    assert permissions.restrict_to_owner(folder)
    acl = subprocess.run(
        ["icacls", str(folder / "inside.txt")], capture_output=True, text=True
    ).stdout
    assert "BUILTIN\\Users" not in acl
    assert "Everyone" not in acl
    assert "NT AUTHORITY\\Authenticated Users" not in acl
    assert "NT AUTHORITY\\SYSTEM" in acl
