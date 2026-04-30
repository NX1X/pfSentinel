"""Secure credential storage via system keyring."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

try:
    import keyring
    from keyring.errors import NoKeyringError as _NoKeyringError

    _KEYRING_AVAILABLE = True
except ImportError:
    _KEYRING_AVAILABLE = False
    _NoKeyringError = Exception  # type: ignore[assignment,misc]

_SERVICE = "pfsentinel"
_TELEGRAM_KEY = "__telegram_token__"
_SLACK_KEY = "__slack_webhook__"

# Resolved at import time for isinstance checks
try:
    from keyrings.alt.file import PlaintextKeyring as _PlaintextKeyringType
except ImportError:
    _PlaintextKeyringType = type(None)  # type: ignore[assignment,misc]


def _ensure_keyring_backend() -> None:
    """Configure a persistent keyring backend if the system backend is unavailable.

    On WSL / headless Linux, the default backend raises NoKeyringError.
    keyrings.alt provides a file-based fallback that persists across processes.
    """
    if not _KEYRING_AVAILABLE:
        return
    try:
        # Quick probe to detect missing backend
        keyring.get_keyring()
        backend = keyring.get_keyring()
        # If it's the fail backend, switch to keyrings.alt
        if "fail" in type(backend).__module__.lower():
            _switch_to_alt_backend()
    except Exception:
        _switch_to_alt_backend()


def _switch_to_alt_backend() -> None:
    # Prefer EncryptedKeyring (prompts for master password) over PlaintextKeyring
    try:
        from keyrings.alt.file import EncryptedKeyring

        keyring.set_keyring(EncryptedKeyring())
        logger.info(
            "Using EncryptedKeyring fallback — credentials are encrypted "
            "at ~/.local/share/python_keyring/crypted_pass.cfg. "
            "You will be prompted for a master password."
        )
        return
    except ImportError:
        pass

    try:
        from keyrings.alt.file import PlaintextKeyring

        keyring.set_keyring(PlaintextKeyring())
        logger.warning(
            "Using PlaintextKeyring fallback — credentials are stored UNENCRYPTED "
            "at ~/.local/share/python_keyring/keyring_pass.cfg. "
            "Consider installing a system keyring or using EncryptedKeyring."
        )
        # Restrict file permissions on the plaintext keyring file (Unix only)
        _restrict_keyring_file_permissions()
    except ImportError:
        pass  # keyrings.alt not installed - will fall back to in-memory


def _restrict_keyring_file_permissions() -> None:
    """Set owner-only permissions on the plaintext keyring file."""
    if sys.platform == "win32":
        return
    keyring_path = Path.home() / ".local" / "share" / "python_keyring" / "keyring_pass.cfg"
    if keyring_path.exists():
        try:
            os.chmod(keyring_path, 0o600)
        except OSError as e:
            logger.warning(f"Could not restrict keyring file permissions: {e}")


# Configure on module load
_ensure_keyring_backend()


class CredentialService:
    """Thin wrapper around system keyring for secure password storage.

    On Windows: uses Windows Credential Manager.
    On Linux with desktop: uses SecretService (gnome-keyring / kwallet).
    On WSL / headless Linux: uses keyrings.alt file backend
      (~/.local/share/python_keyring/keyring_pass.cfg).
    Falls back to in-memory if nothing else works (lost on process exit).
    """

    def __init__(self) -> None:
        self._memory: dict[str, str] = {}
        self._use_keyring = _KEYRING_AVAILABLE
        self._memory_only_warning_issued = False

    def _keyring_store(self, key: str, value: str) -> bool:
        try:
            keyring.set_password(_SERVICE, key, value)
            # Ensure file permissions are restricted after writes
            # (the file may have just been created)
            if isinstance(keyring.get_keyring(), _PlaintextKeyringType):
                _restrict_keyring_file_permissions()
            return True
        except _NoKeyringError:
            self._use_keyring = False
            return False
        except Exception:
            return False

    def _keyring_get(self, key: str) -> str | None:
        try:
            return keyring.get_password(_SERVICE, key)
        except (_NoKeyringError, Exception):
            self._use_keyring = False
            return None

    @property
    def is_persistent(self) -> bool:
        """True if credentials are stored in a persistent backend (keyring/file).

        False means in-memory only — credentials will be lost when the process exits.
        """
        if not self._use_keyring:
            return False
        try:
            backend = type(keyring.get_keyring()).__name__
            return "fail" not in backend.lower() and "memory" not in backend.lower()
        except Exception:
            return False

    def store(self, device_id: str, password: str) -> None:
        """Store device password securely."""
        if self._use_keyring and self._keyring_store(device_id, password):
            return
        self._memory[device_id] = password

    def get(self, device_id: str) -> str | None:
        """Retrieve device password."""
        if self._use_keyring:
            result = self._keyring_get(device_id)
            if result is not None:
                return result
        return self._memory.get(device_id)

    def delete(self, device_id: str) -> None:
        """Delete stored device password."""
        if self._use_keyring:
            try:
                keyring.delete_password(_SERVICE, device_id)
            except Exception:
                pass
        self._memory.pop(device_id, None)

    def store_telegram_token(self, token: str) -> None:
        if self._use_keyring and self._keyring_store(_TELEGRAM_KEY, token):
            return
        self._memory[_TELEGRAM_KEY] = token

    def get_telegram_token(self) -> str | None:
        if self._use_keyring:
            result = self._keyring_get(_TELEGRAM_KEY)
            if result is not None:
                return result
        return self._memory.get(_TELEGRAM_KEY)

    def store_slack_webhook(self, url: str) -> None:
        if self._use_keyring and self._keyring_store(_SLACK_KEY, url):
            return
        self._memory[_SLACK_KEY] = url

    def get_slack_webhook(self) -> str | None:
        if self._use_keyring:
            result = self._keyring_get(_SLACK_KEY)
            if result is not None:
                return result
        return self._memory.get(_SLACK_KEY)

    def store_ssh_key_passphrase(self, device_id: str, passphrase: str) -> None:
        key = f"{device_id}__keypass"
        if self._use_keyring and self._keyring_store(key, passphrase):
            return
        self._memory[key] = passphrase

    def get_ssh_key_passphrase(self, device_id: str) -> str | None:
        key = f"{device_id}__keypass"
        if self._use_keyring:
            result = self._keyring_get(key)
            if result is not None:
                return result
        return self._memory.get(key)

    def has_password(self, device_id: str) -> bool:
        return self.get(device_id) is not None

    def backend_name(self) -> str:
        """Return a human-readable name of the active keyring backend."""
        if not _KEYRING_AVAILABLE:
            return "in-memory (keyring not installed)"
        try:
            return type(keyring.get_keyring()).__name__
        except Exception:
            return "unknown"
