"""The age v1 format: C2SP CCTV test vectors, round trips, tampering, encodings."""

from __future__ import annotations

import hashlib
import io
import zlib
from pathlib import Path

import pytest

from pfsentinel.services import age_format as af

VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "age"
KINDS: dict[type[af.AgeError], str] = {
    af.AgeHeaderError: "header failure",
    af.AgeNoMatchError: "no match",
    af.AgeHmacError: "HMAC failure",
    af.AgePayloadError: "payload failure",
}


def _vector(path: Path) -> tuple[dict[str, list[str]], bytes]:
    head, body = path.read_bytes().split(b"\n\n", 1)
    meta: dict[str, list[str]] = {}
    for line in head.decode().splitlines():
        key, value = line.split(": ", 1)
        meta.setdefault(key, []).append(value)
    if meta.get("compressed") == ["zlib"]:
        body = zlib.decompress(body)
    return meta, body


def _decrypt(data: bytes, identities: list[af.Identity]) -> tuple[str, bytes]:
    """(outcome, plaintext released before any error), like the CCTV harness."""
    released = bytearray()
    try:
        reader = af.open_payload(io.BytesIO(data), identities)
        buffer = bytearray(8192)
        while n := reader.readinto(buffer):
            released += buffer[:n]
    except af.AgeError as e:
        return KINDS[type(e)], bytes(released)
    return "success", bytes(released)


VECTOR_FILES = sorted(p for p in VECTORS.iterdir() if p.name != "README.md")


def test_the_vector_set_is_complete() -> None:
    assert len(VECTOR_FILES) == 68


@pytest.mark.parametrize("path", VECTOR_FILES, ids=lambda p: p.name)
def test_cctv_vector(path: Path) -> None:
    meta, data = _vector(path)
    identities = [af.Identity.parse(i) for i in meta.get("identity", [])]
    outcome, released = _decrypt(data, identities)
    assert outcome == meta["expect"][0], meta.get("comment")
    if "payload" in meta:
        assert hashlib.sha256(released).hexdigest() == meta["payload"][0]
    elif outcome != "success":
        assert released == b""


# --- round trips -------------------------------------------------------------------

SIZES = [0, 1, af.CHUNK - 1, af.CHUNK, af.CHUNK + 1, 2 * af.CHUNK, 3 * af.CHUNK + 5]


def _encrypt(data: bytes, *recipients: af.Identity, step: int = 10_000) -> bytes:
    sink = io.BytesIO()
    sink.close = lambda: None  # type: ignore[method-assign]  # keep the bytes readable
    writer = af.EncryptingWriter(sink, [r.public_key for r in recipients])
    for i in range(0, len(data), step):
        writer.write(data[i : i + step])
    writer.close()
    return sink.getvalue()


