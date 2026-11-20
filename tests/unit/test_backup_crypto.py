"""Encryption at rest: the file formats, all key modes, and tamper detection."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from pfsentinel.models.config import EncryptionPolicy
from pfsentinel.services import age_format
from pfsentinel.services import backup_crypto as bc
from pfsentinel.services.backup_crypto import BackupCrypto, BackupCryptoError, MissingKeyError


class _Keystore:
    """A credential store of one machine (the real one is patched per test)."""

    is_persistent = True

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    def store_secret(self, name: str, value: str) -> None:
        self.secrets[name] = value

    def get_secret(self, name: str) -> str | None:
        return self.secrets.get(name)


@pytest.fixture
def keystore_crypto() -> tuple[BackupCrypto, _Keystore, str]:
    store = _Keystore()
    crypto = BackupCrypto(EncryptionPolicy(), store)  # type: ignore[arg-type]
    recovery = crypto.setup_keystore()
    return crypto, store, recovery


SIZES = [0, 1, bc.CHUNK - 1, bc.CHUNK, bc.CHUNK + 1, 3 * bc.CHUNK + 5]


@pytest.mark.parametrize("size", SIZES)
def test_round_trip_at_chunk_edges(keystore_crypto, tmp_path: Path, size: int) -> None:
    crypto, _, _ = keystore_crypto
    data = bytes(range(256)) * (size // 256) + bytes(size % 256)
    final = crypto.write_bytes(tmp_path / "f.xml.gz", data)
    assert final.name == "f.xml.gz.enc"
    assert crypto.read_bytes(final) == data
    if size >= 64:
        assert data[:64] not in final.read_bytes()


def test_header_is_43_bytes() -> None:
    assert bc.HEADER_LEN == 43


def _tamper(path: Path, edit) -> None:
    raw = bytearray(path.read_bytes())
    edit(raw)
    path.write_bytes(bytes(raw))


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda b: b.__setitem__(60, b[60] ^ 1), id="flipped-byte"),
        pytest.param(lambda b: b.__setitem__(9, b[9] ^ 1), id="changed-header"),
        pytest.param(lambda b: b.__delitem__(slice(-10, None)), id="cut-end"),
        pytest.param(
            lambda b: b.__delitem__(slice(bc.HEADER_LEN + bc.CHUNK + bc.TAG, None)),
            id="dropped-final-chunks",
        ),
        pytest.param(
            lambda b: b.__setitem__(
                slice(bc.HEADER_LEN, bc.HEADER_LEN + 2 * (bc.CHUNK + bc.TAG)),
                b[bc.HEADER_LEN + bc.CHUNK + bc.TAG : bc.HEADER_LEN + 2 * (bc.CHUNK + bc.TAG)]
                + b[bc.HEADER_LEN : bc.HEADER_LEN + bc.CHUNK + bc.TAG],
            ),
            id="swapped-chunks",
        ),
        pytest.param(lambda b: b.extend(b[-(bc.TAG + 5) :]), id="appended-data"),
    ],
)
def test_any_change_fails_authentication(keystore_crypto, tmp_path: Path, edit) -> None:
    crypto, _, _ = keystore_crypto
    final = crypto.write_bytes(tmp_path / "f", b"x" * (3 * bc.CHUNK + 5))
    _tamper(final, edit)
    with pytest.raises(BackupCryptoError):
        crypto.read_bytes(final)


def test_a_failed_write_leaves_no_file(keystore_crypto, tmp_path: Path) -> None:
    """A cut-off encrypted file would look complete, so it must not remain."""
    crypto, _, _ = keystore_crypto
    with pytest.raises(RuntimeError), crypto.create(tmp_path / "f") as (sink, final):
        sink.write(b"partial")
        raise RuntimeError("connection lost")
    assert not final.exists()


class TestKeystoreMode:
    def test_recovery_key_decrypts_on_another_machine(self, keystore_crypto, tmp_path) -> None:
        crypto, _, recovery = keystore_crypto
        final = crypto.write_bytes(tmp_path / "f", b"secret config")

        elsewhere = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
        with pytest.raises(MissingKeyError, match="--recovery-key"):
            elsewhere.read_bytes(final)
        other = BackupCrypto(EncryptionPolicy(), _Keystore(), recovery_key=recovery)  # type: ignore[arg-type]
        assert other.read_bytes(final) == b"secret config"

    def test_recovery_key_typo_is_caught(self, keystore_crypto) -> None:
        _, _, recovery = keystore_crypto
        typo = recovery[:-1] + ("A" if recovery[-1] != "A" else "B")
        with pytest.raises(BackupCryptoError, match="not valid"):
            bc.parse_recovery_key(typo)

    def test_recovery_key_accepts_spaces_and_lowercase(self, keystore_crypto) -> None:
        crypto, _, recovery = keystore_crypto
        relaxed = recovery.lower().replace("-", " ")
        assert bc.key_id(bc.parse_recovery_key(relaxed)).hex() == crypto._policy.key_id

    def test_old_files_stay_readable_after_a_new_key(self, keystore_crypto, tmp_path) -> None:
        crypto, _, _ = keystore_crypto
        old = crypto.write_bytes(tmp_path / "old", b"before")
        crypto.setup_keystore()
        new = crypto.write_bytes(tmp_path / "new", b"after")
        assert crypto.read_bytes(old) == b"before"
        assert crypto.read_bytes(new) == b"after"

    def test_recovery_key_can_be_shown_again(self, keystore_crypto) -> None:
        crypto, _, recovery = keystore_crypto
        assert crypto.recovery_key() == recovery


class TestPassphraseMode:
    def test_round_trip_and_other_machine(self, tmp_path: Path) -> None:
        crypto = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
        crypto.setup_passphrase("correct horse battery")
        final = crypto.write_bytes(tmp_path / "f", b"config")
        assert crypto.read_bytes(final) == b"config"

        elsewhere = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
        with pytest.raises(MissingKeyError, match="--passphrase"):
            elsewhere.read_bytes(final)
        right = BackupCrypto(EncryptionPolicy(), _Keystore(), passphrase="correct horse battery")  # type: ignore[arg-type]
        assert right.read_bytes(final) == b"config"
        wrong = BackupCrypto(EncryptionPolicy(), _Keystore(), passphrase="wrong horse battery")  # type: ignore[arg-type]
        with pytest.raises(BackupCryptoError, match="Wrong passphrase"):
            wrong.read_bytes(final)

    def test_short_passphrase_is_refused(self) -> None:
        crypto = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
        with pytest.raises(BackupCryptoError, match="at least"):
            crypto.setup_passphrase("short")


def test_off_mode_writes_plain_files(tmp_path: Path) -> None:
    crypto = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
    final = crypto.write_bytes(tmp_path / "f.xml", b"<pfsense/>")
    assert final.name == "f.xml" and final.read_bytes() == b"<pfsense/>"


def test_no_persistent_keystore_refuses_setup() -> None:
    store = _Keystore()
    store.is_persistent = False  # type: ignore[misc]
    with pytest.raises(BackupCryptoError, match="persistent keystore"):
        BackupCrypto(EncryptionPolicy(), store).setup_keystore()  # type: ignore[arg-type]


def test_streaming_reader_works_with_gzip_and_tar(keystore_crypto, tmp_path: Path) -> None:
    import tarfile

    crypto, _, _ = keystore_crypto
    with crypto.create(tmp_path / "a.tar.gz") as (sink, final):
        with tarfile.open(fileobj=sink, mode="w|gz") as tar:
            info = tarfile.TarInfo("cert.key")
            payload = b"-----BEGIN PRIVATE KEY-----" * 5000
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    with crypto.open_read(final) as raw, tarfile.open(fileobj=raw, mode="r|gz") as tar:
        member = next(iter(tar))
        assert tar.extractfile(member).read() == payload  # type: ignore[union-attr]
    assert b"PRIVATE KEY" not in final.read_bytes()


def _doc_script() -> dict:
    """The standalone decryption script from docs/encryption.md, loaded as code."""
    doc = (Path(__file__).resolve().parents[2] / "docs" / "encryption.md").read_text()
    section = doc.split("### Standalone decryption", 1)[1]
    code = section.split("```python\n", 1)[1].split("```", 1)[0]
    namespace: dict = {"__name__": "pfs_decrypt"}
    exec(compile(code, "pfs_decrypt.py", "exec"), namespace)  # noqa: S102 - our own docs
    return namespace


@pytest.mark.parametrize("mode", ["keystore", "passphrase"])
def test_the_documented_standalone_script_decrypts(tmp_path: Path, mode: str) -> None:
    """The docs script is the last way back if pfSentinel itself is gone: keep it working."""
    crypto = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
    if mode == "keystore":
        secret = crypto.setup_keystore()
    else:
        secret = "correct horse battery"
        crypto.setup_passphrase(secret)
    data = b"<pfsense>" + bytes(3 * bc.CHUNK) + b"</pfsense>"
    enc = crypto.write_bytes(tmp_path / "c.xml", data)

    _doc_script()["decrypt"](str(enc), str(tmp_path / "out.xml"), secret)
    assert (tmp_path / "out.xml").read_bytes() == data


# --- age mode -------------------------------------------------------------------


@pytest.fixture
def age_crypto() -> tuple[BackupCrypto, _Keystore, str]:
    store = _Keystore()
    crypto = BackupCrypto(EncryptionPolicy(), store)  # type: ignore[arg-type]
    identity = crypto.setup_age([])
    return crypto, store, identity


class TestAgeMode:
    def test_setup_stores_the_identity_and_only_public_keys_in_config(self, age_crypto) -> None:
        crypto, store, identity = age_crypto
        policy = crypto.policy
        assert identity.startswith("AGE-SECRET-KEY-1")
        assert policy.mode == "age" and policy.age_key_ids == [policy.key_id]
        assert policy.age_recipient == age_format.Identity.parse(identity).recipient
        assert store.secrets == {f"__backup_secret__{policy.key_id}": "a:" + identity}
        assert identity not in policy.model_dump_json()

    @pytest.mark.parametrize("size", [0, 1, bc.CHUNK, 3 * bc.CHUNK + 5])
    def test_files_are_age_v1_and_read_back(self, age_crypto, tmp_path: Path, size: int) -> None:
        crypto, _, _ = age_crypto
        data = bytes(range(256)) * (size // 256) + bytes(size % 256)
        final = crypto.write_bytes(tmp_path / "f.xml.gz", data)
        assert final.name == "f.xml.gz.enc"
        assert final.read_bytes().startswith(b"age-encryption.org/v1\n-> X25519 ")
        assert crypto.read_bytes(final) == data

    def test_users_own_key_opens_files_on_another_machine(self, tmp_path: Path) -> None:
        mine = age_format.Identity.generate()
        crypto = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
        crypto.setup_age([mine.recipient, mine.recipient])
        assert crypto.policy.age_recipients == [mine.recipient]
        final = crypto.write_bytes(tmp_path / "f", b"secret config")
        assert final.read_bytes().count(b"\n-> X25519 ") == 2

        elsewhere = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
        with pytest.raises(MissingKeyError, match="--identity"):
            elsewhere.read_bytes(final)
        other = BackupCrypto(EncryptionPolicy(), _Keystore(), identities=[str(mine)])  # type: ignore[arg-type]
        assert other.read_bytes(final) == b"secret config"

    def test_pfsentinels_identity_opens_files_on_another_machine(
        self, age_crypto, tmp_path: Path
    ) -> None:
        crypto, _, identity = age_crypto
        final = crypto.write_bytes(tmp_path / "f", b"secret config")
        other = BackupCrypto(EncryptionPolicy(), _Keystore(), identities=[identity])  # type: ignore[arg-type]
        assert other.read_bytes(final) == b"secret config"

    def test_wrong_identity_is_refused(self, age_crypto, tmp_path: Path) -> None:
        crypto, _, _ = age_crypto
        final = crypto.write_bytes(tmp_path / "f", b"secret config")
        stranger = str(age_format.Identity.generate())
        other = BackupCrypto(EncryptionPolicy(), _Keystore(), identities=[stranger])  # type: ignore[arg-type]
        with pytest.raises(MissingKeyError, match="No age identity"):
            other.read_bytes(final)

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(lambda b: b.__setitem__(-20, b[-20] ^ 1), id="payload"),
            pytest.param(lambda b: b.__delitem__(slice(-10, None)), id="cut-end"),
            pytest.param(lambda b: b.extend(b"\x00"), id="appended"),
            pytest.param(lambda b: b.__setitem__(slice(0, 3), b"AGE"), id="magic"),
        ],
    )
    def test_any_change_fails(self, age_crypto, tmp_path: Path, edit) -> None:
        crypto, _, _ = age_crypto
        final = crypto.write_bytes(tmp_path / "f", b"x" * (2 * bc.CHUNK + 5))
        _tamper(final, edit)
        with pytest.raises(BackupCryptoError):
            crypto.read_bytes(final)

    def test_a_failed_write_leaves_no_file(self, age_crypto, tmp_path: Path) -> None:
        crypto, _, _ = age_crypto
        with pytest.raises(RuntimeError), crypto.create(tmp_path / "f") as (sink, final):
            sink.write(b"partial")
            raise RuntimeError("connection lost")
        assert not final.exists()

    def test_bad_recipient_is_refused_at_setup(self) -> None:
        crypto = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
        with pytest.raises(BackupCryptoError, match="age recipient"):
            crypto.setup_age(["age1notavalidkey"])
        assert crypto.policy.mode == "off"

    def test_bad_identity_input_is_refused(self) -> None:
        with pytest.raises(BackupCryptoError, match="age identity"):
            BackupCrypto(EncryptionPolicy(), _Keystore(), identities=["AGE-SECRET-KEY-1XYZ"])  # type: ignore[arg-type]

    def test_identity_can_be_shown_again(self, age_crypto) -> None:
        crypto, _, identity = age_crypto
        assert crypto.recovery_key() == identity

    def test_all_modes_stay_readable_across_key_changes(self, tmp_path: Path) -> None:
        crypto = BackupCrypto(EncryptionPolicy(), _Keystore())  # type: ignore[arg-type]
        crypto.setup_keystore()
        native = crypto.write_bytes(tmp_path / "native", b"keystore era")
        crypto.setup_age([])
        first_age = crypto.write_bytes(tmp_path / "age1", b"first age key")
        crypto.setup_age([])
        second_age = crypto.write_bytes(tmp_path / "age2", b"second age key")
        crypto.setup_passphrase("correct horse battery")
        last = crypto.write_bytes(tmp_path / "pass", b"passphrase era")
        assert len(crypto.policy.age_key_ids) == 2
        assert crypto.read_bytes(native) == b"keystore era"
        assert crypto.read_bytes(first_age) == b"first age key"
        assert crypto.read_bytes(second_age) == b"second age key"
        assert crypto.read_bytes(last) == b"passphrase era"
        assert native.read_bytes().startswith(b"PFSENC")

    def test_streaming_with_gzip_and_tar(self, age_crypto, tmp_path: Path) -> None:
        import tarfile

        crypto, _, _ = age_crypto
        payload = b"-----BEGIN PRIVATE KEY-----" * 5000
        with crypto.create(tmp_path / "a.tar.gz") as (sink, final):
            with tarfile.open(fileobj=sink, mode="w|gz") as tar:
                info = tarfile.TarInfo("cert.key")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        with crypto.open_read(final) as raw, tarfile.open(fileobj=raw, mode="r|gz") as tar:
            member = next(iter(tar))
            assert tar.extractfile(member).read() == payload  # type: ignore[union-attr]


def test_configs_from_before_age_mode_still_load() -> None:
    old = {"mode": "keystore", "key_id": "00" * 8, "salt": None}
    policy = EncryptionPolicy.model_validate(old)
    assert policy.age_recipients == [] and policy.age_key_ids == []
    assert policy.age_recipient is None


def test_a_non_encrypted_file_named_enc_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.enc"
    path.write_bytes(b"not encrypted at all, just a name" * 3)
    with pytest.raises(BackupCryptoError, match="Not a pfSentinel encrypted file"):
        BackupCrypto(EncryptionPolicy(), _Keystore()).read_bytes(path)  # type: ignore[arg-type]
