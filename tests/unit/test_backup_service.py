"""Tests for BackupService — secure wipe, verify, delete, restore."""

from __future__ import annotations

import gzip
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pfsentinel.models.backup import BackupRecord, BackupType, ChangeCategory
from pfsentinel.models.config import AppConfig, BackupPolicy
from pfsentinel.models.device import DeviceConfig
from pfsentinel.services.backup import BackupError, BackupService, _secure_wipe
from pfsentinel.services.credentials import CredentialService


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        devices=[DeviceConfig(id="fw1", label="FW", host="10.0.0.1")],
        backup_policy=BackupPolicy(backup_root=tmp_path / "backups"),
    )


def _make_record(device_id="fw1", **overrides):
    defaults = {
        "device_id": device_id,
        "filename": "backup.xml.gz",
        "relative_path": "2025/03/05/backup.xml.gz",
        "sha256": "",
        "changes": [ChangeCategory.MINOR],
        "backup_type": BackupType.CONFIG,
    }
    defaults.update(overrides)
    return BackupRecord(**defaults)


class TestSecureWipe:
    def test_overwrites_file(self, tmp_path: Path):
        f = tmp_path / "secret.txt"
        f.write_text("sensitive data")
        original_size = f.stat().st_size
        _secure_wipe(f)
        # File should still exist but content overwritten
        assert f.exists()
        content = f.read_bytes()
        assert content == b"\x00" * original_size

    def test_nonexistent_file_no_crash(self, tmp_path: Path):
        f = tmp_path / "missing.txt"
        _secure_wipe(f)  # should not raise


class TestVerifyBackup:
    def test_file_not_found_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = BackupService(config, CredentialService())
        record = _make_record(relative_path="2025/03/05/missing.xml")
        with pytest.raises(BackupError, match="not found"):
            svc.verify_backup(record)

    def test_checksum_mismatch_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = BackupService(config, CredentialService())
        dest = tmp_path / "backups" / "fw1" / "2025" / "03" / "05" / "backup.xml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("<pfsense><system/></pfsense>")
        record = _make_record(
            relative_path="2025/03/05/backup.xml",
            sha256="wrong_hash",
        )
        with pytest.raises(BackupError, match="Checksum mismatch"):
            svc.verify_backup(record)

    def test_valid_config_passes(self, tmp_path: Path, sample_xml: str):
        config = _make_config(tmp_path)
        svc = BackupService(config, CredentialService())
        dest = tmp_path / "backups" / "fw1" / "2025" / "03" / "05" / "backup.xml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(sample_xml)

        from pfsentinel.utils.checksum import sha256_file

        record = _make_record(
            relative_path="2025/03/05/backup.xml",
            sha256=sha256_file(dest),
        )
        assert svc.verify_backup(record) is True

    def test_tar_type_calls_verify_tar(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = BackupService(config, CredentialService())

        # Create a valid tar.gz
        dest = tmp_path / "backups" / "fw1" / "2025" / "03" / "05" / "rrd.tar.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dummy = tmp_path / "dummy.txt"
        dummy.write_text("data")
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(str(dummy), arcname="dummy.txt")

        from pfsentinel.utils.checksum import sha256_file

        record = _make_record(
            relative_path="2025/03/05/rrd.tar.gz",
            sha256=sha256_file(dest),
            backup_type=BackupType.RRD,
            compressed=True,
        )
        assert svc.verify_backup(record) is True


class TestDeleteBackup:
    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin")
    def test_symlink_refuses(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = BackupService(config, CredentialService())

        real_file = tmp_path / "real.txt"
        real_file.write_text("data")
        link_dir = tmp_path / "backups" / "fw1" / "2025" / "03" / "05"
        link_dir.mkdir(parents=True, exist_ok=True)
        link = link_dir / "backup.xml"
        link.symlink_to(real_file)

        record = _make_record(relative_path="2025/03/05/backup.xml")

        with (
            patch.object(svc._retention(), "load_index") as mock_load,
            patch.object(svc._retention(), "save_index"),
            pytest.raises(BackupError, match="symlink"),
        ):
            mock_load.return_value = MagicMock()
            svc.delete_backup(record)


class TestRestoreBackup:
    def test_source_not_found_raises(self, tmp_path: Path):
        config = _make_config(tmp_path)
        svc = BackupService(config, CredentialService())
        record = _make_record(relative_path="2025/03/05/missing.xml")
        with pytest.raises(BackupError, match="not found"):
            svc.restore_backup(record, tmp_path / "restored")

    def test_decompress_on_restore(self, tmp_path: Path, sample_xml: str):
        config = _make_config(tmp_path)
        svc = BackupService(config, CredentialService())

        # Create compressed backup
        dest = tmp_path / "backups" / "fw1" / "2025" / "03" / "05" / "backup.xml.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(gzip.compress(sample_xml.encode("utf-8")))

        record = _make_record(
            relative_path="2025/03/05/backup.xml.gz",
            compressed=True,
        )
        output_dir = tmp_path / "restored"
        output_dir.mkdir()
        result = svc.restore_backup(record, output_dir)
        assert result.exists()
        assert "<pfsense" in result.read_text(encoding="utf-8")
