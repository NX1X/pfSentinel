"""Encryption at rest for backup files.

A pfSense config.xml holds password hashes, CA private keys, TOTP secrets
and VPN keys. With encryption on, every backup file is written encrypted as
``<name>.enc``; the plain bytes never touch the disk.

## Modes

- ``keystore``: a random 256-bit key, kept in the OS keystore (Windows
  Credential Manager). Scheduled runs need no input. A recovery key is shown
  once at setup; with it, any machine can decrypt the backups.
- ``passphrase``: the key is derived from a passphrase with scrypt. The
  passphrase is also kept in the OS keystore, for scheduled runs.

Every key ever used stays in the keystore under its key ID, so files from
before a key change can still be read.

## File format (version 1), all integers big-endian

    magic      8   b"PFSENC" 0x00 0x01
    mode       1   1 = keystore key, 2 = passphrase (scrypt)
    key_id     8   first 8 bytes of SHA-256(b"pfsentinel-key-id" + key)
    salt      16   scrypt salt (zeros in keystore mode)
    log2_n     1   scrypt cost N = 2**log2_n (0 in keystore mode)
    r          1   scrypt block size (0 in keystore mode)
    p          1   scrypt parallelism (0 in keystore mode)
    nonce     7   random prefix, new for every file
    chunks         AES-256-GCM of each 64 KiB of plaintext, plus a 16-byte tag

The nonce of chunk ``i`` is ``prefix + i (4 bytes) + last (1 byte)``, where
``last`` is 1 only for the final chunk; every chunk's associated data is the
43-byte header. This is the STREAM construction (as in age and Tink): a
reordered, dropped, truncated or extended chunk, or a changed header, fails
authentication. docs/encryption.md has a standalone decryption script.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import io
import os
import struct
from collections.abc import Buffer, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

if TYPE_CHECKING:
    from pfsentinel.models.config import EncryptionPolicy
    from pfsentinel.services.credentials import CredentialService

MAGIC = b"PFSENC\x00\x01"
SUFFIX = ".enc"
MODE_KEYSTORE = 1
MODE_PASSPHRASE = 2
CHUNK = 64 * 1024
TAG = 16
_HEADER = struct.Struct(">8sB8s16sBBB7s")
HEADER_LEN = _HEADER.size  # 43 bytes

# scrypt cost: N = 2**17, r = 8 -> 128 MiB and about 0.3 s per derivation.
# Derived once per process and passphrase, not per file.
SCRYPT_LOG2_N = 17
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSPHRASE = 12

_RECOVERY_PREFIX = "PFSK"
_CHECK_BYTES = 3


class BackupCryptoError(Exception):
    """A backup file cannot be encrypted or decrypted."""


class MissingKeyError(BackupCryptoError):
    """The key for a file is not on this machine."""


def key_id(key: bytes) -> bytes:
    return hashlib.sha256(b"pfsentinel-key-id" + key).digest()[:8]


@functools.lru_cache(maxsize=8)
def derive_key(passphrase: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    if not 10 <= log2_n <= 22 or not 1 <= r <= 32 or not 1 <= p <= 16:
        raise BackupCryptoError("Unsupported scrypt parameters in file header")
    kdf = Scrypt(salt=salt, length=32, n=2**log2_n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


# --- recovery key ---------------------------------------------------------


def format_recovery_key(key: bytes) -> str:
    """'PFSK-XXXX-XXXX-...': the key plus a 3-byte check, in base32 groups of 4."""
    raw = key + hashlib.sha256(key).digest()[:_CHECK_BYTES]
    text = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join([_RECOVERY_PREFIX] + [text[i : i + 4] for i in range(0, len(text), 4)])


def parse_recovery_key(text: str) -> bytes:
    cleaned = "".join(text.split()).upper().replace("-", "")
    cleaned = cleaned.removeprefix(_RECOVERY_PREFIX)
    try:
        raw = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except (ValueError, TypeError) as e:
        raise BackupCryptoError("Recovery key is not valid (bad characters)") from e
    key, check = raw[:-_CHECK_BYTES], raw[-_CHECK_BYTES:]
    if len(key) != 32 or hashlib.sha256(key).digest()[:_CHECK_BYTES] != check:
        raise BackupCryptoError("Recovery key is not valid (typo or incomplete)")
    return key


# --- header ---------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    mode: int
    key_id: bytes
    salt: bytes = bytes(16)
    log2_n: int = 0
    r: int = 0
    p: int = 0
    nonce_prefix: bytes = b""

    def pack(self) -> bytes:
        return _HEADER.pack(
            MAGIC, self.mode, self.key_id, self.salt, self.log2_n, self.r, self.p, self.nonce_prefix
        )

    @classmethod
    def unpack(cls, data: bytes) -> Header:
        if len(data) != HEADER_LEN or not data.startswith(MAGIC[:6]):
            raise BackupCryptoError("Not a pfSentinel encrypted file")
        magic, mode, kid, salt, log2_n, r, p, prefix = _HEADER.unpack(data)
        if magic != MAGIC:
            raise BackupCryptoError("Encrypted with a newer pfSentinel; update to read it")
        if mode not in (MODE_KEYSTORE, MODE_PASSPHRASE):
            raise BackupCryptoError(f"Unknown encryption mode {mode}")
        return cls(mode, kid, salt, log2_n, r, p, prefix)


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    if counter >= 2**32:
        raise BackupCryptoError("File too large for one encrypted stream")
    return prefix + struct.pack(">I", counter) + (b"\x01" if last else b"\x00")


# --- streaming writer and reader -------------------------------------------


class EncryptingWriter(io.RawIOBase):
    """A write-only file object that encrypts into ``sink``.

    Plaintext is buffered and sealed in 64 KiB chunks; ``close()`` seals the
    final chunk (which may be empty) and closes the sink.
    """

    def __init__(self, sink: IO[bytes], key: bytes, header: Header) -> None:
        super().__init__()
        self._sink = sink
        self._aead = AESGCM(key)
        self._header = header.pack()
        self._prefix = header.nonce_prefix
        self._buffer = bytearray()
        self._counter = 0
        self.bytes_in = 0
        sink.write(self._header)

    def writable(self) -> bool:
        return True

    def write(self, data: Buffer) -> int:  # type: ignore[override]
        if self.closed:
            raise ValueError("write to closed file")
        view = bytes(data)
        self._buffer += view
        self.bytes_in += len(view)
        # Keep at least one byte back: only close() knows which chunk is last.
        while len(self._buffer) > CHUNK:
            self._seal(bytes(self._buffer[:CHUNK]), last=False)
            del self._buffer[:CHUNK]
        return len(view)

    def _seal(self, chunk: bytes, last: bool) -> None:
        nonce = _nonce(self._prefix, self._counter, last)
        self._sink.write(self._aead.encrypt(nonce, chunk, self._header))
        self._counter += 1

    def close(self) -> None:
        if not self.closed:
            try:
                self._seal(bytes(self._buffer), last=True)
                self._buffer.clear()
                self._sink.close()
            finally:
                super().close()


class DecryptingReader(io.RawIOBase):
    """A read-only file object over an encrypted backup file."""

    def __init__(self, source: IO[bytes], key: bytes, header_bytes: bytes) -> None:
        super().__init__()
        self._source = source
        self._aead = AESGCM(key)
        self._header = header_bytes
        self._prefix = Header.unpack(header_bytes).nonce_prefix
        self._counter = 0
        self._plain = b""
        self._pending = source.read(CHUNK + TAG)
        self._done = False

    def readable(self) -> bool:
        return True

    def _next_chunk(self) -> bytes:
        if self._done:
            return b""
        current = self._pending
        self._pending = self._source.read(CHUNK + TAG)
        last = not self._pending
        if len(current) < TAG:
            raise BackupCryptoError("Encrypted file is truncated")
        try:
            plain = self._aead.decrypt(
                _nonce(self._prefix, self._counter, last), current, self._header
            )
        except InvalidTag as e:
            raise BackupCryptoError(
                "Encrypted file failed authentication: it was changed, truncated, "
                "or the key is wrong"
            ) from e
        self._counter += 1
        self._done = last
        return plain

    def readinto(self, buffer: memoryview | bytearray) -> int:  # type: ignore[override]
        while not self._plain and not self._done:
            self._plain = self._next_chunk()
        n = min(len(buffer), len(self._plain))
        buffer[:n] = self._plain[:n]
        self._plain = self._plain[n:]
        return n

    def close(self) -> None:
        if not self.closed:
            self._source.close()
            super().close()


# --- keys -----------------------------------------------------------------


def _secret_name(kid: bytes) -> str:
    return f"__backup_secret__{kid.hex()}"


class BackupCrypto:
    """Encrypts new backup files and decrypts any file it has a key for.

    ``recovery_key`` and ``passphrase`` let `pfs backup decrypt` read files
    on a machine whose keystore does not hold the key.
    """

    def __init__(
        self,
        policy: EncryptionPolicy,
        creds: CredentialService,
        recovery_key: str | None = None,
        passphrase: str | None = None,
    ) -> None:
        self._policy = policy
        self._creds = creds
        self._recovery = parse_recovery_key(recovery_key) if recovery_key else None
        self._passphrase = passphrase

    @property
    def enabled(self) -> bool:
        return self._policy.mode != "off"

    # setup ------------------------------------------------------------------

    def setup_keystore(self) -> str:
        """Create a new random key, store it, return its recovery key."""
        key = AESGCM.generate_key(bit_length=256)
        kid = key_id(key)
        self._store_secret(kid, "k:" + base64.b64encode(key).decode("ascii"))
        self._policy.mode = "keystore"
        self._policy.key_id = kid.hex()
        self._policy.salt = None
        return format_recovery_key(key)

    def setup_passphrase(self, passphrase: str) -> None:
        if len(passphrase) < MIN_PASSPHRASE:
            raise BackupCryptoError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters")
        salt = os.urandom(16)
        key = derive_key(passphrase, salt, SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P)
        kid = key_id(key)
        self._store_secret(kid, "p:" + passphrase)
        self._policy.mode = "passphrase"
        self._policy.key_id = kid.hex()
        self._policy.salt = salt.hex()

    def recovery_key(self) -> str:
        """The recovery key of the current keystore key."""
        if self._policy.mode != "keystore" or not self._policy.key_id:
            raise BackupCryptoError("Recovery keys exist only in keystore mode")
        secret = self._creds.get_secret(_secret_name(bytes.fromhex(self._policy.key_id)))
        if not secret or not secret.startswith("k:"):
            raise MissingKeyError("The backup key is not in this machine's keystore")
        return format_recovery_key(base64.b64decode(secret[2:]))

    def _store_secret(self, kid: bytes, value: str) -> None:
        if not self._creds.is_persistent:
            raise BackupCryptoError(
                "No persistent keystore on this machine: the key would be lost on exit"
            )
        self._creds.store_secret(_secret_name(kid), value)

    # encrypt ----------------------------------------------------------------

    def _current(self) -> tuple[bytes, Header]:
        if not self._policy.key_id:
            raise BackupCryptoError("Encryption is on but no key is set up: pfs encryption setup")
        kid = bytes.fromhex(self._policy.key_id)
        prefix = os.urandom(7)
        if self._policy.mode == "keystore":
            return self._key_for(Header(MODE_KEYSTORE, kid)), Header(
                MODE_KEYSTORE, kid, nonce_prefix=prefix
            )
        salt = bytes.fromhex(self._policy.salt or "")
        header = Header(
            MODE_PASSPHRASE, kid, salt, SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P, nonce_prefix=prefix
        )
        return self._key_for(header), header

    def open_write(self, dest: Path) -> tuple[IO[bytes], Path]:
        """Open ``dest`` for a new backup file; returns (file, final path).

        With encryption on, the final path ends in ``.enc`` and the file object
        encrypts everything written to it.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not self.enabled:
            return _open_private(dest), dest
        key, header = self._current()
        final = dest.with_name(dest.name + SUFFIX)
        return EncryptingWriter(_open_private(final), key, header), final  # type: ignore[return-value]

    @contextlib.contextmanager
    def create(self, dest: Path) -> Iterator[tuple[IO[bytes], Path]]:
        """Write a new backup file; on any error the partial file is deleted.

        Closing an encrypting writer seals a final chunk, so a file cut off by
        an error would otherwise look complete.
        """
        handle, final = self.open_write(dest)
        try:
            yield handle, final
        except BaseException:
            with contextlib.suppress(Exception):
                handle.close()
            final.unlink(missing_ok=True)
            raise
        handle.close()

    def write_bytes(self, dest: Path, data: bytes) -> Path:
        with self.create(dest) as (handle, final):
            handle.write(data)
        return final

    # decrypt ----------------------------------------------------------------

    def _key_for(self, header: Header) -> bytes:
        if self._recovery is not None and key_id(self._recovery) == header.key_id:
            return self._recovery
        secret = self._creds.get_secret(_secret_name(header.key_id))
        if header.mode == MODE_PASSPHRASE:
            passphrase = self._passphrase or (
                secret[2:] if secret and secret.startswith("p:") else None
            )
            if passphrase is None:
                raise MissingKeyError(
                    "This file was encrypted with a passphrase that is not stored on this "
                    "machine. Run: pfs backup decrypt FILE --passphrase"
                )
            key = derive_key(passphrase, header.salt, header.log2_n, header.r, header.p)
        else:
            if not secret or not secret.startswith("k:"):
                raise MissingKeyError(
                    f"The key for this file (ID {header.key_id.hex()}) is not on this machine. "
                    "Run: pfs backup decrypt FILE --recovery-key"
                )
            key = base64.b64decode(secret[2:])
        if key_id(key) != header.key_id:
            raise BackupCryptoError("Wrong passphrase or recovery key for this file")
        return key

    def open_read(self, path: Path) -> IO[bytes]:
        """Open a backup file for reading, decrypting it when it is encrypted."""
        source = path.open("rb")
        if not is_encrypted(path):
            return source
        try:
            header_bytes = source.read(HEADER_LEN)
            key = self._key_for(Header.unpack(header_bytes))
            return io.BufferedReader(DecryptingReader(source, key, header_bytes))  # type: ignore[arg-type]
        except BaseException:
            source.close()
            raise

    def read_bytes(self, path: Path) -> bytes:
        with self.open_read(path) as handle:
            return handle.read()


def is_encrypted(path: Path) -> bool:
    return path.name.endswith(SUFFIX)


def plain_name(name: str) -> str:
    """'x.xml.gz.enc' -> 'x.xml.gz'."""
    return name.removesuffix(SUFFIX)


def _open_private(path: Path) -> IO[bytes]:
    """Create a file readable by the owner only (on POSIX)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    return os.fdopen(fd, "wb")
