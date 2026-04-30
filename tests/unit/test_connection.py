"""Tests for connection service — CSRF extraction, login, fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.device import ConnectionMethod, DeviceConfig
from pfsentinel.services.connection import (
    AuthenticationError,
    ConnectionError,
    ConnectionManager,
    HTTPSConnector,
    SSHConnector,
)
from pfsentinel.services.credentials import CredentialService


def _make_device(**overrides):
    defaults = {"id": "fw1", "label": "FW", "host": "10.0.0.1"}
    defaults.update(overrides)
    return DeviceConfig(**defaults)


class TestHTTPSConnectorCsrfExtraction:
    def test_name_first(self):
        connector = HTTPSConnector(_make_device(), "pass")
        html = '<input name="__csrf_magic" value="tok123" />'
        assert connector._extract_csrf_token(html) == "tok123"

    def test_value_first(self):
        connector = HTTPSConnector(_make_device(), "pass")
        html = '<input value="tok456" name="__csrf_magic" />'
        assert connector._extract_csrf_token(html) == "tok456"

    def test_missing_returns_none(self):
        connector = HTTPSConnector(_make_device(), "pass")
        html = "<html><body>No CSRF here</body></html>"
        assert connector._extract_csrf_token(html) is None


class TestHTTPSConnectorLogin:
    @patch("pfsentinel.services.connection.HTTPSConnector._make_session")
    def test_login_success(self, mock_session_factory):
        connector = HTTPSConnector(_make_device(), "pass")
        mock_session = MagicMock()
        mock_session_factory.return_value = mock_session

        # GET login page returns CSRF
        get_resp = MagicMock()
        get_resp.text = '<input name="__csrf_magic" value="csrf_tok" />'
        get_resp.raise_for_status = MagicMock()

        # POST login succeeds (no "sign in" in response)
        post_resp = MagicMock()
        post_resp.text = "<html>Dashboard</html>"
        post_resp.raise_for_status = MagicMock()

        mock_session.get.return_value = get_resp
        mock_session.post.return_value = post_resp

        connector._login(mock_session)  # should not raise

    @patch("pfsentinel.services.connection.HTTPSConnector._make_session")
    def test_login_bad_creds(self, mock_session_factory):
        connector = HTTPSConnector(_make_device(), "pass")
        mock_session = MagicMock()
        mock_session_factory.return_value = mock_session

        get_resp = MagicMock()
        get_resp.text = '<input name="__csrf_magic" value="csrf_tok" />'
        get_resp.raise_for_status = MagicMock()

        post_resp = MagicMock()
        post_resp.text = (
            "<html><form>Username<input/>Password<input/>Sign In<button/></form></html>"
        )
        post_resp.raise_for_status = MagicMock()

        mock_session.get.return_value = get_resp
        mock_session.post.return_value = post_resp

        with pytest.raises(AuthenticationError, match="invalid credentials"):
            connector._login(mock_session)

    @patch("pfsentinel.services.connection.HTTPSConnector._make_session")
    def test_login_connection_error(self, mock_session_factory):
        connector = HTTPSConnector(_make_device(), "pass")
        mock_session = MagicMock()
        mock_session_factory.return_value = mock_session
        mock_session.get.side_effect = Exception("Network unreachable")

        with pytest.raises(ConnectionError, match="Cannot reach"):
            connector._login(mock_session)


class TestConnectionManagerFallback:
    def test_primary_succeeds(self):
        device = _make_device()
        creds = CredentialService()
        creds.store("fw1", "pass")
        cm = ConnectionManager(device, creds)

        mock_connector = MagicMock(spec=SSHConnector)
        mock_connector.__enter__ = MagicMock(return_value=mock_connector)
        mock_connector.__exit__ = MagicMock(return_value=False)
        mock_connector.download_config.return_value = "<pfsense/>"

        with patch.object(cm, "_make_connector", return_value=mock_connector):
            xml, method = cm.download_config()
        assert xml == "<pfsense/>"
        assert method == "ssh"

    def test_primary_fails_fallback_succeeds(self):
        device = _make_device(
            primary_method=ConnectionMethod.SSH,
            fallback_method=ConnectionMethod.HTTPS,
        )
        creds = CredentialService()
        creds.store("fw1", "pass")
        cm = ConnectionManager(device, creds)

        mock_ssh = MagicMock(spec=SSHConnector)
        mock_ssh.__enter__ = MagicMock(return_value=mock_ssh)
        mock_ssh.__exit__ = MagicMock(return_value=False)
        mock_ssh.download_config.side_effect = ConnectionError("SSH refused")

        mock_https = MagicMock(spec=HTTPSConnector)
        mock_https.download_config.return_value = "<pfsense/>"

        call_count = 0

        def make_connector(method, password):
            nonlocal call_count
            call_count += 1
            if method == ConnectionMethod.SSH:
                return mock_ssh
            return mock_https

        with patch.object(cm, "_make_connector", side_effect=make_connector):
            xml, method = cm.download_config()
        assert method == "https"

    def test_all_methods_fail(self):
        device = _make_device(
            primary_method=ConnectionMethod.SSH,
            fallback_method=ConnectionMethod.HTTPS,
        )
        creds = CredentialService()
        creds.store("fw1", "pass")
        cm = ConnectionManager(device, creds)

        mock_conn = MagicMock(spec=SSHConnector)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.download_config.side_effect = ConnectionError("fail")

        with (
            patch.object(cm, "_make_connector", return_value=mock_conn),
            pytest.raises(ConnectionError, match="All connection methods failed"),
        ):
            cm.download_config()

    def test_auth_error_no_fallback(self):
        device = _make_device(
            primary_method=ConnectionMethod.SSH,
            fallback_method=ConnectionMethod.HTTPS,
        )
        creds = CredentialService()
        creds.store("fw1", "pass")
        cm = ConnectionManager(device, creds)

        mock_conn = MagicMock(spec=SSHConnector)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.download_config.side_effect = AuthenticationError("bad creds")

        with (
            patch.object(cm, "_make_connector", return_value=mock_conn),
            pytest.raises(AuthenticationError),
        ):
            cm.download_config()
