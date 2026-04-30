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

    def test_is_persistent_false_without_keyring(self):
        with patch("pfsentinel.services.credentials._KEYRING_AVAILABLE", False):
            svc = CredentialService()
            assert svc.is_persistent is False

    def test_backend_name_without_keyring(self):
        with patch("pfsentinel.services.credentials._KEYRING_AVAILABLE", False):
            svc = CredentialService()
            assert "in-memory" in svc.backend_name()
