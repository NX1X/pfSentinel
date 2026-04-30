"""Tests for platform detection and OS-specific helpers."""

from __future__ import annotations

import sys

from pfsentinel.utils.platform import (
    app_config_dir,
    create_windows_task,
    default_backup_dir,
    delete_windows_task,
    get_executable_path,
    is_linux,
    is_macos,
    is_windows,
    query_windows_task,
    run_command,
)


class TestPlatformDetection:
    def test_is_windows_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert is_windows() is True

    def test_is_windows_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert is_windows() is False

    def test_is_linux_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert is_linux() is True

    def test_is_linux_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert is_linux() is False

    def test_is_macos_true(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert is_macos() is True

    def test_is_macos_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert is_macos() is False


class TestPaths:
    def test_app_config_dir(self):
        d = app_config_dir()
        assert d.name == ".pfsentinel"

    def test_default_backup_dir_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        d = default_backup_dir()
        assert "Documents" in str(d)
        assert "pfSentinel" in str(d)

    def test_default_backup_dir_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        d = default_backup_dir()
        assert "pfSentinel" in str(d)
        assert "Documents" not in str(d)


import pytest


class TestRunCommand:
    @pytest.mark.skipif(sys.platform == "win32", reason="echo not on Windows")
    def test_success(self):
        # echo is available on both Linux and Windows (Git Bash)
        result = run_command(["echo", "hello"], check=True)
        assert result.returncode == 0
        assert "hello" in result.stdout


class TestGetExecutablePath:
    def test_frozen_returns_sys_executable(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", "/usr/local/bin/pfs")
        assert get_executable_path() == "/usr/local/bin/pfs"

    def test_normal_returns_module_cmd(self, monkeypatch):
        # Ensure 'frozen' is not set
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen")
        result = get_executable_path()
        assert "-m pfsentinel" in result


class TestWindowsTaskNonWindows:
    """On non-Windows, these should return False/{exists: False}."""

    def test_create_task(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert create_windows_task("test", "pfs", "backup", "DAILY", "02:00") is False

    def test_delete_task(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert delete_windows_task("test") is False

    def test_query_task(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        result = query_windows_task("test")
        assert result["exists"] is False
