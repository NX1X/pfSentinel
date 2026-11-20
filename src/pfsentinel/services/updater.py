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
from urllib.parse import urlparse

import requests
from packaging.version import InvalidVersion, Version

from pfsentinel import __version__
from pfsentinel.utils.logging import get_logger
from pfsentinel.utils.platform import app_config_dir, is_windows

logger = get_logger(__name__)


# Library search variables a PyInstaller build rewrites to point at its bundle.
_LOADER_VARS = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "LIBPATH",
)


def child_env() -> dict[str, str]:
    """Environment for spawning another program from a frozen (PyInstaller) build.

    A one-file binary unpacks itself into a temp directory and records that in
    ``_MEIPASS2`` / ``_PYI_*`` environment variables. Those are inherited by any
    child process, so a second one-file binary started from here believes it is
    already unpacked and fails to start. PyInstaller also rewrites loader
    variables such as ``LD_LIBRARY_PATH`` and saves the caller's value in
    ``<NAME>_ORIG``; the original has to be put back for a non-bundled child.

    This is why self-update could verify the download and still report that the
    new binary would not start, while the same file ran fine from a shell.
    """
    env = dict(os.environ)
    # PyInstaller's own markers: these names mean nothing to anything else.
    for key in list(env):
        if key in {"_MEIPASS", "_MEIPASS2"} or key.startswith("_PYI_"):
            del env[key]

    # Loader variables are only rewritten by a frozen build, and only these
    # names. A user's own FOO_ORIG is left alone.
    if getattr(sys, "frozen", False) is not True:
        return env
    bundle = getattr(sys, "_MEIPASS", "")
    if not isinstance(bundle, str):
        bundle = ""
    for var in _LOADER_VARS:
        original = env.pop(f"{var}_ORIG", None)
        if original is not None:
            if original:
                env[var] = original
            else:
                env.pop(var, None)
        elif bundle and bundle in env.get(var, ""):
            # No saved original: the caller had none, so drop the bundle path.
            env.pop(var, None)
    return env


def _newest_release(releases: list[dict]) -> dict:
    """The highest-versioned non-draft release, pre-releases included."""
    best: tuple[Version, dict] | None = None
    for release in releases:
        if release.get("draft"):
            continue
        try:
            version = Version(str(release.get("tag_name", "")).lstrip("v"))
        except InvalidVersion:
            continue
        if best is None or version > best[0]:
            best = (version, release)
    return best[1] if best else {}


class UpdateError(Exception):
    """Raised when an update operation fails."""


