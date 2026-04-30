"""Tests for the self-update service — targeting 100% coverage."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import responses

from pfsentinel import __version__
from pfsentinel.services.updater import UpdateError, UpdateService

# ── Helpers ────────────────────────────────────────────────────────────

CURRENT_VERSION = __version__
GITHUB_API_URL = UpdateService.GITHUB_API_URL
CHECKSUMS_URL = "https://github.com/nx1x/pfSentinel/releases/download/v99.0.0/checksums-sha256.txt"

FAKE_RELEASE = {
    "tag_name": "v99.0.0",
    "html_url": "https://github.com/nx1x/pfSentinel/releases/tag/v99.0.0",
    "assets": [
        {
            "name": "pfs",
            "browser_download_url": "https://github.com/nx1x/pfSentinel/releases/download/v99.0.0/pfs",
        },
        {
            "name": "pfs.exe",
            "browser_download_url": "https://github.com/nx1x/pfSentinel/releases/download/v99.0.0/pfs.exe",
        },
        {
            "name": "checksums-sha256.txt",
            "browser_download_url": CHECKSUMS_URL,
        },
    ],
}


def _checksums_body(*entries: tuple[str, bytes]) -> str:
    """Generate a checksums-sha256.txt body from (name, content) pairs."""
    lines = []
    for name, content in entries:
        h = hashlib.sha256(content).hexdigest()
        lines.append(f"{h}  {name}")
    return "\n".join(lines) + "\n"


FAKE_RELEASE_CURRENT = {
    "tag_name": "v0.4.0",
    "html_url": "https://github.com/nx1x/pfSentinel/releases/tag/v0.4.0",
    "assets": [],
}

FAKE_RELEASE_INVALID_VERSION = {
    "tag_name": "not-a-version",
    "html_url": "https://github.com/nx1x/pfSentinel/releases/tag/not-a-version",
    "assets": [],
}


def _make_service(tmp_path: Path, state: dict | None = None) -> UpdateService:
    """Create an UpdateService with config dir pointing to tmp_path."""
    state_path = tmp_path / "update_check.json"
    if state is not None:
        state_path.write_text(json.dumps(state), encoding="utf-8")

    with patch("pfsentinel.services.updater.app_config_dir", return_value=tmp_path):
        svc = UpdateService()
    # Keep the config dir override for subsequent calls
    svc._config_dir = tmp_path
    svc._state_path = tmp_path / "update_check.json"
    svc._backup_dir = tmp_path / "update_backup"
    return svc


# ── State persistence ──────────────────────────────────────────────────


class TestLoadState:
    def test_returns_dict_from_valid_json(self, tmp_path):
        svc = _make_service(tmp_path, state={"foo": "bar"})
        assert svc._state == {"foo": "bar"}

    def test_returns_empty_dict_on_missing_file(self, tmp_path):
        svc = _make_service(tmp_path)
        assert svc._state == {}

    def test_returns_empty_dict_on_corrupted_file(self, tmp_path):
        state_path = tmp_path / "update_check.json"
        state_path.write_text("NOT JSON {{{", encoding="utf-8")
        svc = _make_service(tmp_path)
        assert svc._state == {}


class TestSaveState:
    def test_roundtrip(self, tmp_path):
        svc = _make_service(tmp_path)
        svc._state = {"key": "value", "num": 42}
        svc._save_state()

        data = json.loads(svc._state_path.read_text(encoding="utf-8"))
        assert data == {"key": "value", "num": 42}

    def test_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        svc = _make_service(tmp_path)
        svc._config_dir = nested
        svc._state_path = nested / "update_check.json"
        svc._state = {"a": 1}
        svc._save_state()

        assert svc._state_path.exists()

    def test_cleans_up_temp_on_failure(self, tmp_path):
        svc = _make_service(tmp_path)
        svc._state = {"x": 1}

        with patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                svc._save_state()

        # Temp file should be cleaned up
        temps = list(tmp_path.glob("*.tmp"))
        assert len(temps) == 0


# ── Detection ──────────────────────────────────────────────────────────


class TestDetectInstallMethod:
    def test_frozen_binary(self, tmp_path):
        svc = _make_service(tmp_path)
        with patch.object(svc, "_detect_install_method", wraps=svc._detect_install_method):
            with patch("pfsentinel.services.updater.sys") as mock_sys:
                mock_sys.frozen = True
                # Need to call directly since sys is mocked at module level
                result = svc._detect_install_method()
        assert result == "binary"

    def test_pipx_in_prefix(self, tmp_path, monkeypatch):
        svc = _make_service(tmp_path)
        monkeypatch.setattr("sys.prefix", "/home/user/.local/pipx/venvs/pfsentinel")
        monkeypatch.delattr("sys.frozen", raising=False)
        assert svc._detect_install_method() == "pipx"

    def test_pipx_detected_via_list(self, tmp_path, monkeypatch):
        svc = _make_service(tmp_path)
        monkeypatch.setattr("sys.prefix", "/usr/lib/python3.13")
        monkeypatch.delattr("sys.frozen", raising=False)

        mock_result = MagicMock()
        mock_result.stdout = "pfsentinel 0.4.0\n"
        with (
            patch("shutil.which", return_value="/usr/bin/pipx"),
            patch("subprocess.run", return_value=mock_result),
        ):
            assert svc._detect_install_method() == "pipx"

    def test_pipx_list_fails_falls_back_to_pip(self, tmp_path, monkeypatch):
        svc = _make_service(tmp_path)
        monkeypatch.setattr("sys.prefix", "/usr/lib/python3.13")
        monkeypatch.delattr("sys.frozen", raising=False)

        with (
            patch("shutil.which", return_value="/usr/bin/pipx"),
            patch("subprocess.run", side_effect=OSError("no pipx")),
        ):
            assert svc._detect_install_method() == "pip"

    def test_pipx_list_no_pfsentinel(self, tmp_path, monkeypatch):
        svc = _make_service(tmp_path)
        monkeypatch.setattr("sys.prefix", "/usr/lib/python3.13")
        monkeypatch.delattr("sys.frozen", raising=False)

        mock_result = MagicMock()
        mock_result.stdout = "otherpkg 1.0.0\n"
        with (
            patch("shutil.which", return_value="/usr/bin/pipx"),
            patch("subprocess.run", return_value=mock_result),
        ):
            assert svc._detect_install_method() == "pip"

    def test_no_pipx_on_path(self, tmp_path, monkeypatch):
        svc = _make_service(tmp_path)
        monkeypatch.setattr("sys.prefix", "/usr/lib/python3.13")
        monkeypatch.delattr("sys.frozen", raising=False)

        with patch("shutil.which", return_value=None):
            assert svc._detect_install_method() == "pip"


class TestCurrentVersion:
    def test_returns_version_object(self, tmp_path):
        svc = _make_service(tmp_path)
        v = svc.current_version()
        assert str(v) == CURRENT_VERSION


# ── should_auto_check ──────────────────────────────────────────────────


class TestShouldAutoCheck:
    def test_true_when_no_timestamp(self, tmp_path):
        svc = _make_service(tmp_path)
        assert svc.should_auto_check() is True

    def test_false_when_checked_recently(self, tmp_path):
        recent = datetime.now(UTC) - timedelta(hours=1)
        svc = _make_service(tmp_path, state={"last_check_ts": recent.isoformat()})
        assert svc.should_auto_check() is False

    def test_true_when_checked_long_ago(self, tmp_path):
        old = datetime.now(UTC) - timedelta(hours=25)
        svc = _make_service(tmp_path, state={"last_check_ts": old.isoformat()})
        assert svc.should_auto_check() is True

    def test_true_on_invalid_timestamp(self, tmp_path):
        svc = _make_service(tmp_path, state={"last_check_ts": "not-a-date"})
        assert svc.should_auto_check() is True

    def test_handles_naive_datetime(self, tmp_path):
        # Naive datetime (no tzinfo) — should be treated as UTC
        recent = datetime.now(UTC) - timedelta(hours=1)
        naive_str = recent.replace(tzinfo=None).isoformat()
        svc = _make_service(tmp_path, state={"last_check_ts": naive_str})
        assert svc.should_auto_check() is False

    def test_true_on_none_timestamp(self, tmp_path):
        svc = _make_service(tmp_path, state={"last_check_ts": None})
        assert svc.should_auto_check() is True


# ── check ──────────────────────────────────────────────────────────────


class TestCheck:
    @responses.activate
    def test_finds_update_when_newer_version(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE, status=200)

        svc = _make_service(tmp_path)
        with patch.object(svc, "_detect_install_method", return_value="pip"):
            result = svc.check(force=True)

        assert result is not None
        assert result["latest"] == "99.0.0"
        assert result["current"] == CURRENT_VERSION
        assert result["tag"] == "v99.0.0"
        assert result["install_method"] == "pip"

    @responses.activate
    def test_returns_none_when_up_to_date(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE_CURRENT, status=200)

        svc = _make_service(tmp_path)
        result = svc.check(force=True)
        assert result is None

    @responses.activate
    def test_returns_none_on_invalid_version(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE_INVALID_VERSION, status=200)

        svc = _make_service(tmp_path)
        result = svc.check(force=True)
        assert result is None

    @responses.activate
    def test_caches_state_after_check(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE, status=200)

        svc = _make_service(tmp_path)
        with patch.object(svc, "_detect_install_method", return_value="pip"):
            svc.check(force=True)

        assert svc._state["latest_version"] == "99.0.0"
        assert svc._state["latest_tag"] == "v99.0.0"
        assert "last_check_ts" in svc._state

    @responses.activate
    def test_finds_linux_asset(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE, status=200)

        svc = _make_service(tmp_path)
        with (
            patch("pfsentinel.services.updater.is_windows", return_value=False),
            patch.object(svc, "_detect_install_method", return_value="binary"),
        ):
            svc.check(force=True)

        assert "pfs" in svc._state["download_url"]
        assert "pfs.exe" not in svc._state["download_url"]

    @responses.activate
    def test_finds_windows_asset(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE, status=200)

        svc = _make_service(tmp_path)
        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch.object(svc, "_detect_install_method", return_value="binary"),
        ):
            svc.check(force=True)

        assert svc._state["download_url"].endswith("pfs.exe")

    @responses.activate
    def test_empty_download_url_when_no_matching_asset(self, tmp_path):
        release = {**FAKE_RELEASE, "assets": [{"name": "other.zip", "browser_download_url": "x"}]}
        responses.add(responses.GET, GITHUB_API_URL, json=release, status=200)

        svc = _make_service(tmp_path)
        with patch.object(svc, "_detect_install_method", return_value="pip"):
            svc.check(force=True)

        assert svc._state["download_url"] == ""

    def test_uses_cache_when_not_force_and_recent(self, tmp_path):
        recent = datetime.now(UTC) - timedelta(hours=1)
        svc = _make_service(
            tmp_path,
            state={
                "last_check_ts": recent.isoformat(),
                "latest_version": "99.0.0",
                "latest_tag": "v99.0.0",
                "release_notes_url": "https://example.com",
            },
        )
        with patch.object(svc, "_detect_install_method", return_value="pip"):
            result = svc.check(force=False)

        assert result is not None
        assert result["latest"] == "99.0.0"

    @responses.activate
    def test_http_error_propagates(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json={"message": "rate limited"}, status=403)

        svc = _make_service(tmp_path)
        with pytest.raises(Exception):
            svc.check(force=True)


# ── _cached_result ─────────────────────────────────────────────────────


class TestCachedResult:
    def test_returns_none_when_no_cached_version(self, tmp_path):
        svc = _make_service(tmp_path)
        assert svc._cached_result() is None

    def test_returns_dict_when_newer(self, tmp_path):
        svc = _make_service(
            tmp_path,
            state={
                "latest_version": "99.0.0",
                "latest_tag": "v99.0.0",
                "release_notes_url": "https://example.com",
            },
        )
        with patch.object(svc, "_detect_install_method", return_value="pip"):
            result = svc._cached_result()

        assert result is not None
        assert result["latest"] == "99.0.0"

    def test_returns_none_when_current(self, tmp_path):
        svc = _make_service(tmp_path, state={"latest_version": "0.4.0"})
        assert svc._cached_result() is None

    def test_returns_none_when_older(self, tmp_path):
        svc = _make_service(tmp_path, state={"latest_version": "0.1.0"})
        assert svc._cached_result() is None

    def test_returns_none_on_invalid_cached_version(self, tmp_path):
        svc = _make_service(tmp_path, state={"latest_version": "not-valid"})
        assert svc._cached_result() is None


# ── auto_check ─────────────────────────────────────────────────────────


class TestAutoCheck:
    def test_returns_message_from_cache(self, tmp_path):
        recent = datetime.now(UTC) - timedelta(hours=1)
        svc = _make_service(
            tmp_path,
            state={
                "last_check_ts": recent.isoformat(),
                "latest_version": "99.0.0",
                "latest_tag": "v99.0.0",
                "release_notes_url": "https://example.com",
            },
        )
        with patch.object(svc, "_detect_install_method", return_value="pip"):
            msg = svc.auto_check()

        assert msg is not None
        assert "99.0.0" in msg
        assert "pfs update install" in msg

    def test_returns_none_from_cache_when_current(self, tmp_path):
        recent = datetime.now(UTC) - timedelta(hours=1)
        svc = _make_service(
            tmp_path,
            state={"last_check_ts": recent.isoformat(), "latest_version": "0.4.0"},
        )
        assert svc.auto_check() is None

    @responses.activate
    def test_returns_message_after_fresh_check(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE, status=200)

        svc = _make_service(tmp_path)
        with patch.object(svc, "_detect_install_method", return_value="pip"):
            msg = svc.auto_check()

        assert msg is not None
        assert "99.0.0" in msg

    @responses.activate
    def test_returns_none_when_up_to_date(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE_CURRENT, status=200)

        svc = _make_service(tmp_path)
        msg = svc.auto_check()
        assert msg is None

    def test_returns_none_on_exception(self, tmp_path):
        svc = _make_service(tmp_path)
        with patch.object(svc, "check", side_effect=Exception("network down")):
            msg = svc.auto_check()
        assert msg is None


# ── install ────────────────────────────────────────────────────────────


class TestInstall:
    @responses.activate
    def test_already_up_to_date(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE_CURRENT, status=200)

        svc = _make_service(tmp_path)
        msg = svc.install()
        assert "Already up to date" in msg

    @responses.activate
    def test_install_pip(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE, status=200)

        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch.object(svc, "_detect_install_method", return_value="pip"),
            patch("subprocess.run", return_value=mock_result),
        ):
            msg = svc.install()

        assert "pip" in msg
        assert svc._state["previous_version"] == CURRENT_VERSION
        assert svc._state["install_method"] == "pip"

    @responses.activate
    def test_install_pipx(self, tmp_path):
        responses.add(responses.GET, GITHUB_API_URL, json=FAKE_RELEASE, status=200)

        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch.object(svc, "_detect_install_method", return_value="pipx"),
            patch("subprocess.run", return_value=mock_result),
        ):
            msg = svc.install()

        assert "pipx" in msg

    @responses.activate
    def test_install_binary_no_url_raises(self, tmp_path):
        release_no_assets = {**FAKE_RELEASE, "assets": []}
        responses.add(responses.GET, GITHUB_API_URL, json=release_no_assets, status=200)

        svc = _make_service(tmp_path)
        with patch.object(svc, "_detect_install_method", return_value="binary"):
            with pytest.raises(UpdateError, match="No binary download URL"):
                svc.install()


class TestInstallPip:
    def test_success(self, tmp_path):
        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            msg = svc._install_pip()
        assert "pip" in msg

    def test_failure_raises(self, tmp_path):
        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=1, stderr="Could not find package")
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(UpdateError, match="pip upgrade failed"):
                svc._install_pip()


class TestInstallPipx:
    def test_success(self, tmp_path):
        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            msg = svc._install_pipx()
        assert "pipx" in msg

    def test_failure_raises(self, tmp_path):
        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=1, stderr="pipx error")
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(UpdateError, match="pipx upgrade failed"):
                svc._install_pipx()


class TestInstallBinary:
    def _setup_binary_env(self, tmp_path):
        """Create a fake binary environment for testing."""
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs"
        fake_exe.write_bytes(b"old-binary-content")
        return fake_exe

    @responses.activate
    def test_linux_happy_path(self, tmp_path):
        download_url = "https://example.com/pfs"
        binary_content = b"new-binary-content"
        responses.add(responses.GET, download_url, body=binary_content, status=200)
        responses.add(
            responses.GET,
            CHECKSUMS_URL,
            body=_checksums_body(("pfs", binary_content)),
            status=200,
        )

        svc = _make_service(tmp_path)
        svc._state["checksums_url"] = CHECKSUMS_URL
        fake_exe = self._setup_binary_env(tmp_path)

        verify_result = MagicMock(returncode=0)
        with (
            patch("pfsentinel.services.updater.is_windows", return_value=False),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("subprocess.run", return_value=verify_result),
        ):
            mock_sys.executable = str(fake_exe)
            msg = svc._install_binary(download_url, "v99.0.0")

        assert msg == "Updated to v99.0.0"
        assert fake_exe.read_bytes() == binary_content
        # Backup should exist
        backup = svc._backup_dir / "pfs.bak"
        assert backup.exists()
        assert backup.read_bytes() == b"old-binary-content"

    @responses.activate
    def test_windows_rename_dance(self, tmp_path):
        download_url = "https://example.com/pfs.exe"
        binary_content = b"new-binary-content"
        responses.add(responses.GET, download_url, body=binary_content, status=200)
        responses.add(
            responses.GET,
            CHECKSUMS_URL,
            body=_checksums_body(("pfs.exe", binary_content)),
            status=200,
        )

        svc = _make_service(tmp_path)
        svc._state["checksums_url"] = CHECKSUMS_URL
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs.exe"
        fake_exe.write_bytes(b"old-binary-content")

        verify_result = MagicMock(returncode=0)
        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("subprocess.run", return_value=verify_result),
        ):
            mock_sys.executable = str(fake_exe)
            msg = svc._install_binary(download_url, "v99.0.0")

        assert msg == "Updated to v99.0.0"
        assert fake_exe.read_bytes() == binary_content
        # .old should exist (from rename dance)
        old_path = fake_exe.with_suffix(".old")
        assert old_path.exists()

    @responses.activate
    def test_windows_removes_existing_old(self, tmp_path):
        download_url = "https://example.com/pfs.exe"
        binary_content = b"new-binary-content"
        responses.add(responses.GET, download_url, body=binary_content, status=200)
        responses.add(
            responses.GET,
            CHECKSUMS_URL,
            body=_checksums_body(("pfs.exe", binary_content)),
            status=200,
        )

        svc = _make_service(tmp_path)
        svc._state["checksums_url"] = CHECKSUMS_URL
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs.exe"
        fake_exe.write_bytes(b"old-binary")
        # Pre-existing .old file
        old_file = exe_dir / "pfs.old"
        old_file.write_bytes(b"ancient-binary")

        verify_result = MagicMock(returncode=0)
        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("subprocess.run", return_value=verify_result),
        ):
            mock_sys.executable = str(fake_exe)
            svc._install_binary(download_url, "v99.0.0")

        # .old should be the renamed current, not the ancient one
        assert old_file.read_bytes() == b"old-binary"

    @responses.activate
    def test_empty_download_raises(self, tmp_path):
        download_url = "https://example.com/pfs"
        responses.add(responses.GET, download_url, body=b"", status=200)

        svc = _make_service(tmp_path)
        fake_exe = self._setup_binary_env(tmp_path)

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=False),
            patch("pfsentinel.services.updater.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_exe)
            with pytest.raises(UpdateError, match="Downloaded file is empty"):
                svc._install_binary(download_url, "v99.0.0")

    @responses.activate
    def test_verification_failure_reverts(self, tmp_path):
        download_url = "https://example.com/pfs"
        binary_content = b"bad-binary"
        responses.add(responses.GET, download_url, body=binary_content, status=200)
        responses.add(
            responses.GET,
            CHECKSUMS_URL,
            body=_checksums_body(("pfs", binary_content)),
            status=200,
        )

        svc = _make_service(tmp_path)
        svc._state["checksums_url"] = CHECKSUMS_URL
        fake_exe = self._setup_binary_env(tmp_path)

        verify_result = MagicMock(returncode=1)
        with (
            patch("pfsentinel.services.updater.is_windows", return_value=False),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("subprocess.run", return_value=verify_result),
        ):
            mock_sys.executable = str(fake_exe)
            with pytest.raises(UpdateError, match="New binary failed version check"):
                svc._install_binary(download_url, "v99.0.0")

        # Should have reverted to old binary
        assert fake_exe.read_bytes() == b"old-binary-content"

    @responses.activate
    def test_verification_exception_reverts(self, tmp_path):
        download_url = "https://example.com/pfs"
        binary_content = b"bad-binary"
        responses.add(responses.GET, download_url, body=binary_content, status=200)
        responses.add(
            responses.GET,
            CHECKSUMS_URL,
            body=_checksums_body(("pfs", binary_content)),
            status=200,
        )

        svc = _make_service(tmp_path)
        svc._state["checksums_url"] = CHECKSUMS_URL
        fake_exe = self._setup_binary_env(tmp_path)

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=False),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("subprocess.run", side_effect=OSError("cannot execute")),
        ):
            mock_sys.executable = str(fake_exe)
            with pytest.raises(UpdateError, match="Update verification failed, reverted"):
                svc._install_binary(download_url, "v99.0.0")

        assert fake_exe.read_bytes() == b"old-binary-content"

    @responses.activate
    def test_windows_rename_fails_restores(self, tmp_path):
        download_url = "https://example.com/pfs.exe"
        binary_content = b"new-binary"
        responses.add(responses.GET, download_url, body=binary_content, status=200)
        responses.add(
            responses.GET,
            CHECKSUMS_URL,
            body=_checksums_body(("pfs.exe", binary_content)),
            status=200,
        )

        svc = _make_service(tmp_path)
        svc._state["checksums_url"] = CHECKSUMS_URL
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs.exe"
        fake_exe.write_bytes(b"old-binary")

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("os.rename", side_effect=OSError("locked")),
        ):
            mock_sys.executable = str(fake_exe)
            with pytest.raises(UpdateError, match="Failed to replace binary"):
                svc._install_binary(download_url, "v99.0.0")


# ── Revert ─────────────────────────────────────────────────────────────


class TestRevert:
    def test_raises_when_no_previous_version(self, tmp_path):
        svc = _make_service(tmp_path)
        with pytest.raises(UpdateError, match="No previous version info"):
            svc.revert()

    def test_raises_when_no_method(self, tmp_path):
        svc = _make_service(tmp_path, state={"previous_version": "0.3.2"})
        with pytest.raises(UpdateError, match="No previous version info"):
            svc.revert()

    def test_revert_pip(self, tmp_path):
        svc = _make_service(tmp_path, state={"previous_version": "0.3.2", "install_method": "pip"})
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            msg = svc.revert()

        assert "v0.3.2" in msg
        assert "pip" in msg
        # Check the correct pip command was called
        cmd = mock_run.call_args[0][0]
        assert "pfsentinel==0.3.2" in cmd[-1]
        # State should be cleared
        assert "previous_version" not in svc._state
        assert "install_method" not in svc._state

    def test_revert_pipx(self, tmp_path):
        svc = _make_service(tmp_path, state={"previous_version": "0.3.2", "install_method": "pipx"})
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            msg = svc.revert()

        assert "v0.3.2" in msg
        assert "pipx" in msg
        cmd = mock_run.call_args[0][0]
        assert "--force" in cmd
        assert "pfsentinel==0.3.2" in cmd[-1]

    def test_revert_binary_linux(self, tmp_path):
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs"
        fake_exe.write_bytes(b"new-broken-binary")

        backup_dir = tmp_path / "update_backup"
        backup_dir.mkdir()
        backup_path = backup_dir / "pfs.bak"
        backup_path.write_bytes(b"old-good-binary")

        svc = _make_service(
            tmp_path,
            state={
                "previous_version": "0.3.2",
                "install_method": "binary",
                "previous_binary_backup": str(backup_path),
            },
        )

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=False),
            patch("pfsentinel.services.updater.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_exe)
            msg = svc.revert()

        assert "v0.3.2" in msg
        assert fake_exe.read_bytes() == b"old-good-binary"

    def test_revert_binary_windows(self, tmp_path):
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs.exe"
        fake_exe.write_bytes(b"new-broken-binary")

        backup_dir = tmp_path / "update_backup"
        backup_dir.mkdir()
        backup_path = backup_dir / "pfs.exe.bak"
        backup_path.write_bytes(b"old-good-binary")

        svc = _make_service(
            tmp_path,
            state={
                "previous_version": "0.3.2",
                "install_method": "binary",
                "previous_binary_backup": str(backup_path),
            },
        )

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch("pfsentinel.services.updater.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_exe)
            msg = svc.revert()

        assert "v0.3.2" in msg
        assert fake_exe.read_bytes() == b"old-good-binary"

    def test_revert_binary_clears_state(self, tmp_path):
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs"
        fake_exe.write_bytes(b"binary")

        backup_dir = tmp_path / "update_backup"
        backup_dir.mkdir()
        backup_path = backup_dir / "pfs.bak"
        backup_path.write_bytes(b"old-binary")

        svc = _make_service(
            tmp_path,
            state={
                "previous_version": "0.3.2",
                "install_method": "binary",
                "previous_binary_backup": str(backup_path),
            },
        )

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=False),
            patch("pfsentinel.services.updater.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_exe)
            svc.revert()

        assert "previous_version" not in svc._state
        assert "install_method" not in svc._state
        assert "previous_binary_backup" not in svc._state


class TestRevertBinary:
    def test_raises_when_no_backup_path(self, tmp_path):
        svc = _make_service(tmp_path)
        with pytest.raises(UpdateError, match="No binary backup found"):
            svc._revert_binary()

    def test_raises_when_backup_missing(self, tmp_path):
        svc = _make_service(
            tmp_path, state={"previous_binary_backup": str(tmp_path / "nonexistent.bak")}
        )
        with pytest.raises(UpdateError, match="No binary backup found"):
            svc._revert_binary()

    def test_windows_removes_existing_old(self, tmp_path):
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs.exe"
        fake_exe.write_bytes(b"new-binary")
        old_file = exe_dir / "pfs.old"
        old_file.write_bytes(b"ancient")

        backup_dir = tmp_path / "update_backup"
        backup_dir.mkdir()
        backup_path = backup_dir / "pfs.exe.bak"
        backup_path.write_bytes(b"good-binary")

        svc = _make_service(
            tmp_path,
            state={
                "previous_version": "0.3.2",
                "previous_binary_backup": str(backup_path),
            },
        )

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch("pfsentinel.services.updater.sys") as mock_sys,
        ):
            mock_sys.executable = str(fake_exe)
            svc._revert_binary()

        assert fake_exe.read_bytes() == b"good-binary"

    def test_windows_rename_fails_restores(self, tmp_path):
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs.exe"
        fake_exe.write_bytes(b"current")

        backup_dir = tmp_path / "update_backup"
        backup_dir.mkdir()
        backup_path = backup_dir / "pfs.exe.bak"
        backup_path.write_bytes(b"old-good")

        svc = _make_service(tmp_path, state={"previous_binary_backup": str(backup_path)})

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("os.rename", side_effect=OSError("locked")),
        ):
            mock_sys.executable = str(fake_exe)
            with pytest.raises(UpdateError, match="Failed to revert binary"):
                svc._revert_binary()


class TestRevertPip:
    def test_success(self, tmp_path):
        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            msg = svc._revert_pip("0.3.2")
        assert "v0.3.2" in msg
        assert "pip" in msg

    def test_failure_raises(self, tmp_path):
        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=1, stderr="not found")
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(UpdateError, match="pip revert failed"):
                svc._revert_pip("0.3.2")


class TestRevertPipx:
    def test_success(self, tmp_path):
        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            msg = svc._revert_pipx("0.3.2")
        assert "v0.3.2" in msg
        assert "pipx" in msg

    def test_failure_raises(self, tmp_path):
        svc = _make_service(tmp_path)
        mock_result = MagicMock(returncode=1, stderr="pipx error")
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(UpdateError, match="pipx revert failed"):
                svc._revert_pipx("0.3.2")


# ── Cleanup ────────────────────────────────────────────────────────────


class TestCleanupOldBinary:
    def test_noop_on_linux(self, tmp_path):
        with patch("pfsentinel.services.updater.is_windows", return_value=False):
            svc = _make_service(tmp_path)
        # Should complete without error

    def test_noop_when_not_frozen(self, tmp_path, monkeypatch):
        monkeypatch.delattr("sys.frozen", raising=False)
        with patch("pfsentinel.services.updater.is_windows", return_value=True):
            svc = _make_service(tmp_path)
        # Should complete without error

    def test_removes_old_file_on_windows_frozen(self, tmp_path):
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs.exe"
        fake_exe.write_bytes(b"current")
        old_file = exe_dir / "pfs.old"
        old_file.write_bytes(b"leftover")

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("pfsentinel.services.updater.app_config_dir", return_value=tmp_path),
        ):
            mock_sys.frozen = True
            mock_sys.executable = str(fake_exe)
            svc = UpdateService()

        assert not old_file.exists()

    def test_no_old_file_no_error(self, tmp_path):
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs.exe"
        fake_exe.write_bytes(b"current")

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("pfsentinel.services.updater.app_config_dir", return_value=tmp_path),
        ):
            mock_sys.frozen = True
            mock_sys.executable = str(fake_exe)
            svc = UpdateService()
        # Should complete without error

    def test_suppresses_os_error_on_unlink(self, tmp_path):
        exe_dir = tmp_path / "bin"
        exe_dir.mkdir()
        fake_exe = exe_dir / "pfs.exe"
        fake_exe.write_bytes(b"current")

        with (
            patch("pfsentinel.services.updater.is_windows", return_value=True),
            patch("pfsentinel.services.updater.sys") as mock_sys,
            patch("pfsentinel.services.updater.app_config_dir", return_value=tmp_path),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.unlink", side_effect=OSError("in use")),
        ):
            mock_sys.frozen = True
            mock_sys.executable = str(fake_exe)
            # Should not raise
            svc = UpdateService()


# ── CLI commands ───────────────────────────────────────────────────────


class TestCLICheck:
    def test_update_available(self, tmp_path):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.check.return_value = {
            "current": "0.4.0",
            "latest": "99.0.0",
            "tag": "v99.0.0",
            "release_url": "https://example.com",
            "install_method": "pip",
        }

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["check"])

        assert result.exit_code == 0
        assert "99.0.0" in result.output

    def test_up_to_date(self, tmp_path):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.check.return_value = None

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["check"])

        assert result.exit_code == 0
        assert "up to date" in result.output

    def test_check_fails(self):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.check.side_effect = Exception("network error")

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["check"])

        assert result.exit_code == 1
        assert "failed" in result.output.lower()


class TestCLIInstall:
    def test_install_success_with_yes(self):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.check.return_value = {
            "current": "0.4.0",
            "latest": "99.0.0",
            "tag": "v99.0.0",
            "release_url": "https://example.com",
            "install_method": "pip",
        }
        mock_svc.install.return_value = "Updated via pip to latest version"

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["install", "--yes"])

        assert result.exit_code == 0
        assert "Updated" in result.output

    def test_install_already_up_to_date(self):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.check.return_value = None

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["install", "--yes"])

        assert result.exit_code == 0
        assert "up to date" in result.output.lower()

    def test_install_user_cancels(self):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.check.return_value = {
            "current": "0.4.0",
            "latest": "99.0.0",
            "tag": "v99.0.0",
            "release_url": "https://example.com",
            "install_method": "pip",
        }

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["install"], input="n\n")

        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()

    def test_install_fails(self):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.check.return_value = {
            "current": "0.4.0",
            "latest": "99.0.0",
            "tag": "v99.0.0",
            "release_url": "https://example.com",
            "install_method": "pip",
        }
        mock_svc.install.side_effect = UpdateError("pip upgrade failed")

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["install", "--yes"])

        assert result.exit_code == 1
        assert "pip upgrade failed" in result.output

    def test_check_fails_on_install(self):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.check.side_effect = Exception("network error")

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["install", "--yes"])

        assert result.exit_code == 1


class TestCLIRevert:
    def test_revert_success_with_yes(self):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.revert.return_value = "Reverted to v0.3.2"

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["revert", "--yes"])

        assert result.exit_code == 0
        assert "Reverted" in result.output

    def test_revert_user_cancels(self):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["revert"], input="n\n")

        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()
        mock_svc.revert.assert_not_called()

    def test_revert_fails(self):
        from typer.testing import CliRunner

        from pfsentinel.cli.commands.update import app

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.revert.side_effect = UpdateError("No previous version info")

        with patch("pfsentinel.cli.commands.update.UpdateService", return_value=mock_svc):
            result = runner.invoke(app, ["revert", "--yes"])

        assert result.exit_code == 1
        assert "No previous version info" in result.output
