"""Tests for type-aware backup verification."""

from __future__ import annotations

import gzip
import tarfile
from pathlib import Path

import pytest

from pfsentinel.models.backup import BackupRecord, BackupType
from pfsentinel.models.config import AppConfig, BackupPolicy
from pfsentinel.services.backup import BackupError, BackupService
from pfsentinel.services.credentials import CredentialService
from pfsentinel.utils.checksum import sha256_file
from tests.conftest import SAMPLE_XML


@pytest.fixture
def backup_root(tmp_path: Path) -> Path:
    root = tmp_path / "backups"
    root.mkdir()
    return root


@pytest.fixture
def backup_service(backup_root: Path) -> BackupService:
    config = AppConfig(backup_policy=BackupPolicy(backup_root=backup_root))
    creds = CredentialService()
    return BackupService(config, creds)


class TestVerifyConfigBackup:
    def test_verify_valid_config(self, backup_service: BackupService, backup_root: Path):
        device_dir = backup_root / "fw1"
        device_dir.mkdir()
        backup_path = device_dir / "test.xml.gz"
        backup_path.write_bytes(gzip.compress(SAMPLE_XML.encode("utf-8")))

        record = BackupRecord(
            device_id="fw1",
            filename="test.xml.gz",
            relative_path="test.xml.gz",
            backup_type=BackupType.CONFIG,
            sha256=sha256_file(backup_path),
            compressed=True,
        )

        assert backup_service.verify_backup(record) is True

    def test_verify_config_bad_checksum(self, backup_service: BackupService, backup_root: Path):
        device_dir = backup_root / "fw1"
        device_dir.mkdir()
        backup_path = device_dir / "test.xml.gz"
        backup_path.write_bytes(gzip.compress(SAMPLE_XML.encode("utf-8")))

        record = BackupRecord(
            device_id="fw1",
            filename="test.xml.gz",
            relative_path="test.xml.gz",
            backup_type=BackupType.CONFIG,
            sha256="wrong_hash",
        )

        with pytest.raises(BackupError, match="Checksum mismatch"):
            backup_service.verify_backup(record)


class TestVerifyTarBackup:
    def _create_tar_gz(self, path: Path, filenames: list[str]) -> None:
        with tarfile.open(path, "w:gz") as tar:
            for name in filenames:
                import io

                data = b"test data"
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    def test_verify_valid_tar(self, backup_service: BackupService, backup_root: Path):
        device_dir = backup_root / "fw1" / "rrd" / "2025" / "07" / "06"
        device_dir.mkdir(parents=True)
        tar_path = device_dir / "fw1_rrd.tar.gz"
        self._create_tar_gz(tar_path, ["wan.rrd", "lan.rrd"])

        record = BackupRecord(
            device_id="fw1",
            filename="fw1_rrd.tar.gz",
            relative_path="rrd/2025/07/06/fw1_rrd.tar.gz",
            backup_type=BackupType.RRD,
            sha256=sha256_file(tar_path),
            compressed=True,
        )

        assert backup_service.verify_backup(record) is True

    def test_verify_corrupted_tar(self, backup_service: BackupService, backup_root: Path):
        device_dir = backup_root / "fw1"
        device_dir.mkdir()
        tar_path = device_dir / "bad.tar.gz"
        tar_path.write_bytes(b"not a valid tar file")

        record = BackupRecord(
            device_id="fw1",
            filename="bad.tar.gz",
            relative_path="bad.tar.gz",
            backup_type=BackupType.RRD,
            sha256=sha256_file(tar_path),
            compressed=True,
        )

        with pytest.raises(BackupError, match="Tar archive verification failed"):
            backup_service.verify_backup(record)

    def test_verify_all_tar_types(self, backup_service: BackupService, backup_root: Path):
        """All tar-based backup types should use tar verification."""
        tar_types = [
            BackupType.RRD,
            BackupType.PACKAGE_CONFIGS,
            BackupType.CERTIFICATES,
            BackupType.LOGS,
            BackupType.ALIASES,
            BackupType.FS_ARCHIVE,
        ]
        for btype in tar_types:
            device_dir = backup_root / "fw1"
            device_dir.mkdir(exist_ok=True)
            tar_path = device_dir / f"{btype.value}.tar.gz"
            self._create_tar_gz(tar_path, ["test.txt"])

            record = BackupRecord(
                device_id="fw1",
                filename=f"{btype.value}.tar.gz",
                relative_path=f"{btype.value}.tar.gz",
                backup_type=btype,
                sha256=sha256_file(tar_path),
                compressed=True,
            )

            assert backup_service.verify_backup(record) is True, f"Failed for {btype}"


class TestVerifyZfsBackup:
    def test_verify_zfs_checksum_only(self, backup_service: BackupService, backup_root: Path):
        """ZFS snapshots only need checksum verification, not tar."""
        device_dir = backup_root / "fw1"
        device_dir.mkdir()
        zfs_path = device_dir / "fw1_zfs.zfs.gz"
        zfs_path.write_bytes(b"fake zfs stream data")

        record = BackupRecord(
            device_id="fw1",
            filename="fw1_zfs.zfs.gz",
            relative_path="fw1_zfs.zfs.gz",
            backup_type=BackupType.ZFS_SNAPSHOT,
            sha256=sha256_file(zfs_path),
        )

        assert backup_service.verify_backup(record) is True


class TestVerifyDhcpBackup:
    def test_verify_dhcp_checksum_only(self, backup_service: BackupService, backup_root: Path):
        """DHCP leases only need checksum verification."""
        device_dir = backup_root / "fw1"
        device_dir.mkdir()
        dhcp_path = device_dir / "fw1_dhcp.txt.gz"
        dhcp_path.write_bytes(gzip.compress(b"lease data"))

        record = BackupRecord(
            device_id="fw1",
            filename="fw1_dhcp.txt.gz",
            relative_path="fw1_dhcp.txt.gz",
            backup_type=BackupType.DHCP_LEASES,
            sha256=sha256_file(dhcp_path),
        )

        assert backup_service.verify_backup(record) is True


class TestVerifyMissingFile:
    def test_verify_missing_file_raises(self, backup_service: BackupService):
        record = BackupRecord(
            device_id="fw1",
            filename="nonexistent.xml",
            relative_path="nonexistent.xml",
        )
        with pytest.raises(BackupError, match="Backup file not found"):
            backup_service.verify_backup(record)
