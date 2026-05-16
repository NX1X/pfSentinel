"""Self-update service: check GitHub for new releases, install, and revert."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
from loguru import logger
from packaging.version import Version

from pfsentinel import __version__
from pfsentinel.utils.platform import app_config_dir, is_windows


class UpdateError(Exception):
    """Raised when an update operation fails."""


class UpdateService:
    """Check for updates, install new versions, and revert to previous."""

    GITHUB_API_URL = "https://api.github.com/repos/NX1X/pfSentinel/releases/latest"
    CHECK_INTERVAL = timedelta(hours=24)
    REQUEST_TIMEOUT = 5
    DOWNLOAD_TIMEOUT = 120

    def __init__(self) -> None:
        self._config_dir = app_config_dir()
        self._state_path = self._config_dir / "update_check.json"
        self._backup_dir = self._config_dir / "update_backup"
        self._state: dict = self._load_state()
        self._cleanup_old_binary()

    # ── State persistence ──────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self._state, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self._config_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ── Detection ──────────────────────────────────────────────────────

    def _detect_install_method(self) -> str:
        """Detect how pfSentinel was installed: binary, pipx, or pip."""
        if getattr(sys, "frozen", False):
            return "binary"

        prefix = str(Path(sys.prefix).resolve()).lower()
        if "pipx" in prefix:
            return "pipx"

        if shutil.which("pipx"):
            try:
                result = subprocess.run(
                    ["pipx", "list", "--short"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if "pfsentinel" in result.stdout.lower():
                    return "pipx"
            except Exception:
                pass

        return "pip"

    def current_version(self) -> Version:
        return Version(__version__)

    # ── Check ──────────────────────────────────────────────────────────

    def should_auto_check(self) -> bool:
        ts = self._state.get("last_check_ts")
        if not ts:
            return True
        try:
            last = datetime.fromisoformat(ts)
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            return datetime.now(UTC) - last > self.CHECK_INTERVAL
        except (ValueError, TypeError):
            return True

    def check(self, force: bool = False) -> dict | None:
        """Check GitHub for the latest release.

        Returns info dict if a newer version is available, None if up-to-date.
        If force=False, uses cached result when last check was <24h ago.
        """
        if not force and not self.should_auto_check():
            return self._cached_result()

        resp = requests.get(
            self.GITHUB_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "pfSentinel-updater",
            },
            timeout=self.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        tag = data.get("tag_name", "")
        version_str = tag.lstrip("v")
        html_url = data.get("html_url", "")

        # Find binary asset and checksums for this platform
        asset_name = "pfs.exe" if is_windows() else "pfs"
        download_url = ""
        checksums_url = ""
        for asset in data.get("assets", []):
            if asset.get("name") == asset_name:
                download_url = asset.get("browser_download_url", "")
            elif asset.get("name") == "checksums-sha256.txt":
                checksums_url = asset.get("browser_download_url", "")

        # Update cached state
        self._state["last_check_ts"] = datetime.now(UTC).isoformat()
        self._state["latest_version"] = version_str
        self._state["latest_tag"] = tag
        self._state["download_url"] = download_url
        self._state["checksums_url"] = checksums_url
        self._state["release_notes_url"] = html_url
        self._state["current_version_at_check"] = __version__
        self._save_state()

        try:
            latest = Version(version_str)
        except Exception:
            return None

        if latest > self.current_version():
            return {
                "current": __version__,
                "latest": version_str,
                "tag": tag,
                "release_url": html_url,
                "install_method": self._detect_install_method(),
            }
        return None

    def _cached_result(self) -> dict | None:
        """Return update info from cached state if newer version exists."""
        latest_str = self._state.get("latest_version")
        if not latest_str:
            return None
        try:
            if Version(latest_str) > self.current_version():
                return {
                    "current": __version__,
                    "latest": latest_str,
                    "tag": self._state.get("latest_tag", ""),
                    "release_url": self._state.get("release_notes_url", ""),
                    "install_method": self._detect_install_method(),
                }
        except Exception:
            pass
        return None

    def auto_check(self) -> str | None:
        """Non-blocking auto-check for the main callback.

        Returns a one-line notification string or None.
        Catches all exceptions internally — never crashes.
        """
        try:
            if not self.should_auto_check():
                cached = self._cached_result()
                if cached:
                    return (
                        f"Update available: v{cached['latest']} "
                        f"(current: v{cached['current']}). "
                        f"Run: pfs update install"
                    )
                return None

            result = self.check(force=True)
            if result:
                return (
                    f"Update available: v{result['latest']} "
                    f"(current: v{result['current']}). "
                    f"Run: pfs update install"
                )
            return None
        except Exception:
            logger.debug("Auto update check failed silently")
            return None

    # ── Install ────────────────────────────────────────────────────────

    def install(self) -> str:
        """Install the latest version. Returns a status message.

        Raises UpdateError on failure.
        """
        result = self.check(force=True)
        if result is None:
            return f"Already up to date (v{__version__})"

        method = self._detect_install_method()
        self._state["previous_version"] = __version__
        self._state["install_method"] = method
        self._save_state()

        if method == "binary":
            download_url = self._state.get("download_url", "")
            if not download_url:
                raise UpdateError(
                    "No binary download URL found for this platform. "
                    "Try installing via pip: pip install --upgrade pfsentinel"
                )
            return self._install_binary(download_url, result["tag"])
        elif method == "pipx":
            return self._install_pipx()
        else:
            return self._install_pip()

    def _install_binary(self, download_url: str, tag: str) -> str:
        current_exe = Path(sys.executable).resolve()
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        suffix = ".exe" if is_windows() else ""
        temp_path = self._backup_dir / f"pfs_new{suffix}"

        # Download new binary
        resp = requests.get(download_url, timeout=self.DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()

        with open(temp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

        if temp_path.stat().st_size == 0:
            temp_path.unlink()
            raise UpdateError("Downloaded file is empty")

        # Verify SHA-256 checksum against release checksums
        self._verify_checksum(temp_path, asset_name="pfs.exe" if is_windows() else "pfs")

        # Back up current binary
        backup_path = self._backup_dir / f"{current_exe.name}.bak"
        shutil.copy2(current_exe, backup_path)
        self._state["previous_binary_backup"] = str(backup_path)
        self._save_state()

        # Replace current binary
        if is_windows():
            old_path = current_exe.with_suffix(".old")
            try:
                if old_path.exists():
                    old_path.unlink()
                os.rename(current_exe, old_path)
                shutil.copy2(temp_path, current_exe)
            except OSError as e:
                if old_path.exists() and not current_exe.exists():
                    os.rename(old_path, current_exe)
                raise UpdateError(f"Failed to replace binary: {e}") from e
        else:
            # Suppression note (B103): standard exec perms (rwxr-xr-x, not world-writable)
            # on a binary whose SHA-256 was verified by _verify_checksum() above.
            os.chmod(temp_path, 0o755)  # nosec B103
            os.replace(temp_path, current_exe)

        # Verify new binary
        try:
            verify = subprocess.run(
                [str(current_exe), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if verify.returncode != 0:
                raise UpdateError("New binary failed version check")
        except UpdateError:
            shutil.copy2(backup_path, current_exe)
            raise
        except Exception as e:
            logger.warning(f"New binary verification failed: {e}")
            shutil.copy2(backup_path, current_exe)
            raise UpdateError(f"Update verification failed, reverted: {e}") from e

        # Cleanup temp download
        with contextlib.suppress(OSError):
            temp_path.unlink()

        return f"Updated to {tag}"

    def _install_pip(self) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "pfsentinel"],
            capture_output=True,
            text=True,
            timeout=self.DOWNLOAD_TIMEOUT,
        )
        if result.returncode != 0:
            raise UpdateError(f"pip upgrade failed:\n{result.stderr}")
        return "Updated via pip to latest version"

    def _install_pipx(self) -> str:
        result = subprocess.run(
            ["pipx", "upgrade", "pfsentinel"],
            capture_output=True,
            text=True,
            timeout=self.DOWNLOAD_TIMEOUT,
        )
        if result.returncode != 0:
            raise UpdateError(f"pipx upgrade failed:\n{result.stderr}")
        return "Updated via pipx to latest version"

    # ── Checksum verification ────────────────────────────────────────

    def _verify_checksum(self, file_path: Path, asset_name: str) -> None:
        """Verify downloaded binary against checksums-sha256.txt from the release.

        Raises UpdateError if checksum doesn't match or can't be verified.
        """
        checksums_url = self._state.get("checksums_url", "")
        if not checksums_url:
            raise UpdateError(
                "No checksums file found in release assets. "
                "Cannot verify download integrity — aborting for safety."
            )

        try:
            resp = requests.get(checksums_url, timeout=self.REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            raise UpdateError(f"Failed to download checksums file: {e}") from e

        # Parse checksums file (format: "hash  filename" per line)
        expected_hash: str | None = None
        for line in resp.text.strip().splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip() == asset_name:
                expected_hash = parts[0].strip().lower()
                break

        if not expected_hash:
            raise UpdateError(f"Checksum for '{asset_name}' not found in release checksums file")

        # Calculate actual hash
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
        actual_hash = sha256.hexdigest().lower()

        if actual_hash != expected_hash:
            file_path.unlink()
            raise UpdateError(
                f"Checksum mismatch for downloaded binary.\n"
                f"  Expected: {expected_hash}\n"
                f"  Got:      {actual_hash}\n"
                "Download may be corrupted or tampered with — file deleted."
            )

        logger.info(f"Binary checksum verified: {actual_hash[:16]}...")

    # ── Revert ─────────────────────────────────────────────────────────

    def revert(self) -> str:
        """Revert to the previous version. Returns a status message.

        Raises UpdateError if no previous version info is available.
        """
        prev = self._state.get("previous_version")
        method = self._state.get("install_method")

        if not prev or not method:
            raise UpdateError(
                "No previous version info found. "
                "Revert is only available after a successful 'pfs update install'."
            )

        if method == "binary":
            msg = self._revert_binary()
        elif method == "pipx":
            msg = self._revert_pipx(prev)
        else:
            msg = self._revert_pip(prev)

        # Clear revert state after successful revert
        self._state.pop("previous_version", None)
        self._state.pop("install_method", None)
        self._state.pop("previous_binary_backup", None)
        self._save_state()

        return msg

    def _revert_binary(self) -> str:
        backup = self._state.get("previous_binary_backup")
        if not backup or not Path(backup).exists():
            raise UpdateError("No binary backup found to revert to")

        current_exe = Path(sys.executable).resolve()
        backup_path = Path(backup)

        if is_windows():
            old_path = current_exe.with_suffix(".old")
            try:
                if old_path.exists():
                    old_path.unlink()
                os.rename(current_exe, old_path)
                shutil.copy2(backup_path, current_exe)
            except OSError as e:
                if old_path.exists() and not current_exe.exists():
                    os.rename(old_path, current_exe)
                raise UpdateError(f"Failed to revert binary: {e}") from e
        else:
            os.replace(backup_path, current_exe)

        prev = self._state.get("previous_version", "unknown")
        return f"Reverted to v{prev}"

    def _revert_pip(self, version: str) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", f"pfsentinel=={version}"],
            capture_output=True,
            text=True,
            timeout=self.DOWNLOAD_TIMEOUT,
        )
        if result.returncode != 0:
            raise UpdateError(f"pip revert failed:\n{result.stderr}")
        return f"Reverted to v{version} via pip"

    def _revert_pipx(self, version: str) -> str:
        result = subprocess.run(
            ["pipx", "install", "--force", f"pfsentinel=={version}"],
            capture_output=True,
            text=True,
            timeout=self.DOWNLOAD_TIMEOUT,
        )
        if result.returncode != 0:
            raise UpdateError(f"pipx revert failed:\n{result.stderr}")
        return f"Reverted to v{version} via pipx"

    # ── Cleanup ────────────────────────────────────────────────────────

    def _cleanup_old_binary(self) -> None:
        """Remove leftover .old binary from a previous Windows update."""
        if not is_windows() or not getattr(sys, "frozen", False):
            return
        old = Path(sys.executable).with_suffix(".old")
        if old.exists():
            with contextlib.suppress(OSError):
                old.unlink()
