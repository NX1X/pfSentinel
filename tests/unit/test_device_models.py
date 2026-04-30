"""Tests for device configuration models."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pfsentinel.models.device import ConnectionMethod, DeviceConfig, DeviceStatus


def _make_device(**overrides):
    defaults = {"id": "fw1", "label": "FW", "host": "192.168.1.1"}
    defaults.update(overrides)
    return DeviceConfig(**defaults)


class TestHostValidator:
    def test_empty_host_raises(self):
        with pytest.raises(ValidationError, match="host"):
            _make_device(host="")

    def test_whitespace_host_raises(self):
        with pytest.raises(ValidationError, match="host"):
            _make_device(host="   ")

    def test_host_stripped(self):
        d = _make_device(host="  fw.local  ")
        assert d.host == "fw.local"


class TestIdValidator:
    def test_id_lowercased(self):
        d = DeviceConfig(id="FW1", label="FW", host="10.0.0.1")
        assert d.id == "fw1"


class TestSshKeyPathValidator:
    def test_none_is_valid(self):
        d = _make_device(ssh_key_path=None)
        assert d.ssh_key_path is None

    def test_directory_raises(self, tmp_path: Path):
        with pytest.raises(ValidationError, match="ssh_key_path.*file.*directory"):
            _make_device(ssh_key_path=tmp_path)

    def test_file_is_valid(self, tmp_path: Path):
        key = tmp_path / "id_rsa"
        key.write_text("key-data")
        d = _make_device(ssh_key_path=key)
        assert d.ssh_key_path == key


class TestConnectionUrl:
    def test_ssh_url(self):
        d = _make_device()
        assert d.connection_url(ConnectionMethod.SSH) == "ssh://192.168.1.1:22"

    def test_https_url(self):
        d = _make_device()
        assert d.connection_url(ConnectionMethod.HTTPS) == "https://192.168.1.1:443"

    def test_http_url(self):
        d = _make_device()
        assert d.connection_url(ConnectionMethod.HTTP) == "http://192.168.1.1:80"

    def test_custom_ports(self):
        d = _make_device(ssh_port=2222, https_port=8443, http_port=8080)
        assert d.connection_url(ConnectionMethod.SSH) == "ssh://192.168.1.1:2222"
        assert d.connection_url(ConnectionMethod.HTTPS) == "https://192.168.1.1:8443"
        assert d.connection_url(ConnectionMethod.HTTP) == "http://192.168.1.1:8080"

    def test_default_uses_primary_method(self):
        d = _make_device(primary_method=ConnectionMethod.HTTPS)
        assert d.connection_url() == "https://192.168.1.1:443"


class TestDeviceStatusAnyReachable:
    def test_all_false(self):
        s = DeviceStatus(device_id="fw1")
        assert s.any_reachable is False

    def test_ssh_only(self):
        s = DeviceStatus(device_id="fw1", ssh_reachable=True)
        assert s.any_reachable is True

    def test_https_only(self):
        s = DeviceStatus(device_id="fw1", https_reachable=True)
        assert s.any_reachable is True

    def test_http_only(self):
        s = DeviceStatus(device_id="fw1", http_reachable=True)
        assert s.any_reachable is True


class TestDeviceStatusBestMethod:
    def test_ssh_priority(self):
        s = DeviceStatus(device_id="fw1", ssh_reachable=True, https_reachable=True)
        assert s.best_method == ConnectionMethod.SSH

    def test_https_fallback(self):
        s = DeviceStatus(device_id="fw1", https_reachable=True)
        assert s.best_method == ConnectionMethod.HTTPS

    def test_http_fallback(self):
        s = DeviceStatus(device_id="fw1", http_reachable=True)
        assert s.best_method == ConnectionMethod.HTTP

    def test_none_when_unreachable(self):
        s = DeviceStatus(device_id="fw1")
        assert s.best_method is None
