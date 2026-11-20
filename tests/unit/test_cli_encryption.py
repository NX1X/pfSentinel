"""CLI tests for `pfs encryption` paths the e2e suite does not reach.

Covered here: the non-persistent keystore refusal, re-keying when
encryption is already on, the interactive mode prompt, passphrase mode,
the recovery-key and disable commands, and the encrypt-existing guards.
The keystore is the in-memory keyring from the root conftest.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from pfsentinel.cli.app import app
from pfsentinel.models.config import AppConfig
from pfsentinel.services.backup_crypto import MIN_PASSPHRASE, BackupCrypto
from pfsentinel.services.credentials import CredentialService

RECOVERY = re.compile(r"PFSK(?:-[A-Z2-7]{4})+")
PASSPHRASE = "correct horse battery staple"


def _out(result) -> str:
    return " ".join(((result.stdout or "") + (result.stderr or "")).split())


@pytest.fixture
def recovery_answers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer the 'type the last group' prompt.

    The recovery key is random, so the answer is only known after
    setup_keystore ran. Tests push extra (wrong) answers first if they want
    to see the mismatch path; the real last group is appended last.
    """
    import typer

    answers: list[str] = []
    real_prompt = typer.prompt
    real_setup = BackupCrypto.setup_keystore

    def setup(self):
        key = real_setup(self)
        answers.append(key.rsplit("-", 1)[-1])
        return key

    def prompt(text, *args, **kwargs):
        if text.startswith("Type the last group"):
            return answers.pop(0)
        return real_prompt(text, *args, **kwargs)

    monkeypatch.setattr(BackupCrypto, "setup_keystore", setup)
    monkeypatch.setattr("pfsentinel.cli.commands.encryption.typer.prompt", prompt)
    return answers


