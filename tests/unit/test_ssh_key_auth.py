"""Tests for SSH key authentication: SSHConnector, CredentialService, ConnectionManager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.device import ConnectionMethod, DeviceConfig
from pfsentinel.services.connection import (
    AuthenticationError,
    ConnectionError,
    ConnectionManager,
    SSHConnector,
)
from pfsentinel.services.credentials import CredentialService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def device_password():
    return DeviceConfig(id="fw1", label="FW", host="192.168.1.1")


@pytest.fixture
def device_key(tmp_path: Path):
    key_file = tmp_path / "id_rsa"
    key_file.write_text("FAKE KEY CONTENT")
    return DeviceConfig(id="fw1", label="FW", host="192.168.1.1", ssh_key_path=key_file)


# ---------------------------------------------------------------------------
# SSHConnector — password auth
# ---------------------------------------------------------------------------


class TestSSHConnectorPasswordAuth:
    def test_connect_uses_password_kwarg(self, device_password):
        connector = SSHConnector(device_password, password="secret")
        mock_client = MagicMock()
        with patch.object(connector, "_get_client", return_value=mock_client):
            connector.connect()

        kwargs = mock_client.connect.call_args.kwargs
        assert kwargs["password"] == "secret"
        assert "key_filename" not in kwargs

    def test_connect_sets_look_for_keys_false(self, device_password):
        connector = SSHConnector(device_password, password="secret")
        mock_client = MagicMock()
        with patch.object(connector, "_get_client", return_value=mock_client):
            connector.connect()

        kwargs = mock_client.connect.call_args.kwargs
        assert kwargs["look_for_keys"] is False
        assert kwargs["allow_agent"] is False

    def test_auth_failure_raises_authentication_error(self, device_password):
        import paramiko

        connector = SSHConnector(device_password, password="wrong")
        mock_client = MagicMock()
        mock_client.connect.side_effect = paramiko.AuthenticationException("bad creds")
        with patch.object(connector, "_get_client", return_value=mock_client):
            with pytest.raises(AuthenticationError, match="authentication failed"):
                connector.connect()

    def test_network_error_raises_connection_error(self, device_password):
        connector = SSHConnector(device_password, password="pass")
        mock_client = MagicMock()
        mock_client.connect.side_effect = TimeoutError("timed out")
        with patch.object(connector, "_get_client", return_value=mock_client):
            with pytest.raises(ConnectionError, match="connection failed"):
                connector.connect()


# ---------------------------------------------------------------------------
# SSHConnector — key-based auth
# ---------------------------------------------------------------------------


class TestSSHConnectorKeyAuth:
    def test_connect_uses_key_filename(self, device_key):
        connector = SSHConnector(device_key, password=None)
        mock_client = MagicMock()
        with patch.object(connector, "_get_client", return_value=mock_client):
            connector.connect()

        kwargs = mock_client.connect.call_args.kwargs
        assert "key_filename" in kwargs
        assert kwargs["key_filename"] == str(device_key.ssh_key_path)

    def test_connect_does_not_pass_password_for_key_auth(self, device_key):
        connector = SSHConnector(device_key, password=None)
        mock_client = MagicMock()
        with patch.object(connector, "_get_client", return_value=mock_client):
            connector.connect()

        kwargs = mock_client.connect.call_args.kwargs
        assert "password" not in kwargs

    def test_connect_passes_passphrase_when_provided(self, device_key):
        connector = SSHConnector(device_key, password=None, ssh_key_passphrase="mypass")
        mock_client = MagicMock()
        with patch.object(connector, "_get_client", return_value=mock_client):
            connector.connect()

        kwargs = mock_client.connect.call_args.kwargs
        assert kwargs["passphrase"] == "mypass"

    def test_connect_passes_none_passphrase_when_not_set(self, device_key):
        connector = SSHConnector(device_key, password=None, ssh_key_passphrase=None)
        mock_client = MagicMock()
        with patch.object(connector, "_get_client", return_value=mock_client):
            connector.connect()

        kwargs = mock_client.connect.call_args.kwargs
        assert kwargs["passphrase"] is None

    def test_key_auth_ignores_password_even_if_provided(self, device_key):
        """When ssh_key_path is set, password should not be used for SSH connect."""
        connector = SSHConnector(device_key, password="ignored_password")
        mock_client = MagicMock()
        with patch.object(connector, "_get_client", return_value=mock_client):
            connector.connect()

        kwargs = mock_client.connect.call_args.kwargs
        assert "key_filename" in kwargs
        assert "password" not in kwargs


# ---------------------------------------------------------------------------
# CredentialService — SSH key passphrase storage
# ---------------------------------------------------------------------------


class TestCredentialServiceSSHKeyPassphrase:
    def test_store_and_retrieve_passphrase(self):
        creds = CredentialService()
        creds.store_ssh_key_passphrase("fw1", "mysecretpass")
        assert creds.get_ssh_key_passphrase("fw1") == "mysecretpass"

    def test_missing_passphrase_returns_none(self):
        creds = CredentialService()
        assert creds.get_ssh_key_passphrase("nonexistent") is None

    def test_passphrase_isolated_per_device(self):
        creds = CredentialService()
        creds.store_ssh_key_passphrase("fw1", "pass1")
        creds.store_ssh_key_passphrase("fw2", "pass2")
        assert creds.get_ssh_key_passphrase("fw1") == "pass1"
        assert creds.get_ssh_key_passphrase("fw2") == "pass2"

    def test_passphrase_isolated_from_device_password(self):
        creds = CredentialService()
        creds.store("fw1", "device_password")
        creds.store_ssh_key_passphrase("fw1", "key_passphrase")
        assert creds.get("fw1") == "device_password"
        assert creds.get_ssh_key_passphrase("fw1") == "key_passphrase"


# ---------------------------------------------------------------------------
# ConnectionManager — password / key handling
# ---------------------------------------------------------------------------


class TestConnectionManagerCredentials:
    def test_get_password_returns_none_for_key_device(self, device_key):
        """_get_password returns None (not raise) when SSH key is configured."""
        creds = CredentialService()
        # No password stored - key handles auth
        cm = ConnectionManager(device_key, creds)
        result = cm._get_password()
        assert result is None

    def test_get_password_returns_value_when_stored(self, device_password):
        creds = CredentialService()
        creds.store("fw1", "mypass")
        cm = ConnectionManager(device_password, creds)
        assert cm._get_password() == "mypass"

    def test_get_password_raises_without_key_or_password(self, device_password):
        creds = CredentialService()
        cm = ConnectionManager(device_password, creds)
        with pytest.raises(ConnectionError, match="No password"):
            cm._get_password()

    def test_make_ssh_connector_with_key(self, device_key):
        creds = CredentialService()
        cm = ConnectionManager(device_key, creds)
        connector = cm._make_connector(ConnectionMethod.SSH, password=None)
        assert isinstance(connector, SSHConnector)

    def test_make_https_connector_without_password_raises(self, device_key):
        """HTTPS requires a password even if SSH key is configured."""
        creds = CredentialService()
        cm = ConnectionManager(device_key, creds)
        with pytest.raises(ConnectionError, match="No password"):
            cm._make_connector(ConnectionMethod.HTTPS, password=None)

    def test_make_http_connector_without_password_raises(self, device_key):
        creds = CredentialService()
        cm = ConnectionManager(device_key, creds)
        with pytest.raises(ConnectionError, match="No password"):
            cm._make_connector(ConnectionMethod.HTTP, password=None)

    def test_download_config_uses_key_auth(self, device_key, sample_xml):
        """Full download_config flow with SSH key authentication."""
        creds = CredentialService()
        # No password stored — key auth
        cm = ConnectionManager(device_key, creds)

        with (
            patch("pfsentinel.services.connection.SSHConnector.connect"),
            patch(
                "pfsentinel.services.connection.SSHConnector.download_config",
                return_value=sample_xml,
            ),
            patch("pfsentinel.services.connection.SSHConnector.disconnect"),
        ):
            xml, method = cm.download_config()

        assert "<pfsense" in xml
        assert method == "ssh"
