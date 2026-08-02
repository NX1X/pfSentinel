"""Shared test fixtures."""

from __future__ import annotations

import gzip
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_keyring():
    """Replace the real keyring with an in-memory dict for every test.

    Prevents tests from storing/reading credentials in the real
    Windows Credential Manager (or any other persistent backend).
    """
    store: dict[tuple[str, str], str] = {}

    def fake_set(service: str, key: str, value: str) -> None:
        store[(service, key)] = value

    def fake_get(service: str, key: str) -> str | None:
        return store.get((service, key))

    def fake_delete(service: str, key: str) -> None:
        store.pop((service, key), None)

    mock_kr = MagicMock()
    mock_kr.set_password = fake_set
    mock_kr.get_password = fake_get
    mock_kr.delete_password = fake_delete
    mock_kr.get_keyring.return_value = MagicMock(__class__=type("FakeKeyring", (), {}))

    with patch("pfsentinel.services.credentials.keyring", mock_kr, create=True):
        with patch("pfsentinel.services.credentials._KEYRING_AVAILABLE", True):
            yield


@pytest.fixture(autouse=True)
def _isolate_secret_store(tmp_path_factory, monkeypatch):
    """Point the encrypted file store at a tmp dir for every test.

    Without this, any test that constructs a CredentialService while the
    keyring is unavailable writes a real vault and key into the developer's
    ~/.pfsentinel/store.
    """
    store_dir = tmp_path_factory.mktemp("secret-store")
    monkeypatch.setattr(
        "pfsentinel.services.secret_store.default_store_dir",
        lambda: store_dir,
    )
    monkeypatch.setattr(
        "pfsentinel.services.credentials.default_store_dir",
        lambda: store_dir,
    )
    yield store_dir


SAMPLE_XML = """<?xml version="1.0"?>
<pfsense version="24.03">
  <system>
    <hostname>home-fw</hostname>
    <domain>localdomain</domain>
    <version>24.03</version>
    <config_version>22.7</config_version>
  </system>
  <interfaces>
    <wan><if>em0</if><ipaddr>dhcp</ipaddr></wan>
    <lan><if>em1</if><ipaddr>192.168.1.1</ipaddr><subnet>24</subnet></lan>
  </interfaces>
  <filter>
    <rule>
      <type>pass</type>
      <interface>wan</interface>
      <source><any/></source>
      <destination><any/></destination>
      <descr>Default allow</descr>
    </rule>
  </filter>
  <dhcpd>
    <lan>
      <enable/>
      <range><from>192.168.1.100</from><to>192.168.1.200</to></range>
    </lan>
  </dhcpd>
  <installedpackages>
    <package><name>pfBlockerNG</name></package>
  </installedpackages>
</pfsense>
"""

SAMPLE_XML_MODIFIED = """<?xml version="1.0"?>
<pfsense version="24.03">
  <system>
    <hostname>home-fw</hostname>
    <domain>localdomain</domain>
    <version>24.03</version>
  </system>
  <interfaces>
    <wan><if>em0</if><ipaddr>dhcp</ipaddr></wan>
    <lan><if>em1</if><ipaddr>192.168.1.1</ipaddr><subnet>24</subnet></lan>
    <opt1><if>em2</if><descr>DMZ</descr></opt1>
  </interfaces>
  <filter>
    <rule>
      <type>pass</type>
      <interface>wan</interface>
      <descr>Default allow</descr>
    </rule>
  </filter>
</pfsense>
"""


@pytest.fixture
def tmp_backup_dir(tmp_path: Path) -> Path:
    d = tmp_path / "backups"
    d.mkdir()
    return d


@pytest.fixture
def sample_xml() -> str:
    return SAMPLE_XML


@pytest.fixture
def sample_xml_modified() -> str:
    return SAMPLE_XML_MODIFIED


@pytest.fixture
def sample_xml_gz(tmp_path: Path) -> Path:
    path = tmp_path / "config.xml.gz"
    with gzip.open(path, "wb") as f:
        f.write(SAMPLE_XML.encode("utf-8"))
    return path


@pytest.fixture
def mock_ssh_client() -> MagicMock:
    client = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    stderr = MagicMock()
    stderr.read.return_value = b""
    client.exec_command.return_value = (MagicMock(), stdout, stderr)
    return client
