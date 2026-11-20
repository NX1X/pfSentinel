"""The age v1 file format (https://age-encryption.org/v1, c2sp.org/age).

Only the X25519 recipient type is implemented: that is what ``age-keygen``
makes, and what pfSentinel writes. Files from this module open with the
standard ``age`` tool, and files from ``age -r age1...`` open here.

Built from ``cryptography`` primitives only:

- File key: 16 random bytes per file.
- X25519 stanza: ``-> X25519 <ephemeral share>`` and a body of
  ChaCha20-Poly1305(wrap key, nonce 12 zero bytes, file key), where
  wrap key = HKDF-SHA-256(shared secret, salt = share || recipient,
  info = "age-encryption.org/v1/X25519").
- Header MAC: HMAC-SHA-256 with key HKDF-SHA-256(file key, salt = "",
  info = "header"), over the header up to and including ``---``.
- Payload: a 16-byte random nonce, then 64 KiB chunks of ChaCha20-Poly1305
  with key HKDF-SHA-256(file key, salt = nonce, info = "payload") and nonce
  ``11-byte big-endian counter || last flag``. The final chunk may be short
  but is empty only when the whole payload is empty.
- base64 is RFC 4648 standard base64 without padding; non-canonical input
  is rejected.
- Bech32 (BIP 173) without the 90-character length limit.

The error classes match the failure kinds of the C2SP CCTV age test
vectors (tests/fixtures/age/): header, no match, HMAC and payload.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import re
from collections.abc import Buffer, Iterable
from dataclasses import dataclass
from typing import IO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION_LINE = b"age-encryption.org/v1\n"
INTRO = b"age-encryption.org/"
CHUNK = 64 * 1024
TAG = 16
FILE_KEY_LEN = 16
NONCE_LEN = 16
X25519_INFO = b"age-encryption.org/v1/X25519"
RECIPIENT_HRP = "age"
IDENTITY_HRP = "age-secret-key-"

# Limits for reading a header. Our own headers are a few hundred bytes; the
# limits only stop a hostile file from making us buffer without end.
MAX_LINE = 16 * 1024
MAX_HEADER = 1024 * 1024

_ZERO_NONCE = bytes(12)
_B64_CHARS = re.compile(rb"[A-Za-z0-9+/]*")
_MAC_LINE = re.compile(rb"--- ([A-Za-z0-9+/]{43})\n")


class AgeError(Exception):
    """An age file cannot be read or written."""


class AgeHeaderError(AgeError):
    """The header does not parse, or a stanza for our recipient type is malformed."""


class AgeNoMatchError(AgeError):
    """The header parses, but no given identity opens any stanza."""


class AgeHmacError(AgeError):
    """A file key was found, but the header MAC does not match."""


class AgePayloadError(AgeError):
    """The encrypted payload fails authentication, is truncated or has extra data."""


# --- base64 (RFC 4648, no padding, canonical) --------------------------------


def b64encode(data: bytes) -> bytes:
    return base64.b64encode(data).rstrip(b"=")


def b64decode(text: bytes) -> bytes:
    """Decode unpadded base64, rejecting padding and non-canonical encodings."""
    if not _B64_CHARS.fullmatch(text) or len(text) % 4 == 1:
        raise AgeHeaderError("Invalid base64 in age header")
    raw = base64.b64decode(text + b"=" * (-len(text) % 4))
    if b64encode(raw) != text:
        raise AgeHeaderError("Non-canonical base64 in age header")
    return raw


# --- Bech32 (BIP 173, no length limit) ----------------------------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _polymod(values: Iterable[int]) -> int:
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i, gen in enumerate(_BECH32_GEN):
            if (top >> i) & 1:
                chk ^= gen
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert_bits(data: Iterable[int], from_bits: int, to_bits: int, pad: bool) -> list[int]:
    acc = 0
    bits = 0
    out: list[int] = []
    max_value = (1 << to_bits) - 1
    for value in data:
        acc = (acc << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((acc >> bits) & max_value)
    if pad:
        if bits:
            out.append((acc << (to_bits - bits)) & max_value)
    elif bits >= from_bits or (acc << (to_bits - bits)) & max_value:
        raise ValueError("invalid padding in Bech32 data")
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    """Encode bytes as Bech32 with a lowercase HRP; the result is lowercase."""
    values = _convert_bits(data, 8, 5, pad=True)
    poly = _polymod(_hrp_expand(hrp) + values + [0] * 6) ^ 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[v] for v in values + checksum)


def bech32_decode(text: str) -> tuple[str, bytes]:
    """Decode a Bech32 string to (lowercase HRP, bytes). Raises ValueError."""
    if any(not 33 <= ord(c) <= 126 for c in text):
        raise ValueError("invalid character in Bech32 string")
    if text.lower() != text and text.upper() != text:
        raise ValueError("mixed case in Bech32 string")
    text = text.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text):
        raise ValueError("Bech32 separator in the wrong place")
    hrp = text[:pos]
    try:
        values = [_BECH32_CHARSET.index(c) for c in text[pos + 1 :]]
    except ValueError as e:
        raise ValueError("invalid character in Bech32 data") from e
    if _polymod(_hrp_expand(hrp) + values) != 1:
        raise ValueError("Bech32 checksum is wrong (typo?)")
    return hrp, bytes(_convert_bits(values[:-6], 5, 8, pad=False))


# --- keys ----------------------------------------------------------------------


def _hkdf(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(ikm)


def parse_recipient(text: str) -> bytes:
    """'age1...' -> the 32-byte X25519 public key."""
    try:
        hrp, data = bech32_decode(text.strip())
    except ValueError as e:
        raise AgeError(f"Not a valid age recipient: {e}") from e
    if hrp != RECIPIENT_HRP or len(data) != 32:
        raise AgeError(
            "Only X25519 age recipients (age1..., from age-keygen) are supported; "
            "post-quantum, plugin and SSH recipients are not"
        )
    return data


def format_recipient(public_key: bytes) -> str:
    return bech32_encode(RECIPIENT_HRP, public_key)


@dataclass(frozen=True)
class Identity:
    """An X25519 age identity (the secret key)."""

    secret: bytes

    @classmethod
    def generate(cls) -> Identity:
        return cls(X25519PrivateKey.generate().private_bytes_raw())

    @classmethod
    def parse(cls, text: str) -> Identity:
        """'AGE-SECRET-KEY-1...' -> Identity."""
        try:
            hrp, data = bech32_decode(text.strip())
        except ValueError as e:
            raise AgeError(f"Not a valid age identity: {e}") from e
        if hrp != IDENTITY_HRP or len(data) != 32:
            raise AgeError(
                "Only X25519 age identities (AGE-SECRET-KEY-1..., from age-keygen) are supported"
            )
        return cls(data)

    def __str__(self) -> str:
        return bech32_encode(IDENTITY_HRP, self.secret).upper()

    def __repr__(self) -> str:  # never print the secret by accident
        return f"Identity({self.recipient})"

    @property
    def public_key(self) -> bytes:
        return X25519PrivateKey.from_private_bytes(self.secret).public_key().public_bytes_raw()

    @property
    def recipient(self) -> str:
        return format_recipient(self.public_key)


def parse_identity_file(text: str) -> list[Identity]:
    """Identities from an age identity file: one per line, '#' comments allowed."""
    identities = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            identities.append(Identity.parse(line))
    if not identities:
        raise AgeError("No age identity found in the identity file")
    return identities


# --- X25519 stanza -------------------------------------------------------------


@dataclass(frozen=True)
class Stanza:
    args: tuple[bytes, ...]
    body: bytes

    def encode(self) -> bytes:
        text = b64encode(self.body)
        lines = [text[i : i + 64] for i in range(0, len(text), 64)]
        if not lines or len(lines[-1]) == 64:
            lines.append(b"")  # the body always ends with a line shorter than 64
        return b"-> " + b" ".join(self.args) + b"\n" + b"".join(line + b"\n" for line in lines)


def wrap_x25519(file_key: bytes, recipient: bytes) -> Stanza:
    ephemeral = X25519PrivateKey.generate()
    share = ephemeral.public_key().public_bytes_raw()
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient))
    wrap_key = _hkdf(shared, share + recipient, X25519_INFO)
    body = ChaCha20Poly1305(wrap_key).encrypt(_ZERO_NONCE, file_key, None)
    return Stanza((b"X25519", b64encode(share)), body)


def _unwrap_x25519(stanza: Stanza, identity: Identity) -> bytes | None:
    """The file key, or None when the stanza is for someone else.

    Raises AgeHeaderError for a malformed X25519 stanza, as the spec requires.
    """
    if len(stanza.args) != 2:
        raise AgeHeaderError("X25519 stanza must have exactly two arguments")
    share = b64decode(stanza.args[1])
    if len(share) != 32:
        raise AgeHeaderError("X25519 stanza share must be 32 bytes")
    if len(stanza.body) != FILE_KEY_LEN + TAG:
        raise AgeHeaderError("X25519 stanza body must be 32 bytes")
    try:
        shared = X25519PrivateKey.from_private_bytes(identity.secret).exchange(
            X25519PublicKey.from_public_bytes(share)
        )
    except ValueError as e:  # OpenSSL refuses an all-zero shared secret
        raise AgeHeaderError("X25519 stanza share is a low-order point") from e
    if not any(shared):
        raise AgeHeaderError("X25519 stanza share is a low-order point")
    wrap_key = _hkdf(shared, share + identity.public_key, X25519_INFO)
    try:
        return ChaCha20Poly1305(wrap_key).decrypt(_ZERO_NONCE, stanza.body, None)
    except InvalidTag:
        return None


# --- header ----------------------------------------------------------------------


def _header_mac(file_key: bytes, header_up_to_dashes: bytes) -> bytes:
    key = _hkdf(file_key, b"", b"header")
    return hmac.new(key, header_up_to_dashes, hashlib.sha256).digest()


def _is_vchar_arg(arg: bytes) -> bool:
    return bool(arg) and all(0x21 <= c <= 0x7E for c in arg)


class _LineReader:
    """Reads header lines and keeps the raw bytes for the MAC."""

    def __init__(self, source: IO[bytes]) -> None:
        self._source = source
        self.raw = bytearray()

    def line(self) -> bytes:
        line = self._source.readline(MAX_LINE)
        if not line.endswith(b"\n"):
            raise AgeHeaderError("age header is truncated or has an over-long line")
        self.raw += line
        if len(self.raw) > MAX_HEADER:
            raise AgeHeaderError("age header is too large")
        return line


def read_header(source: IO[bytes]) -> tuple[list[Stanza], bytes, bytes]:
    """Parse a header from ``source``; returns (stanzas, MAC input, MAC).

    ``source`` is left at the first byte of the payload.
    """
    reader = _LineReader(source)
    first = reader.line()
    if first != VERSION_LINE:
        if first.startswith(INTRO):
            raise AgeHeaderError("Unsupported age format version")
        raise AgeHeaderError("Not an age file")
    stanzas: list[Stanza] = []
    while True:
        line = reader.line()
        if line.startswith(b"---"):
            match = _MAC_LINE.fullmatch(line)
            if not match:
                raise AgeHeaderError("Malformed age header MAC line")
            if not stanzas:
                raise AgeHeaderError("age header has no recipient stanza")
            mac_input = bytes(reader.raw[: len(reader.raw) - len(line) + 3])
            return stanzas, mac_input, b64decode(match.group(1))
        if not line.startswith(b"-> "):
            raise AgeHeaderError("Malformed age header line")
        args = tuple(line[3:-1].split(b" "))
        if not all(_is_vchar_arg(a) for a in args):
            raise AgeHeaderError("Malformed age stanza arguments")
        body = bytearray()
        while True:
            body_line = reader.line()[:-1]
            if len(body_line) > 64:
                raise AgeHeaderError("age stanza body line is longer than 64 columns")
            body += body_line
            if len(body_line) < 64:
                break
        stanzas.append(Stanza(args, b64decode(bytes(body))))


def unwrap_file_key(stanzas: list[Stanza], identities: Iterable[Identity]) -> bytes:
    """Find the file key with any of ``identities``. Raises AgeNoMatchError."""
    if any(s.args[0] == b"scrypt" for s in stanzas) and len(stanzas) != 1:
        raise AgeHeaderError("An scrypt stanza must be the only stanza in the header")
    for identity in identities:
        for stanza in stanzas:
            if stanza.args[0] != b"X25519":
                continue  # other recipient types are ignored, as the spec requires
            file_key = _unwrap_x25519(stanza, identity)
            if file_key is not None:
                return file_key
    raise AgeNoMatchError("No given age identity can open this file")


def build_header(file_key: bytes, recipients: Iterable[bytes]) -> bytes:
    """A complete header, MAC line included, for X25519 ``recipients``."""
    body = VERSION_LINE + b"".join(wrap_x25519(file_key, r).encode() for r in recipients)
    if body == VERSION_LINE:
        raise AgeError("An age file needs at least one recipient")
    body += b"---"
    return body + b" " + b64encode(_header_mac(file_key, body)) + b"\n"


# --- payload ---------------------------------------------------------------------


def _chunk_nonce(counter: int, last: bool) -> bytes:
    return counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


class EncryptingWriter(io.RawIOBase):
    """A write-only file object that writes an age file into ``sink``.

    Plaintext is sealed in 64 KiB chunks; ``close()`` seals the final chunk
    and closes the sink. At least one byte is held back until ``close()``, so
    the final chunk is empty only when nothing was written at all.
    """

    def __init__(self, sink: IO[bytes], recipients: Iterable[bytes]) -> None:
        super().__init__()
        file_key = os.urandom(FILE_KEY_LEN)
        header = build_header(file_key, recipients)
        nonce = os.urandom(NONCE_LEN)
        self._sink = sink
        self._aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
        self._buffer = bytearray()
        self._counter = 0
        self.bytes_in = 0
        sink.write(header + nonce)

    def writable(self) -> bool:
        return True

    def write(self, data: Buffer) -> int:  # type: ignore[override]
        if self.closed:
            raise ValueError("write to closed file")
        view = bytes(data)
        self._buffer += view
        self.bytes_in += len(view)
        while len(self._buffer) > CHUNK:
            self._seal(bytes(self._buffer[:CHUNK]), last=False)
            del self._buffer[:CHUNK]
        return len(view)

    def _seal(self, chunk: bytes, last: bool) -> None:
        self._sink.write(self._aead.encrypt(_chunk_nonce(self._counter, last), chunk, None))
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
    """A read-only file object over the payload of an age file.

    Each chunk is released only after it authenticates. A full-size chunk is
    tried as a middle chunk first, then as the final one (a final chunk may
    be exactly 64 KiB); data after the final chunk is an error.
    """

    def __init__(self, source: IO[bytes], payload_key: bytes) -> None:
        super().__init__()
        self._source = source
        self._aead = ChaCha20Poly1305(payload_key)
        self._counter = 0
        self._plain = b""
        self._done = False
        self._error: AgePayloadError | None = None

    def readable(self) -> bool:
        return True

    def _open(self, chunk: bytes, last: bool) -> bytes | None:
        try:
            return self._aead.decrypt(_chunk_nonce(self._counter, last), chunk, None)
        except InvalidTag:
            return None

    def _next_chunk(self) -> bytes:
        chunk = _read_full(self._source, CHUNK + TAG)
        if not chunk:
            raise AgePayloadError("age file is truncated: it ends without a final chunk")
        last = len(chunk) < CHUNK + TAG
        if last and len(chunk) == TAG and self._counter > 0:
            raise AgePayloadError("age file has an empty final chunk")
        plain = self._open(chunk, last)
        if plain is None and not last:
            last = True
            plain = self._open(chunk, last)
        if plain is None:
            raise AgePayloadError("age file failed authentication: it was changed or truncated")
        self._counter += 1
        if last:
            self._done = True
            if self._source.read(1):
                self._error = AgePayloadError("age file has data after its final chunk")
        return plain

    def readinto(self, buffer: memoryview | bytearray) -> int:  # type: ignore[override]
        while not self._plain and not self._done:
            self._plain = self._next_chunk()
        if not self._plain and self._error is not None:
            raise self._error
        n = min(len(buffer), len(self._plain))
        buffer[:n] = self._plain[:n]
        self._plain = self._plain[n:]
        return n

    def close(self) -> None:
        if not self.closed:
            self._source.close()
            super().close()


def _read_full(source: IO[bytes], size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = source.read(size - len(data))
        if not part:
            break
        data += part
    return bytes(data)


def open_payload(source: IO[bytes], identities: Iterable[Identity]) -> DecryptingReader:
    """Read the header from ``source`` and return a reader of the plaintext.

    Raises AgeHeaderError, AgeNoMatchError or AgeHmacError before any
    plaintext is released; payload errors surface while reading.
    """
    stanzas, mac_input, mac = read_header(source)
    file_key = unwrap_file_key(stanzas, identities)
    if not hmac.compare_digest(_header_mac(file_key, mac_input), mac):
        raise AgeHmacError("age header MAC does not match: the header was changed")
    nonce = _read_full(source, NONCE_LEN)
    if len(nonce) != NONCE_LEN:
        raise AgeHeaderError("age file is truncated: the payload nonce is missing")
    return DecryptingReader(source, _hkdf(file_key, nonce, b"payload"))
