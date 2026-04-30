"""Tests for backup filename generation and parsing."""

from __future__ import annotations

from datetime import datetime

from pfsentinel.models.backup import ChangeCategory
from pfsentinel.utils.naming import generate_filename, generate_relative_path, parse_filename

TS = datetime(2025, 3, 5, 14, 30, 22)


class TestGenerateFilename:
    def test_empty_changes_uses_minor(self):
        name = generate_filename("fw1", [], 1, False, TS)
        assert "_minor.xml" in name

    def test_single_change(self):
        name = generate_filename("fw1", [ChangeCategory.FIREWALL], 1, False, TS)
        assert "_firewall.xml" in name

    def test_multiple_changes_joined(self):
        changes = [ChangeCategory.INTERFACES, ChangeCategory.FIREWALL]
        name = generate_filename("fw1", changes, 1, False, TS)
        assert "interfaces+firewall" in name

    def test_max_three_changes_truncated(self):
        changes = [
            ChangeCategory.INTERFACES,
            ChangeCategory.FIREWALL,
            ChangeCategory.SYSTEM,
            ChangeCategory.VPN,
        ]
        name = generate_filename("fw1", changes, 1, False, TS)
        assert name.count("+") == 2  # 3 categories, 2 separators

    def test_compressed_extension(self):
        name = generate_filename("fw1", [], 1, True, TS)
        assert name.endswith(".xml.gz")

    def test_uncompressed_extension(self):
        name = generate_filename("fw1", [], 1, False, TS)
        assert name.endswith(".xml")
        assert not name.endswith(".xml.gz")

    def test_timestamp_in_filename(self):
        name = generate_filename("fw1", [], 1, False, TS)
        assert "2025-03-05" in name
        assert "143022" in name

    def test_sequence_formatting(self):
        name = generate_filename("fw1", [], 42, False, TS)
        assert "#042" in name

    def test_device_id_prefix(self):
        name = generate_filename("home-fw", [], 1, False, TS)
        assert name.startswith("home-fw_")


class TestGenerateRelativePath:
    def test_formats_date_path(self):
        path = generate_relative_path("backup.xml", TS)
        assert path == "2025/03/05/backup.xml"

    def test_zero_padded_month_and_day(self):
        ts = datetime(2025, 1, 2, 0, 0, 0)
        path = generate_relative_path("f.xml", ts)
        assert path == "2025/01/02/f.xml"


class TestParseFilename:
    def test_round_trip(self):
        name = generate_filename("fw1", [ChangeCategory.FIREWALL], 5, True, TS)
        parsed = parse_filename(name)
        assert parsed is not None
        assert parsed["device_id"] == "fw1"
        assert parsed["sequence"] == 5
        assert parsed["compressed"] is True
        assert ChangeCategory.FIREWALL in parsed["changes"]

    def test_invalid_filename_returns_none(self):
        assert parse_filename("random-text.txt") is None

    def test_compressed_detection(self):
        name = generate_filename("fw1", [], 1, True, TS)
        parsed = parse_filename(name)
        assert parsed is not None
        assert parsed["compressed"] is True

    def test_uncompressed_detection(self):
        name = generate_filename("fw1", [], 1, False, TS)
        parsed = parse_filename(name)
        assert parsed is not None
        assert parsed["compressed"] is False

    def test_unknown_change_category_skipped(self):
        # Manually construct a filename with unknown category
        name = "fw1_2025-03-05_143022_#001_nosuchcategory.xml"
        parsed = parse_filename(name)
        assert parsed is not None
        assert parsed["changes"] == []

    def test_timestamp_parsed(self):
        name = generate_filename("fw1", [], 1, False, TS)
        parsed = parse_filename(name)
        assert parsed is not None
        assert parsed["timestamp"] == TS
