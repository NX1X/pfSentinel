"""Tests for extended naming utilities."""

from __future__ import annotations

from datetime import datetime

from pfsentinel.utils.naming import (
    generate_backup_filename,
    generate_typed_relative_path,
)


class TestGenerateBackupFilename:
    def test_rrd_compressed(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        name = generate_backup_filename(
            device_id="fw1", backup_type="rrd", compressed=True, timestamp=ts
        )
        assert name == "fw1_2025-07-06_143022_#001_rrd.tar.gz"

    def test_rrd_uncompressed(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        name = generate_backup_filename(
            device_id="fw1", backup_type="rrd", compressed=False, timestamp=ts
        )
        assert name == "fw1_2025-07-06_143022_#001_rrd.tar"

    def test_dhcp_compressed(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        name = generate_backup_filename(
            device_id="fw1", backup_type="dhcp", compressed=True, timestamp=ts
        )
        assert name == "fw1_2025-07-06_143022_#001_dhcp.txt.gz"

    def test_zfs_full_label(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        name = generate_backup_filename(
            device_id="fw1",
            backup_type="zfs",
            compressed=True,
            timestamp=ts,
            label="zfs-full",
            extension=".zfs",
        )
        assert name == "fw1_2025-07-06_143022_#001_zfs-full.zfs.gz"

    def test_zfs_incremental_label(self):
        ts = datetime(2025, 7, 6, 15, 0, 0)
        name = generate_backup_filename(
            device_id="fw1",
            backup_type="zfs",
            compressed=True,
            timestamp=ts,
            sequence=2,
            label="zfs-incr",
            extension=".zfs",
        )
        assert name == "fw1_2025-07-06_150000_#002_zfs-incr.zfs.gz"

    def test_archive_type(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        name = generate_backup_filename(
            device_id="fw1", backup_type="archive", compressed=True, timestamp=ts
        )
        assert name == "fw1_2025-07-06_143022_#001_archive.tar.gz"

    def test_custom_sequence(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        name = generate_backup_filename(
            device_id="fw1", backup_type="logs", compressed=True, timestamp=ts, sequence=5
        )
        assert "_#005_" in name

    def test_unknown_type_uses_dat(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        name = generate_backup_filename(
            device_id="fw1", backup_type="unknown", compressed=False, timestamp=ts
        )
        assert name.endswith(".dat")

    def test_pkg_type(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        name = generate_backup_filename(
            device_id="fw1", backup_type="pkg", compressed=True, timestamp=ts
        )
        assert name == "fw1_2025-07-06_143022_#001_pkg.tar.gz"

    def test_certs_type(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        name = generate_backup_filename(
            device_id="fw1", backup_type="certs", compressed=False, timestamp=ts
        )
        assert name == "fw1_2025-07-06_143022_#001_certs.tar"


class TestGenerateTypedRelativePath:
    def test_rrd_path(self):
        ts = datetime(2025, 7, 6, 14, 30, 22)
        path = generate_typed_relative_path("rrd", "fw1_rrd.tar.gz", ts)
        assert path == "rrd/2025/07/06/fw1_rrd.tar.gz"

    def test_zfs_path(self):
        ts = datetime(2025, 12, 25, 0, 0, 0)
        path = generate_typed_relative_path("zfs", "fw1_zfs-full.zfs.gz", ts)
        assert path == "zfs/2025/12/25/fw1_zfs-full.zfs.gz"

    def test_archive_path(self):
        ts = datetime(2025, 1, 1, 8, 0, 0)
        path = generate_typed_relative_path("archive", "fw1_archive.tar.gz", ts)
        assert path == "archive/2025/01/01/fw1_archive.tar.gz"

    def test_month_day_zero_padded(self):
        ts = datetime(2025, 3, 5, 12, 0, 0)
        path = generate_typed_relative_path("dhcp", "test.txt.gz", ts)
        assert path == "dhcp/2025/03/05/test.txt.gz"
