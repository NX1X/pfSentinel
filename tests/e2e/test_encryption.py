"""Encryption at rest, end to end through the CLI and the fake SSH server."""

from __future__ import annotations

import gzip
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.services.retention import RetentionService

from .fake_ssh_server import FakeSSHServer

pytestmark = pytest.mark.e2e

RUN = ["backup", "run", "-d", "home-fw", "--include", "dhcp", "--no-notify"]
RECOVERY = re.compile(r"PFSK(?:-[A-Z2-7]{4})+")


def _output(result) -> str:
    return (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def with_leases(ssh_server: FakeSSHServer) -> FakeSSHServer:
    ssh_server.set_file("/var/dhcpd/var/db/dhcpd.leases", b"lease 192.0.2.50 { secret-lease }\n")
    return ssh_server


@pytest.fixture
def encrypted(cli_runner: CliRunner, seeded_config: AppConfig, monkeypatch) -> str:
    """Keystore encryption set up; returns the recovery key."""
    import typer

    answers: list[str] = []
    real_prompt = typer.prompt

    def prompt(text, *args, **kwargs):
        if text.startswith("Type the last group"):
            return answers[-1]
        return real_prompt(text, *args, **kwargs)

    from pfsentinel.services import backup_crypto

    real_setup = backup_crypto.BackupCrypto.setup_keystore

    def setup(self):
        key = real_setup(self)
        answers.append(key.rsplit("-", 1)[-1])
        return key

    monkeypatch.setattr(backup_crypto.BackupCrypto, "setup_keystore", setup)
    monkeypatch.setattr("pfsentinel.cli.commands.encryption.typer.prompt", prompt)
    result = cli_runner.invoke(app, ["encryption", "setup", "--mode", "keystore"])
    assert result.exit_code == 0, _output(result)
    key = RECOVERY.search(result.stdout)
    assert key, result.stdout
    return key.group(0)


def _records(config: AppConfig):
    loaded = AppConfig.load()
    root = loaded.backup_policy.resolved_root
    return RetentionService(root, loaded.backup_policy).load_index("home-fw").records


def test_encrypted_run_leaves_no_plain_bytes_on_disk(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, encrypted: str, sample_xml: str
) -> None:
    result = cli_runner.invoke(app, RUN)
    assert result.exit_code == 0, _output(result)

    records = _records(seeded_config)
    assert records and all(r.encrypted and r.filename.endswith(".enc") for r in records)
    root = seeded_config.backup_policy.resolved_root / "home-fw"
    gz_magic = b"\x1f\x8b"
    for path in root.rglob("*.enc"):
        raw = path.read_bytes()
        assert raw.startswith(b"PFSENC")
        assert gz_magic not in raw[:64] and b"<pfsense" not in raw and b"secret-lease" not in raw
    assert not [p for p in root.rglob("*") if p.is_file() and p.suffix in {".gz", ".xml"}]


def test_verify_diff_and_restore_read_encrypted_backups(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, encrypted: str, tmp_path: Path
) -> None:
    for _ in range(2):
        assert cli_runner.invoke(app, RUN).exit_code == 0
    configs = sorted(
        (r for r in _records(seeded_config) if r.backup_type.value == "config"),
        key=lambda r: r.created_at,
    )

    for record in _records(seeded_config):
        verify = cli_runner.invoke(app, ["backup", "verify", record.filename])
        assert verify.exit_code == 0, _output(verify)

    diff = cli_runner.invoke(app, ["backup", "diff", configs[0].filename, configs[1].filename])
    assert diff.exit_code == 0, _output(diff)

    target = tmp_path / "restored.xml"
    restore = cli_runner.invoke(
        app, ["backup", "restore", configs[0].filename, "--target", str(target)]
    )
    assert restore.exit_code == 0, _output(restore)
    assert target.read_text().lstrip().startswith("<?xml")


def test_decrypt_on_another_pc_with_the_recovery_key(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, encrypted: str, tmp_path: Path
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    record = next(r for r in _records(seeded_config) if r.backup_type.value == "config")
    enc = seeded_config.backup_policy.resolved_root / "home-fw" / record.relative_path
    copy = tmp_path / "usb" / enc.name
    copy.parent.mkdir()
    copy.write_bytes(enc.read_bytes())

    # "Another PC": an empty keystore.
    from pfsentinel.services import credentials

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(credentials.CredentialService, "get_secret", lambda self, name: None)
        no_key = cli_runner.invoke(app, ["backup", "decrypt", str(copy)])
        assert no_key.exit_code != 0
        assert "--recovery-key" in _output(no_key)

        result = cli_runner.invoke(
            app,
            ["backup", "decrypt", str(copy), "--recovery-key", "--decompress"],
            input=encrypted + "\n",
        )
    assert result.exit_code == 0, _output(result)
    plain = copy.with_name(copy.name.removesuffix(".gz.enc"))
    assert plain.read_text().lstrip().startswith("<?xml")


def test_decrypt_of_a_cut_file_leaves_no_partial_copy(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, encrypted: str, tmp_path: Path
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    record = next(r for r in _records(seeded_config) if r.backup_type.value == "config")
    enc = seeded_config.backup_policy.resolved_root / "home-fw" / record.relative_path
    cut = tmp_path / enc.name
    cut.write_bytes(enc.read_bytes()[:-40])

    result = cli_runner.invoke(app, ["backup", "decrypt", str(cut), "--decompress"])

    assert result.exit_code == 1, _output(result)
    assert "Could not decrypt" in _output(result)
    assert not cut.with_name(cut.name.removesuffix(".gz.enc")).exists()


def test_encrypt_existing_converts_plain_backups(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, monkeypatch
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    root = seeded_config.backup_policy.resolved_root / "home-fw"
    plain_files = [p for p in root.rglob("*.gz")]
    assert plain_files

    # Turn encryption on after the fact.
    from pfsentinel.services.backup_crypto import BackupCrypto
    from pfsentinel.services.credentials import CredentialService

    config = AppConfig.load()
    BackupCrypto(config.backup_policy.encryption, CredentialService()).setup_keystore()
    config.save()

    result = cli_runner.invoke(app, ["encryption", "encrypt-existing", "--yes", "--wipe"])
    assert result.exit_code == 0, _output(result)
    assert "Encrypted 2 backup file(s)" in result.stdout
    assert not [p for p in plain_files if p.exists()], "plain files are deleted"
    assert all(r.encrypted for r in _records(seeded_config))
    for record in _records(seeded_config):
        assert cli_runner.invoke(app, ["backup", "verify", record.filename]).exit_code == 0

    status = cli_runner.invoke(app, ["encryption", "status"])
    assert "2 encrypted, 0 plain" in status.stdout


AGE_IDENTITY = re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{58}")


def test_age_mode_end_to_end(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases, tmp_path: Path
) -> None:
    """Setup with the user's own key, back up, read back, and open on another PC."""
    from pfsentinel.services import age_format, credentials

    mine = age_format.Identity.generate()
    setup = cli_runner.invoke(
        app, ["encryption", "setup", "--mode", "age", "--recipient", mine.recipient]
    )
    assert setup.exit_code == 0, _output(setup)
    pfs_identity = AGE_IDENTITY.search(setup.stdout)
    assert pfs_identity, setup.stdout
    policy = AppConfig.load().backup_policy.encryption
    assert policy.mode == "age" and policy.age_recipients == [mine.recipient]

    for _ in range(2):
        run = cli_runner.invoke(app, RUN)
        assert run.exit_code == 0, _output(run)
    records = _records(seeded_config)
    assert records and all(r.encrypted for r in records)
    root = seeded_config.backup_policy.resolved_root / "home-fw"
    for path in root.rglob("*.enc"):
        raw = path.read_bytes()
        assert raw.startswith(b"age-encryption.org/v1\n")
        assert b"<pfsense" not in raw and b"secret-lease" not in raw
    for record in records:
        verify = cli_runner.invoke(app, ["backup", "verify", record.filename])
        assert verify.exit_code == 0, _output(verify)
    configs = sorted(
        (r for r in records if r.backup_type.value == "config"), key=lambda r: r.created_at
    )
    diff = cli_runner.invoke(app, ["backup", "diff", configs[0].filename, configs[1].filename])
    assert diff.exit_code == 0, _output(diff)
    status = cli_runner.invoke(app, ["encryption", "status"])
    assert "age" in status.stdout and mine.recipient in status.stdout

    enc = root / configs[0].relative_path
    copy = tmp_path / "usb" / enc.name
    copy.parent.mkdir()
    copy.write_bytes(enc.read_bytes())
    key_file = tmp_path / "key.txt"
    key_file.write_text(f"# public key: {mine.recipient}\n{mine}\n")

    with pytest.MonkeyPatch.context() as mp:  # "another PC": an empty keystore
        mp.setattr(credentials.CredentialService, "get_secret", lambda self, name: None)
        no_key = cli_runner.invoke(app, ["backup", "decrypt", str(copy)])
        assert no_key.exit_code != 0 and "--identity" in _output(no_key)

        with_file = cli_runner.invoke(
            app,
            ["backup", "decrypt", str(copy), "--identity-file", str(key_file), "--decompress"],
        )
        assert with_file.exit_code == 0, _output(with_file)
        plain = copy.with_name(copy.name.removesuffix(".gz.enc"))
        assert plain.read_text().lstrip().startswith("<?xml")
        plain.unlink()

        typed = cli_runner.invoke(
            app,
            ["backup", "decrypt", str(copy), "--identity", "--decompress"],
            input=pfs_identity.group(0) + "\n",
        )
        assert typed.exit_code == 0, _output(typed)
        assert plain.read_text().lstrip().startswith("<?xml")


def test_age_setup_without_own_key_asks_to_confirm_the_identity(
    cli_runner: CliRunner, seeded_config: AppConfig, monkeypatch
) -> None:
    from pfsentinel.services import backup_crypto

    shown: list[str] = []
    real_setup = backup_crypto.BackupCrypto.setup_age

    def setup(self, recipients):
        shown.append(real_setup(self, recipients))
        return shown[-1]

    monkeypatch.setattr(backup_crypto.BackupCrypto, "setup_age", setup)
    answers = iter(["", "WRNG"])  # no own key; then a wrong confirmation

    def prompt(text, *args, **kwargs):
        answer = next(answers, None)
        return shown[-1][-4:] if answer is None else answer

    monkeypatch.setattr("pfsentinel.cli.commands.encryption.typer.prompt", prompt)
    result = cli_runner.invoke(app, ["encryption", "setup", "--mode", "age"])
    assert result.exit_code == 0, _output(result)
    assert "does not match" in result.stdout
    assert AppConfig.load().backup_policy.encryption.age_recipients == []


def test_age_setup_rejects_a_bad_recipient(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["encryption", "setup", "--recipient", "age1nope"])
    assert result.exit_code == 1
    assert AppConfig.load().backup_policy.encryption.mode == "off"


def test_status_warns_when_off(cli_runner: CliRunner, seeded_config: AppConfig) -> None:
    result = cli_runner.invoke(app, ["encryption", "status"])
    assert "OFF" in result.stdout


def test_plain_gzip_backups_still_work_when_off(
    cli_runner: CliRunner, seeded_config: AppConfig, with_leases
) -> None:
    assert cli_runner.invoke(app, RUN).exit_code == 0
    records = _records(seeded_config)
    assert records and not any(r.encrypted for r in records)
    root = seeded_config.backup_policy.resolved_root / "home-fw"
    config_file = next(root.rglob("*.xml.gz"))
    assert gzip.decompress(config_file.read_bytes()).lstrip().startswith(b"<?xml")
