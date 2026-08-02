"""Secure credential storage via system keyring."""

from __future__ import annotations

from pfsentinel.services.secret_store import (
    EncryptedFileStore,
    SecretStoreError,
    default_store_dir,
)
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

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


def _system_keyring_usable() -> bool:
    """True if the OS provides a real keystore we can persist to.

    Windows Credential Manager and SecretService (gnome-keyring / kwallet)
    both qualify. On WSL, headless Linux and containers there is no backend,
    so keyring installs a "fail" backend that raises on every call.
    """
    if not _KEYRING_AVAILABLE:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    module = type(backend).__module__.lower()
    name = type(backend).__name__.lower()
    return "fail" not in module and "fail" not in name


class CredentialService:
    """Secure password storage, preferring the OS keystore.

    Backend selection, in order:

    1. **System keyring** - Windows Credential Manager, or SecretService
       (gnome-keyring / kwallet) on a Linux desktop.
    2. **Encrypted file store** - AES-256-GCM under ``~/.pfsentinel/store``,
       used on WSL, headless Linux and containers where no OS keystore
       exists. Unlike a passphrase-protected keyring it never prompts, so
       scheduled backups keep working unattended. See
       ``services/secret_store.py`` for its threat model.
    3. **In-memory** - last resort; credentials are lost on process exit.
    """

    def __init__(self) -> None:
        self._memory: dict[str, str] = {}
        self._use_keyring = _system_keyring_usable()
        self._file_store: EncryptedFileStore | None = None

        if not self._use_keyring:
            store = EncryptedFileStore()
            if store.is_available:
                self._file_store = store
                logger.debug(
                    "No OS keystore available; using the encrypted file store "
                    f"at {default_store_dir()}"
                )
            else:
                logger.warning(
                    "No OS keystore and the encrypted file store is not writable. "
                    "Credentials will be kept in memory only and lost on exit."
                )

    def _backend_store(self, key: str, value: str) -> bool:
        """Persist to whichever backend is active. False means in-memory only."""
        if self._use_keyring:
            try:
                keyring.set_password(_SERVICE, key, value)
                return True
            except _NoKeyringError:
                # Backend disappeared underneath us - stop trying it.
                self._use_keyring = False
            except Exception as e:
                logger.warning(f"Keyring write failed: {e}")
                return False

        if self._file_store is not None:
            try:
                self._file_store.set_password(_SERVICE, key, value)
                return True
            except SecretStoreError as e:
                logger.warning(f"Encrypted store write failed: {e}")

        return False

    def _backend_get(self, key: str) -> str | None:
        if self._use_keyring:
            try:
                return keyring.get_password(_SERVICE, key)
            except _NoKeyringError:
                self._use_keyring = False
            except Exception as e:
                logger.warning(f"Keyring read failed: {e}")
                return None

        if self._file_store is not None:
            try:
                return self._file_store.get_password(_SERVICE, key)
            except SecretStoreError as e:
                logger.warning(f"Encrypted store read failed: {e}")

        return None

    @property
    def is_persistent(self) -> bool:
        """True if credentials survive process exit."""
        return self._use_keyring or self._file_store is not None

    def store(self, device_id: str, password: str) -> None:
        """Store device password securely."""
        if self._backend_store(device_id, password):
            return
        self._memory[device_id] = password

    def get(self, device_id: str) -> str | None:
        """Retrieve device password."""
        result = self._backend_get(device_id)
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
        if self._file_store is not None:
            try:
                self._file_store.delete_password(_SERVICE, device_id)
            except SecretStoreError as e:
                logger.warning(f"Encrypted store delete failed: {e}")
        self._memory.pop(device_id, None)

    def store_telegram_token(self, token: str) -> None:
        if self._backend_store(_TELEGRAM_KEY, token):
            return
        self._memory[_TELEGRAM_KEY] = token

    def get_telegram_token(self) -> str | None:
        result = self._backend_get(_TELEGRAM_KEY)
        if result is not None:
            return result
        return self._memory.get(_TELEGRAM_KEY)

    def store_slack_webhook(self, url: str) -> None:
        if self._backend_store(_SLACK_KEY, url):
            return
        self._memory[_SLACK_KEY] = url

    def get_slack_webhook(self) -> str | None:
        result = self._backend_get(_SLACK_KEY)
        if result is not None:
            return result
        return self._memory.get(_SLACK_KEY)

    def store_ssh_key_passphrase(self, device_id: str, passphrase: str) -> None:
        key = f"{device_id}__keypass"
        if self._backend_store(key, passphrase):
            return
        self._memory[key] = passphrase

    def get_ssh_key_passphrase(self, device_id: str) -> str | None:
        key = f"{device_id}__keypass"
        result = self._backend_get(key)
        if result is not None:
            return result
        return self._memory.get(key)

    def has_password(self, device_id: str) -> bool:
        return self.get(device_id) is not None

    def backend_name(self) -> str:
        """Return a human-readable name of the active credential backend."""
        if self._use_keyring:
            try:
                return type(keyring.get_keyring()).__name__
            except Exception:
                return "unknown keyring"
        if self._file_store is not None:
            return f"encrypted file store ({default_store_dir()})"
        return "in-memory (not persistent)"
