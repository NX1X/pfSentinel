"""Tests for credential storage service."""

from __future__ import annotations

from unittest.mock import patch

from pfsentinel.services.credentials import CredentialService


class TestStoreAndGet:
    def test_store_and_retrieve(self):
        svc = CredentialService()
        svc.store("fw1", "secret123")
        assert svc.get("fw1") == "secret123"

    def test_get_missing_returns_none(self):
        svc = CredentialService()
        assert svc.get("nonexistent") is None

    def test_overwrite(self):
        svc = CredentialService()
        svc.store("fw1", "old")
        svc.store("fw1", "new")
        assert svc.get("fw1") == "new"


class TestDelete:
    def test_delete_removes(self):
        svc = CredentialService()
        svc.store("fw1", "pass")
        svc.delete("fw1")
        assert svc.get("fw1") is None

    def test_delete_nonexistent_no_error(self):
        svc = CredentialService()
        svc.delete("missing")  # should not raise


class TestHasPassword:
    def test_true_when_stored(self):
        svc = CredentialService()
        svc.store("fw1", "pass")
        assert svc.has_password("fw1") is True

    def test_false_when_missing(self):
        svc = CredentialService()
        assert svc.has_password("fw1") is False


class TestTelegramToken:
    def test_store_and_get(self):
        svc = CredentialService()
        svc.store_telegram_token("tok123")
        assert svc.get_telegram_token() == "tok123"

    def test_get_none_when_not_set(self):
        svc = CredentialService()
        assert svc.get_telegram_token() is None


class TestSlackWebhook:
    def test_store_and_get(self):
        svc = CredentialService()
        svc.store_slack_webhook("https://hooks.slack.com/xxx")
        assert svc.get_slack_webhook() == "https://hooks.slack.com/xxx"

    def test_get_none_when_not_set(self):
        svc = CredentialService()
        assert svc.get_slack_webhook() is None


class TestSshKeyPassphrase:
    def test_store_and_get(self):
        svc = CredentialService()
        svc.store_ssh_key_passphrase("fw1", "mypass")
        assert svc.get_ssh_key_passphrase("fw1") == "mypass"

    def test_get_none_when_not_set(self):
        svc = CredentialService()
        assert svc.get_ssh_key_passphrase("fw1") is None

    def test_isolated_per_device(self):
        svc = CredentialService()
        svc.store_ssh_key_passphrase("fw1", "pass1")
        svc.store_ssh_key_passphrase("fw2", "pass2")
        assert svc.get_ssh_key_passphrase("fw1") == "pass1"
        assert svc.get_ssh_key_passphrase("fw2") == "pass2"


class TestBackend:
    def test_is_persistent_with_keyring(self):
        svc = CredentialService()
        assert svc.is_persistent is True

    def test_backend_name_with_keyring(self):
        svc = CredentialService()
        name = svc.backend_name()
        assert "fail" not in name.lower()
        assert "in-memory" not in name.lower()

    def test_still_persistent_without_keyring_via_file_store(self):
        """No OS keystore falls back to the encrypted file store, not memory.

        This is the point of secret_store.py: headless hosts (WSL, containers)
        must keep credentials across process exits so scheduled backups work.
        """
        with patch("pfsentinel.services.credentials._KEYRING_AVAILABLE", False):
            svc = CredentialService()
            assert svc.is_persistent is True
            assert "encrypted file store" in svc.backend_name()

    def test_credentials_survive_a_new_service_instance(self):
        """A password stored without a keyring must be readable by a fresh process."""
        with patch("pfsentinel.services.credentials._KEYRING_AVAILABLE", False):
            CredentialService().store("home-fw", "hunter2")
            assert CredentialService().get("home-fw") == "hunter2"

    def test_falls_back_to_memory_when_store_unwritable(self):
        """With neither a keyring nor a writable store, degrade to in-memory."""
        with (
            patch("pfsentinel.services.credentials._KEYRING_AVAILABLE", False),
            patch(
                "pfsentinel.services.secret_store.EncryptedFileStore.is_available",
                new_callable=lambda: property(lambda self: False),
            ),
        ):
            svc = CredentialService()
            assert svc.is_persistent is False
            assert "in-memory" in svc.backend_name()
            svc.store("home-fw", "hunter2")
            assert svc.get("home-fw") == "hunter2"  # memory still works