@pytest.fixture
def no_persistent_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OS keyring and no writable encrypted file store on this machine."""

    class _UnavailableStore:
        is_available = False

    monkeypatch.setattr("pfsentinel.services.credentials._KEYRING_AVAILABLE", False)
    monkeypatch.setattr("pfsentinel.services.credentials.EncryptedFileStore", _UnavailableStore)


def _turn_on_passphrase() -> None:
    config = AppConfig.load()
    BackupCrypto(config.backup_policy.encryption, CredentialService()).setup_passphrase(PASSPHRASE)
    config.save()


def _turn_on_keystore() -> str:
    config = AppConfig.load()
    key = BackupCrypto(config.backup_policy.encryption, CredentialService()).setup_keystore()
    config.save()
    return key


class TestSetup:
    def test_refuses_without_a_persistent_keystore(
        self, cli_runner: CliRunner, saved_config: AppConfig, no_persistent_store: None
    ) -> None:
        result = cli_runner.invoke(app, ["encryption", "setup", "--mode", "keystore"])

        assert result.exit_code == 1
        assert "persistent keystore" in _out(result)
        assert AppConfig.load().backup_policy.encryption.mode == "off"

    def test_passphrase_mode_turns_encryption_on(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        result = cli_runner.invoke(
            app,
            ["encryption", "setup", "--mode", "passphrase"],
            input=f"{PASSPHRASE}\n{PASSPHRASE}\n",
        )

        assert result.exit_code == 0, _out(result)
        policy = AppConfig.load().backup_policy.encryption
        assert policy.mode == "passphrase"
        assert policy.salt

    def test_passphrase_mode_asks_again_when_too_short(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        short = "x" * (MIN_PASSPHRASE - 1)
        result = cli_runner.invoke(
            app,
            ["encryption", "setup", "--mode", "passphrase"],
            input=f"{short}\n{short}\n{PASSPHRASE}\n{PASSPHRASE}\n",
        )

        assert result.exit_code == 0, _out(result)
        assert f"at least {MIN_PASSPHRASE} characters" in _out(result)
        assert AppConfig.load().backup_policy.encryption.mode == "passphrase"

    def test_unknown_mode_is_refused(self, cli_runner: CliRunner, saved_config: AppConfig) -> None:
        result = cli_runner.invoke(app, ["encryption", "setup", "--mode", "rot13"])

        assert result.exit_code == 1
        assert "keystore or passphrase" in _out(result)
        assert AppConfig.load().backup_policy.encryption.mode == "off"

    def test_mode_is_prompted_when_not_given(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        result = cli_runner.invoke(
            app, ["encryption", "setup"], input=f"passphrase\n{PASSPHRASE}\n{PASSPHRASE}\n"
        )

        assert result.exit_code == 0, _out(result)
        assert "keystore" in _out(result)
        assert AppConfig.load().backup_policy.encryption.mode == "passphrase"

    def test_keystore_setup_repeats_until_the_recovery_key_is_confirmed(
        self,
        cli_runner: CliRunner,
        saved_config: AppConfig,
        recovery_answers: list[str],
    ) -> None:
        recovery_answers.append("NOPE")
        result = cli_runner.invoke(app, ["encryption", "setup", "--mode", "keystore"])

        assert result.exit_code == 0, _out(result)
        assert "does not match" in _out(result)
        assert AppConfig.load().backup_policy.encryption.mode == "keystore"

    def test_already_on_and_declined_keeps_the_old_key(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _turn_on_passphrase()
        before = AppConfig.load().backup_policy.encryption.key_id
        result = cli_runner.invoke(app, ["encryption", "setup"], input="n\n")

        assert result.exit_code == 0
        assert "already on" in _out(result)
        assert AppConfig.load().backup_policy.encryption.key_id == before

    def test_already_on_and_confirmed_sets_a_new_key(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _turn_on_passphrase()
        before = AppConfig.load().backup_policy.encryption.key_id
        new = "another long passphrase here"
        result = cli_runner.invoke(
            app,
            ["encryption", "setup", "--mode", "passphrase"],
            input=f"y\n{new}\n{new}\n",
        )

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().backup_policy.encryption.key_id != before


class TestRecoveryKey:
    def test_shows_the_key_after_confirmation(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        key = _turn_on_keystore()
        result = cli_runner.invoke(app, ["encryption", "recovery-key"], input="y\n")

        assert result.exit_code == 0, _out(result)
        assert key in result.stdout

    def test_declining_shows_nothing(self, cli_runner: CliRunner, saved_config: AppConfig) -> None:
        _turn_on_keystore()
        result = cli_runner.invoke(app, ["encryption", "recovery-key"], input="n\n")

        assert result.exit_code == 0
        assert not RECOVERY.search(result.stdout)

    def test_passphrase_mode_has_no_recovery_key(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _turn_on_passphrase()
        result = cli_runner.invoke(app, ["encryption", "recovery-key"], input="y\n")

        assert result.exit_code == 1
        assert "keystore mode" in _out(result)


class TestDisable:
    def test_yes_flag_turns_encryption_off(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _turn_on_passphrase()
        result = cli_runner.invoke(app, ["encryption", "disable", "--yes"])

        assert result.exit_code == 0, _out(result)
        assert AppConfig.load().backup_policy.encryption.mode == "off"

    def test_declining_the_prompt_keeps_encryption_on(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _turn_on_passphrase()
        result = cli_runner.invoke(app, ["encryption", "disable"], input="n\n")

        assert result.exit_code == 0
        assert AppConfig.load().backup_policy.encryption.mode == "passphrase"

    def test_keys_stay_in_the_keystore(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        key = _turn_on_keystore()
        cli_runner.invoke(app, ["encryption", "disable", "--yes"])

        # Turning the mode back on with the same key ID finds the key again.
        config = AppConfig.load()
        config.backup_policy.encryption.mode = "keystore"
        crypto = BackupCrypto(config.backup_policy.encryption, CredentialService())
        assert crypto.recovery_key() == key


class TestEncryptExisting:
    def test_refuses_when_encryption_is_off(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        result = cli_runner.invoke(app, ["encryption", "encrypt-existing", "--yes"])

        assert result.exit_code == 1
        assert "pfs encryption setup" in _out(result)

    def test_warns_about_recoverable_plain_files_and_can_be_declined(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _turn_on_passphrase()
        result = cli_runner.invoke(app, ["encryption", "encrypt-existing"], input="n\n")

        assert result.exit_code == 0
        assert "--wipe" in _out(result)
        assert "Encrypted" not in _out(result)

    def test_with_wipe_the_recoverable_warning_is_not_shown(
        self, cli_runner: CliRunner, saved_config: AppConfig
    ) -> None:
        _turn_on_passphrase()
        result = cli_runner.invoke(app, ["encryption", "encrypt-existing", "--wipe"], input="y\n")

        assert result.exit_code == 0, _out(result)
        assert "may still be recoverable" not in _out(result)
        assert "Encrypted 0 backup file(s)" in _out(result)