class UpdateService:
    """Check for updates, install new versions, and revert to previous."""

    GITHUB_API_URL = "https://api.github.com/repos/NX1X/pfSentinel/releases/latest"
    GITHUB_RELEASES_URL = "https://api.github.com/repos/NX1X/pfSentinel/releases?per_page=20"
    # Downloads are accepted only over https from these hosts (GitHub release
    # assets redirect to the last two). Checked again after every redirect.
    ALLOWED_HOSTS = frozenset(
        {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
    )
    # The binary is about 40 MB; anything far larger is not a pfSentinel release.
    MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
    CHECK_INTERVAL = timedelta(hours=24)
    # Generous: a one-file binary unpacks and may be antivirus-scanned on
    # first run, which is slow on Windows.
    VERIFY_TIMEOUT = 120

    REQUEST_TIMEOUT = 5
    DOWNLOAD_TIMEOUT = 120

    def __init__(self) -> None:
        self._config_dir = app_config_dir()
        self._state_path = self._config_dir / "update_check.json"
        self._backup_dir = self._config_dir / "update_backup"
        self._state: dict = self._load_state()
        self._cleanup_old_binary()

    # ── State persistence ──────────────────────────────────────────────

    @property
    def state(self) -> dict:
        return self._state

    def save_state(self) -> None:
        self._save_state()

    def _load_state(self) -> dict:
        try:
            state: dict = json.loads(self._state_path.read_text(encoding="utf-8"))
            return state
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
                    env=child_env(),
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

    def check(self, force: bool = False, prerelease: bool = False) -> dict | None:
        """Check GitHub for the latest release.

        Returns info dict if a newer version is available, None if up-to-date.
        If force=False, uses cached result when last check was <24h ago.
        prerelease: also consider pre-releases (for example 1.0.0-rc1), which
        GitHub never marks as the latest release.
        """
        if not force and not prerelease and not self.should_auto_check():
            return self._cached_result()

        headers = {"Accept": "application/vnd.github+json", "User-Agent": "pfSentinel-updater"}
        if prerelease:
            resp = requests.get(
                self.GITHUB_RELEASES_URL, headers=headers, timeout=self.REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            data = _newest_release(resp.json())
        else:
            resp = requests.get(self.GITHUB_API_URL, headers=headers, timeout=self.REQUEST_TIMEOUT)
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

    def install(self, prerelease: bool = False) -> str:
        """Install the latest version. Returns a status message.

        Raises UpdateError on failure.
        """
        result = self.check(force=True, prerelease=prerelease)
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
            return self._install_pipx(prerelease)
        else:
            return self._install_pip(prerelease)

    def _install_binary(self, download_url: str, tag: str) -> str:
        current_exe = Path(sys.executable).resolve()
        install_dir = current_exe.parent
        self._refuse_unsafe_root_update(install_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        asset_name = "pfs.exe" if is_windows() else "pfs"

        # Stage next to the binary: same folder, same filesystem, so the final
        # rename is atomic, and owner-only from the first byte.
        fd, temp_name = tempfile.mkstemp(dir=install_dir, prefix=".pfs-update-")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                self._download(download_url, f)
            if temp_path.stat().st_size == 0:
                raise UpdateError("Downloaded file is empty")

            # Integrity (release checksums), then origin (signed build attestation).
            self._verify_checksum(temp_path, asset_name=asset_name)
            self._verify_attestation(temp_path, asset_name)

            # Back up the current binary to a fixed path the updater derives
            # itself; revert never trusts a path from the state file.
            backup_path = self._binary_backup_path(current_exe)
            shutil.copy2(current_exe, backup_path)
            self._state["previous_binary_backup"] = str(backup_path)
            self._save_state()

            if is_windows():
                # A running .exe cannot be overwritten, only renamed.
                old_path = current_exe.with_suffix(".old")
                try:
                    if old_path.exists():
                        old_path.unlink()
                    os.rename(current_exe, old_path)
                    os.replace(temp_path, current_exe)
                except OSError as e:
                    if old_path.exists() and not current_exe.exists():
                        os.rename(old_path, current_exe)
                    raise UpdateError(f"Failed to replace binary: {e}") from e
            else:
                # Owner-only rwx (rwx------); no group or world access.
                os.chmod(temp_path, 0o700)
                os.replace(temp_path, current_exe)
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()

        # Verify the new binary actually starts. A PyInstaller one-file build
        # unpacks itself into %TEMP% on first run and an antivirus scan of a
        # 30 MB executable can take a while, so the timeout is generous.
        try:
            verify = subprocess.run(
                [str(current_exe), "--version"],
                capture_output=True,
                text=True,
                timeout=self.VERIFY_TIMEOUT,
                env=child_env(),
            )
            if verify.returncode != 0:
                detail = (verify.stderr or verify.stdout or "").strip().splitlines()
                first = detail[0][:200] if detail else "no output"
                raise UpdateError(
                    f"The new binary did not start (exit code {verify.returncode}): {first}. "
                    "Antivirus or SmartScreen blocking the new file is the usual cause; "
                    "the previous version has been restored."
                )
        except UpdateError:
            shutil.copy2(backup_path, current_exe)
            raise
        except Exception as e:
            logger.warning(f"New binary verification failed: {e}")
            shutil.copy2(backup_path, current_exe)
            raise UpdateError(f"Update verification failed, reverted: {e}") from e

        return f"Updated to {tag}"

    # ── Download safety ──────────────────────────────────────────────

    def _check_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self.ALLOWED_HOSTS:
            raise UpdateError(f"Refusing to download from {parsed.scheme}://{parsed.hostname}")

    def _download(self, url: str, sink) -> None:  # type: ignore[no-untyped-def]
        """Stream ``url`` into ``sink``: https GitHub hosts only, size-capped."""
        self._check_url(url)
        resp = requests.get(url, timeout=self.DOWNLOAD_TIMEOUT, stream=True)
        try:
            for hop in [*resp.history, resp]:
                self._check_url(hop.url)
            resp.raise_for_status()
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared > self.MAX_DOWNLOAD_BYTES:
                raise UpdateError(f"Download is {declared} bytes, more than the allowed maximum")
            received = 0
            for chunk in resp.iter_content(chunk_size=65536):
                received += len(chunk)
                if received > self.MAX_DOWNLOAD_BYTES:
                    raise UpdateError("Download is larger than the allowed maximum; aborted")
                sink.write(chunk)
        finally:
            resp.close()

    def _verify_attestation(self, file_path: Path, asset_name: str) -> None:
        """Refuse a binary that this repository's release workflow did not build."""
        from pfsentinel.services import attestation

        try:
            attestation.verify_file(file_path, asset_name, timeout=self.REQUEST_TIMEOUT * 3)
        except attestation.AttestationError as e:
            raise UpdateError(
                f"Build attestation check failed: {e}. The download was not installed. "
                "To update by hand, follow 'Standalone binary' in the manual and run "
                "'gh attestation verify'."
            ) from e
        logger.info("Build attestation verified (release workflow on main)")

    @staticmethod
    def _refuse_unsafe_root_update(install_dir: Path) -> None:
        """As root, update only a binary in a root-owned folder others cannot write.

        Otherwise any local user who can write that folder could plant a file
        that root then runs (the version check executes the new binary).
        """
        if is_windows() or not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        st = install_dir.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise UpdateError(
                f"Refusing to update as root: {install_dir} is not a root-owned folder that "
                "only root can write. Install pfs in a folder you own and update as that user."
            )

    def _binary_backup_path(self, current_exe: Path) -> Path:
        return self._backup_dir / f"{current_exe.name}.bak"

    def _install_pip(self, prerelease: bool = False) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade"]
            + (["--pre"] if prerelease else [])
            + ["pfsentinel"],
            capture_output=True,
            text=True,
            timeout=self.DOWNLOAD_TIMEOUT,
            env=child_env(),
        )
        if result.returncode != 0:
            raise UpdateError(f"pip upgrade failed:\n{result.stderr}")
        return "Updated via pip to latest version"

    def _install_pipx(self, prerelease: bool = False) -> str:
        result = subprocess.run(
            ["pipx", "upgrade"] + (["--pip-args=--pre"] if prerelease else []) + ["pfsentinel"],
            capture_output=True,
            text=True,
            timeout=self.DOWNLOAD_TIMEOUT,
            env=child_env(),
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

        import io

        buffer = io.BytesIO()
        try:
            self._download(checksums_url, buffer)
        except UpdateError:
            raise
        except Exception as e:
            raise UpdateError(f"Failed to download checksums file: {e}") from e
        checksums_text = buffer.getvalue().decode("utf-8", "replace")

        # Parse checksums file (format: "hash  filename" per line).
        # Names are normalised because the generator's spelling has varied:
        # `sha256sum ./*` writes "./pfs.exe", binary mode writes "*pfs.exe",
        # and either separator can appear. Compare on the bare filename.
        expected_hash: str | None = None
        for line in checksums_text.strip().splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            name = parts[1].strip().lstrip("*")
            name = name.replace("\\", "/").rsplit("/", 1)[-1]
            if name == asset_name:
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
        current_exe = Path(sys.executable).resolve()
        # Derived, never read from the state file: a changed state file must
        # not be able to make revert copy an arbitrary file over pfs.
        backup_path = self._binary_backup_path(current_exe)
        if not backup_path.is_file():
            raise UpdateError("No binary backup found to revert to")
        self._refuse_unsafe_root_update(current_exe.parent)

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
            env=child_env(),
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
            env=child_env(),
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
