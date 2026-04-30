"""Extended tests for application configuration models (load/save/device management)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pfsentinel.models.config import AppConfig, BackupPolicy
from pfsentinel.models.device import DeviceConfig


def _make_device(device_id="fw1", **overrides):
    defaults = {"id": device_id, "label": "FW", "host": "10.0.0.1"}
    defaults.update(overrides)
    return DeviceConfig(**defaults)


class TestBackupPolicyResolvedRoot:
    def test_custom_path(self, tmp_path: Path):
        policy = BackupPolicy(backup_root=tmp_path / "custom")
        assert policy.resolved_root == tmp_path / "custom"

    def test_default_when_none(self):
        policy = BackupPolicy(backup_root=None)
        assert policy.resolved_root == Path.home() / "Documents" / "pfSentinel"

    def test_expanduser(self):
        policy = BackupPolicy(backup_root=Path("~/backups"))
        assert "~" not in str(policy.resolved_root)
        assert "backups" in str(policy.resolved_root)


class TestAppConfigPath:
    def test_config_path_returns_json(self):
        p = AppConfig.config_path()
        assert p.name == "config.json"
        assert ".pfsentinel" in str(p)


class TestAppConfigLoad:
    def test_missing_file_returns_default(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(AppConfig, "config_path", staticmethod(lambda: tmp_path / "nope.json"))
        cfg = AppConfig.load()
        assert isinstance(cfg, AppConfig)
        assert cfg.devices == []

    def test_corrupt_file_returns_default(self, tmp_path: Path, monkeypatch):
        bad = tmp_path / "config.json"
        bad.write_text("{{{not json", encoding="utf-8")
        monkeypatch.setattr(AppConfig, "config_path", staticmethod(lambda: bad))
        cfg = AppConfig.load()
        assert isinstance(cfg, AppConfig)

    def test_valid_file_loads(self, tmp_path: Path, monkeypatch):
        p = tmp_path / "config.json"
        data = AppConfig(log_level="DEBUG").model_dump_json()
        p.write_text(data, encoding="utf-8")
        monkeypatch.setattr(AppConfig, "config_path", staticmethod(lambda: p))
        cfg = AppConfig.load()
        assert cfg.log_level == "DEBUG"


class TestAppConfigSave:
    def test_save_and_reload(self, tmp_path: Path, monkeypatch):
        p = tmp_path / ".pfsentinel" / "config.json"
        monkeypatch.setattr(AppConfig, "config_path", staticmethod(lambda: p))

        cfg = AppConfig(log_level="WARNING")
        cfg.save()

        loaded = AppConfig.load()
        assert loaded.log_level == "WARNING"

    def test_save_creates_parent_dirs(self, tmp_path: Path, monkeypatch):
        p = tmp_path / "deep" / "nested" / "config.json"
        monkeypatch.setattr(AppConfig, "config_path", staticmethod(lambda: p))
        AppConfig().save()
        assert p.exists()

    def test_save_writes_valid_json(self, tmp_path: Path, monkeypatch):
        p = tmp_path / "config.json"
        monkeypatch.setattr(AppConfig, "config_path", staticmethod(lambda: p))
        AppConfig().save()
        data = json.loads(p.read_text(encoding="utf-8"))
        assert "backup_policy" in data


class TestAppConfigDevices:
    def test_add_device(self):
        cfg = AppConfig()
        cfg.add_device(_make_device("fw1"))
        assert len(cfg.devices) == 1

    def test_add_duplicate_raises(self):
        cfg = AppConfig()
        cfg.add_device(_make_device("fw1"))
        with pytest.raises(ValueError, match="already exists"):
            cfg.add_device(_make_device("fw1"))

    def test_remove_device_exists(self):
        cfg = AppConfig()
        cfg.add_device(_make_device("fw1"))
        assert cfg.remove_device("fw1") is True
        assert len(cfg.devices) == 0

    def test_remove_device_missing(self):
        cfg = AppConfig()
        assert cfg.remove_device("nonexistent") is False

    def test_get_device_found(self):
        cfg = AppConfig()
        cfg.add_device(_make_device("fw1"))
        assert cfg.get_device("fw1") is not None
        assert cfg.get_device("fw1").id == "fw1"

    def test_get_device_not_found(self):
        cfg = AppConfig()
        assert cfg.get_device("nonexistent") is None

    def test_enabled_devices_filters(self):
        cfg = AppConfig()
        cfg.add_device(_make_device("fw1", enabled=True))
        cfg.add_device(_make_device("fw2", enabled=False))
        cfg.add_device(_make_device("fw3", enabled=True))
        enabled = cfg.enabled_devices()
        assert len(enabled) == 2
        assert all(d.enabled for d in enabled)