@pytest.mark.parametrize("size", SIZES)
def test_round_trip_at_chunk_edges(size: int) -> None:
    me = af.Identity.generate()
    data = bytes(range(256)) * (size // 256) + bytes(size % 256)
    enc = _encrypt(data, me)
    assert enc.startswith(af.VERSION_LINE)
    assert _decrypt(enc, [me]) == ("success", data)


@pytest.mark.parametrize(
    ("size", "chunks"), [(0, 1), (1, 1), (af.CHUNK, 1), (af.CHUNK + 1, 2), (2 * af.CHUNK, 2)]
)
def test_final_chunk_is_never_empty_unless_the_file_is(size: int, chunks: int) -> None:
    me = af.Identity.generate()
    enc = _encrypt(bytes(size), me)
    header_end = enc.index(b"\n---") + len(b"\n--- ") + 43 + 1
    payload = len(enc) - header_end - af.NONCE_LEN
    assert payload == size + chunks * af.TAG


def test_every_recipient_can_decrypt_and_others_cannot() -> None:
    a, b, stranger = af.Identity.generate(), af.Identity.generate(), af.Identity.generate()
    enc = _encrypt(b"config", a, b)
    assert enc.count(b"\n-> X25519 ") == 2
    assert _decrypt(enc, [a]) == ("success", b"config")
    assert _decrypt(enc, [b]) == ("success", b"config")
    assert _decrypt(enc, [stranger]) == ("no match", b"")
    assert _decrypt(enc, [stranger, b]) == ("success", b"config")


def test_each_file_has_a_fresh_file_key_and_nonce() -> None:
    me = af.Identity.generate()
    first = _encrypt(b"same", me)
    second = _encrypt(b"same", me)
    assert first != second, "a fresh file key and nonce every time"


_SHARE_BYTE = len(af.VERSION_LINE) + len(b"-> X25519 ") + 2  # inside the ephemeral share


def _tamper(enc: bytes, edit) -> bytes:
    raw = bytearray(enc)
    edit(raw)
    return bytes(raw)


@pytest.mark.parametrize(
    ("edit", "outcome"),
    [
        pytest.param(lambda b: b.__setitem__(-5, b[-5] ^ 1), "payload failure", id="payload-bit"),
        pytest.param(lambda b: b.__delitem__(slice(-10, None)), "payload failure", id="cut-end"),
        pytest.param(
            lambda b: b.__delitem__(slice(-(af.CHUNK + af.TAG + 5), None)),
            "payload failure",
            id="dropped-final-chunk",
        ),
        pytest.param(lambda b: b.extend(b"x"), "payload failure", id="appended-byte"),
        pytest.param(
            lambda b: b.__setitem__(_SHARE_BYTE, ord("A") if b[_SHARE_BYTE] != ord("A") else 66),
            "no match",
            id="changed-share",
        ),
        pytest.param(
            lambda b: b.__setitem__(slice(0, len(af.VERSION_LINE)), b"age-encryption.org/v2\n"),
            "header failure",
            id="version",
        ),
    ],
)
def test_tampering_is_detected(edit, outcome: str) -> None:
    me = af.Identity.generate()
    enc = _encrypt(b"x" * (2 * af.CHUNK + 5), me)
    assert _decrypt(_tamper(enc, edit), [me])[0] == outcome


def test_a_changed_header_fails_the_mac() -> None:
    """An extra, unknown stanza is legal, but not added after the fact."""
    me = af.Identity.generate()
    enc = _encrypt(b"config", me)
    forged = enc.replace(b"\n---", b"\n-> extra\n\n---", 1)
    assert _decrypt(forged, [me])[0] == "HMAC failure"


def test_payload_errors_surface_through_a_buffered_reader() -> None:
    me = af.Identity.generate()
    enc = _tamper(_encrypt(b"x" * 100, me), lambda b: b.__setitem__(-3, b[-3] ^ 1))
    reader = io.BufferedReader(af.open_payload(io.BytesIO(enc), [me]))  # type: ignore[arg-type]
    with pytest.raises(af.AgePayloadError):
        reader.read()


# --- encodings ------------------------------------------------------------------------


def test_spec_example_identity_and_recipient() -> None:
    """The X25519 example from c2sp.org/age: identity 0x42 * 32."""
    identity = af.Identity(bytes([0x42]) * 32)
    assert str(identity) == "AGE-SECRET-KEY-1" + "GFPYYSJZ" * 6 + "GFPQ4EGAEX"
    assert identity.recipient == "age1zvkyg2lqzraa2lnjvqej32nkuu0ues2s82hzrye869xeexvn73equnujwj"
    assert af.Identity.parse(str(identity).lower()) == identity
    assert af.parse_recipient(identity.recipient) == identity.public_key


def test_identity_repr_does_not_show_the_secret() -> None:
    identity = af.Identity.generate()
    assert str(identity) not in repr(identity)
    assert identity.recipient in repr(identity)


@pytest.mark.parametrize(
    "text", ["A12UEL5L", "a12uel5l", "abcdef1qpzry9x8gf2tvdw0s3jn54khce6mua7lmqqqxw"]
)
def test_bip173_valid_strings(text: str) -> None:
    hrp, data = af.bech32_decode(text)
    assert af.bech32_encode(hrp, data) == text.lower()


@pytest.mark.parametrize(
    "text",
    [
        "1pzry9x0s0muk",  # empty HRP
        "x1b4n0q5v",  # invalid data character
        "li1dgmt3",  # checksum too short
        "A1G7SGD8",  # checksum over the uppercase HRP
        "a12UEL5L",  # mixed case
        "de1lg7wt\x7f",  # invalid character
    ],
)
def test_bip173_invalid_strings(text: str) -> None:
    with pytest.raises(ValueError):
        af.bech32_decode(text)


def test_recipient_checks() -> None:
    good = af.Identity.generate().recipient
    with pytest.raises(af.AgeError, match="checksum"):
        af.parse_recipient(good[:-1] + ("q" if good[-1] != "q" else "p"))
    with pytest.raises(af.AgeError, match="Only X25519"):
        af.parse_recipient(af.bech32_encode("age1pq", bytes(32)))
    with pytest.raises(af.AgeError, match="Only X25519"):
        af.parse_recipient(af.bech32_encode("age", bytes(31)))
    with pytest.raises(af.AgeError, match="Only X25519"):
        af.Identity.parse(good)  # a public key is not an identity


def test_identity_file_with_comments() -> None:
    a, b = af.Identity.generate(), af.Identity.generate()
    text = f"# created: today\n# public key: {a.recipient}\n{a}\n\n{b}\n"
    assert af.parse_identity_file(text) == [a, b]
    with pytest.raises(af.AgeError, match="No age identity"):
        af.parse_identity_file("# only a comment\n")


def test_long_stanza_bodies_wrap_at_64_columns() -> None:
    """A body that fills its last line exactly needs an empty final line."""
    for size in (0, 31, 48, 96):
        stanza = af.Stanza((b"test", b"arg"), bytes(range(size)))
        encoded = stanza.encode()
        lines = encoded.split(b"\n")[1:-1]
        assert all(len(line) <= 64 for line in lines) and len(lines[-1]) < 64
        source = io.BytesIO(af.VERSION_LINE + encoded + b"--- " + b"A" * 43 + b"\n")
        stanzas, _, _ = af.read_header(source)
        assert stanzas == [stanza]


@pytest.mark.parametrize("text", [b"QQ==", b"QR", b"Q", b"QQ\n", b"Q Q"])
def test_base64_must_be_canonical_and_unpadded(text: bytes) -> None:
    with pytest.raises(af.AgeHeaderError):
        af.b64decode(text)


def test_an_oversized_header_is_refused() -> None:
    line = b"-> " + b"a" * (af.MAX_LINE + 10) + b"\n"
    with pytest.raises(af.AgeHeaderError):
        af.read_header(io.BytesIO(af.VERSION_LINE + line))
