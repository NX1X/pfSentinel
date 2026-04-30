"""Tests for selective backup: --area and --no-packages (HTTPSConnector)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.device import ConnectionMethod, DeviceConfig
from pfsentinel.services.connection import HTTPSConnector


@pytest.fixture
def device():
    return DeviceConfig(
        id="fw1",
        label="FW",
        host="192.168.1.1",
        primary_method=ConnectionMethod.HTTPS,
        verify_ssl=False,
    )


def _make_connector_with_mocked_session(device, xml_response="<pfsense/>"):
    """Return an HTTPSConnector whose session is fully mocked.

    The mock satisfies: GET /index.php (login page), POST /index.php (login),
    GET /diag_backup.php (backup page), POST /diag_backup.php (download).
    """
    connector = HTTPSConnector(device, password="admin")

    mock_session = MagicMock()
    # GET responses: login page, then backup page (both carry a CSRF token)
    csrf_html = '<input name="__csrf_magic" value="tok123"/>'
    login_get = MagicMock(text=csrf_html, status_code=200)
    backup_get = MagicMock(text=csrf_html, status_code=200)
    mock_session.get.side_effect = [login_get, backup_get]

    # POST responses: login redirect (no "Sign In" text = success), then backup XML
    login_post = MagicMock(text="<html>dashboard</html>", status_code=200)
    backup_post = MagicMock(
        text=xml_response,
        headers={"Content-Type": "text/xml"},
        status_code=200,
    )
    mock_session.post.side_effect = [login_post, backup_post]

    patch.object(connector, "_make_session", return_value=mock_session).start()
    return connector, mock_session


class TestHTTPSConnectorAreaParam:
    def test_full_backup_sends_empty_backuparea(self, device):
        connector, mock_session = _make_connector_with_mocked_session(device)
        connector.download_config(area="")

        backup_post_kwargs = mock_session.post.call_args_list[1].kwargs
        form_data = backup_post_kwargs["data"]
        assert form_data["backuparea"] == ""

    def test_area_interfaces_is_forwarded(self, device):
        connector, mock_session = _make_connector_with_mocked_session(device)
        connector.download_config(area="interfaces")

        form_data = mock_session.post.call_args_list[1].kwargs["data"]
        assert form_data["backuparea"] == "interfaces"

    def test_area_cert_is_forwarded(self, device):
        connector, mock_session = _make_connector_with_mocked_session(device)
        connector.download_config(area="cert")

        form_data = mock_session.post.call_args_list[1].kwargs["data"]
        assert form_data["backuparea"] == "cert"

    def test_area_filter_is_forwarded(self, device):
        connector, mock_session = _make_connector_with_mocked_session(device)
        connector.download_config(area="filter")

        form_data = mock_session.post.call_args_list[1].kwargs["data"]
        assert form_data["backuparea"] == "filter"


class TestHTTPSConnectorNoPackages:
    def test_no_packages_false_sends_empty_string(self, device):
        connector, mock_session = _make_connector_with_mocked_session(device)
        connector.download_config(no_packages=False)

        form_data = mock_session.post.call_args_list[1].kwargs["data"]
        assert form_data["nopackages"] == ""

    def test_no_packages_true_sends_on(self, device):
        connector, mock_session = _make_connector_with_mocked_session(device)
        connector.download_config(no_packages=True)

        form_data = mock_session.post.call_args_list[1].kwargs["data"]
        assert form_data["nopackages"] == "on"

    def test_default_includes_packages(self, device):
        """Default call (no args) should include packages."""
        connector, mock_session = _make_connector_with_mocked_session(device)
        connector.download_config()

        form_data = mock_session.post.call_args_list[1].kwargs["data"]
        assert form_data["nopackages"] == ""

    def test_area_and_no_packages_combined(self, device):
        connector, mock_session = _make_connector_with_mocked_session(device)
        connector.download_config(area="dhcpd", no_packages=True)

        form_data = mock_session.post.call_args_list[1].kwargs["data"]
        assert form_data["backuparea"] == "dhcpd"
        assert form_data["nopackages"] == "on"


class TestHTTPSConnectorBackupAreaPassthrough:
    """Test that ConnectionManager threads area/no_packages to the HTTPS connector."""

    def test_connection_manager_passes_area_to_https(self, device, sample_xml):
        from pfsentinel.services.connection import ConnectionManager
        from pfsentinel.services.credentials import CredentialService

        creds = CredentialService()
        creds.store("fw1", "pass")
        cm = ConnectionManager(device, creds)

        captured = {}

        def mock_download(area="", no_packages=False):
            captured["area"] = area
            captured["no_packages"] = no_packages
            return sample_xml

        with (
            patch("pfsentinel.services.connection.HTTPSConnector._login"),
            patch(
                "pfsentinel.services.connection.HTTPSConnector.download_config",
                side_effect=mock_download,
            ),
        ):
            cm.download_config(area="system", no_packages=True)

        assert captured["area"] == "system"
        assert captured["no_packages"] is True
