"""Encrypted file-backed secret store for headless systems.

Used only when no OS keystore is available (WSL, headless Linux, containers).
On Windows this never runs - Credential Manager is used. On Linux with a
desktop session, SecretService (gnome-keyring / kwallet) is used.

## Threat model - read this before trusting it

Secrets are sealed with AES-256-GCM. The key is 32 random bytes generated on
first use and written next to the vault with 0600 permissions.

This means the key is readable by anyone who can read the user's home
directory - which is, by definition, the user themselves and root. So this
store protects against:

  - secrets appearing in plain text in backup archives, log bundles,
    screenshares, or an accidental `git add`
  - casual grep / `cat` disclosure
  - another user on the same box (0600 + 0700 directory)

It does NOT protect against an attacker who already executes code as this
user. That is not solvable while unattended (scheduled) backups must decrypt
without a human present: any key the process can reach unaided, an attacker
running as that process can reach too.

If you need protection against a local attacker, install a real OS keystore
(gnome-keyring, kwallet) - pfSentinel will prefer it automatically.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)

# AES-GCM standard nonce length. Never reuse a nonce with the same key -
# a fresh one is drawn for every write.
_NONCE_BYTES = 12
_KEY_BYTES = 32  # AES-256

_VAULT_NAME = "secrets.json"
_KEY_NAME = "secrets.key"


class SecretStoreError(Exception):
    """Raised when the encrypted store cannot be read or written."""


def default_store_dir() -> Path:
    """Directory holding the vault and its key."""
    return Path.home() / ".pfsentinel" / "store"


class EncryptedFileStore:
    """AES-256-GCM secret store backed by two files in one 0700 directory.

    Layout:
        <dir>/secrets.key   32 random bytes, mode 0600
        <dir>/secrets.json  {"nonce": <b64>, "ct": <b64>}, mode 0600

    The whole secret mapping is sealed as a single ciphertext, so the file
    never leaks which keys exist or how many.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or default_store_dir()
        self._vault = self._dir / _VAULT_NAME
        self._key_file = self._dir / _KEY_NAME

    # --- filesystem helpers ------------------------------------------------

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self._dir, stat.S_IRWXU)  # 0700

    @staticmethod
    def _harden(path: Path) -> None:
        if sys.platform != "win32":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600

    def _write_private(self, path: Path, data: bytes) -> None:
        """Write with owner-only permissions set before any content lands."""
        self._ensure_dir()
        tmp = path.with_suffix(path.suffix + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        self._harden(tmp)
        os.replace(tmp, path)  # atomic; never leaves a partial vault

    # --- key management ----------------------------------------------------

    def _load_or_create_key(self) -> bytes:
        if self._key_file.exists():
            key = self._key_file.read_bytes()
            if len(key) != _KEY_BYTES:
                raise SecretStoreError(
                    f"Key file {self._key_file} is {len(key)} bytes, expected {_KEY_BYTES}. "
                    "Delete it and re-enter credentials to regenerate."
                )
            self._harden(self._key_file)
            return key

        key = AESGCM.generate_key(bit_length=256)
        self._write_private(self._key_file, key)
        logger.info(
            f"Generated a new credential encryption key at {self._key_file} (owner-only). "
            "Losing this file makes stored credentials unrecoverable."
        )
        return key

    # --- vault read / write ------------------------------------------------

    def _read_all(self) -> dict[str, str]:
        if not self._vault.exists():
            return {}
        try:
            envelope = json.loads(self._vault.read_text(encoding="utf-8"))
            nonce = bytes.fromhex(envelope["nonce"])
            ciphertext = bytes.fromhex(envelope["ct"])
        except (OSError, ValueError, KeyError) as e:
            raise SecretStoreError(f"Credential store at {self._vault} is corrupt: {e}") from e

        try:
            plaintext = AESGCM(self._load_or_create_key()).decrypt(nonce, ciphertext, None)
        except InvalidTag as e:
            raise SecretStoreError(
                f"Credential store at {self._vault} failed authentication. It was either "
                "tampered with, or encrypted with a different key. Delete both files in "
                f"{self._dir} and re-enter credentials to reset."
            ) from e

        data = json.loads(plaintext.decode("utf-8"))
        return {str(k): str(v) for k, v in data.items()}

    def _write_all(self, secrets: dict[str, str]) -> None:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._load_or_create_key()).encrypt(
            nonce, json.dumps(secrets).encode("utf-8"), None
        )
        envelope = json.dumps({"nonce": nonce.hex(), "ct": ciphertext.hex()})
        self._write_private(self._vault, envelope.encode("utf-8"))

    # --- public API (mirrors the subset of keyring pfSentinel uses) --------

    def set_password(self, service: str, key: str, value: str) -> None:
        secrets = self._read_all()
        secrets[f"{service}:{key}"] = value
        self._write_all(secrets)

    def get_password(self, service: str, key: str) -> str | None:
        return self._read_all().get(f"{service}:{key}")

    def delete_password(self, service: str, key: str) -> None:
        secrets = self._read_all()
        if secrets.pop(f"{service}:{key}", None) is not None:
            self._write_all(secrets)

    @property
    def is_available(self) -> bool:
        """True if the store directory is writable."""
        try:
            self._ensure_dir()
            return os.access(self._dir, os.W_OK)
        except OSError:
            return False
