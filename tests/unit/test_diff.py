"""Tests for change detection between backups."""

from __future__ import annotations

from pathlib import Path

from pfsentinel.models.backup import BackupIndex, BackupRecord, ChangeCategory
from pfsentinel.services.diff import DiffService


def _make_record(device_id="fw1", filename="backup.xml", relative_path="2025/03/05/backup.xml"):
    return BackupRecord(
        device_id=device_id,
        filename=filename,
        relative_path=relative_path,
    )


def _write_backup(backup_root: Path, device_id: str, relative_path: str, xml: str) -> None:
    path = backup_root / device_id / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")


class TestSectionChanged:
    def test_both_none(self):
        svc = DiffService(Path("/tmp"))
        assert svc._section_changed(None, None) is False

    def test_current_none(self):
        svc = DiffService(Path("/tmp"))
        assert svc._section_changed(None, "<x/>") is True

    def test_last_none(self):
        svc = DiffService(Path("/tmp"))
        assert svc._section_changed("<x/>", None) is True

    def test_same_content(self):
        svc = DiffService(Path("/tmp"))
        assert svc._section_changed("<x>hello</x>", "<x>hello</x>") is False

    def test_different_content(self):
        svc = DiffService(Path("/tmp"))
        assert svc._section_changed("<x>old</x>", "<x>new</x>") is True

    def test_whitespace_normalized(self):
        svc = DiffService(Path("/tmp"))
        assert svc._section_changed("  <x/>  ", "<x/>") is False


class TestDetect:
    def test_no_prior_backup_returns_initial(self, tmp_path: Path):
        svc = DiffService(tmp_path)
        index = BackupIndex(device_id="fw1")
        xml = '<?xml version="1.0"?><pfsense><system><hostname>fw</hostname></system></pfsense>'
        result = svc.detect("fw1", xml, index)
        assert result == [ChangeCategory.INITIAL]

    def test_identical_xml_returns_minor(self, tmp_path: Path, sample_xml: str):
        svc = DiffService(tmp_path)
        record = _make_record()
        _write_backup(tmp_path, "fw1", record.relative_path, sample_xml)
        index = BackupIndex(device_id="fw1", records=[record])
        result = svc.detect("fw1", sample_xml, index)
        assert result == [ChangeCategory.MINOR]

    def test_interface_change_detected(
        self, tmp_path: Path, sample_xml: str, sample_xml_modified: str
    ):
        svc = DiffService(tmp_path)
        record = _make_record()
        _write_backup(tmp_path, "fw1", record.relative_path, sample_xml)
        index = BackupIndex(device_id="fw1", records=[record])
        result = svc.detect("fw1", sample_xml_modified, index)
        assert ChangeCategory.INTERFACES in result

    def test_missing_backup_file_returns_initial(self, tmp_path: Path, sample_xml: str):
        svc = DiffService(tmp_path)
        record = _make_record()
        # don't write the file
        index = BackupIndex(device_id="fw1", records=[record])
        result = svc.detect("fw1", sample_xml, index)
        assert result == [ChangeCategory.INITIAL]

    def test_parse_error_returns_minor(self, tmp_path: Path, sample_xml: str):
        svc = DiffService(tmp_path)
        record = _make_record()
        _write_backup(tmp_path, "fw1", record.relative_path, "<invalid>not pfsense</invalid>")
        index = BackupIndex(device_id="fw1", records=[record])
        result = svc.detect("fw1", sample_xml, index)
        assert result == [ChangeCategory.MINOR]


class TestGenerateTextDiff:
    def test_generates_unified_diff(
        self, tmp_path: Path, sample_xml: str, sample_xml_modified: str
    ):
        svc = DiffService(tmp_path)
        rec_a = _make_record(relative_path="2025/03/05/a.xml", filename="a.xml")
        rec_b = _make_record(relative_path="2025/03/05/b.xml", filename="b.xml")
        _write_backup(tmp_path, "fw1", rec_a.relative_path, sample_xml)
        _write_backup(tmp_path, "fw1", rec_b.relative_path, sample_xml_modified)
        diff = svc.generate_text_diff(rec_a, rec_b)
        assert "---" in diff
        assert "+++" in diff

    def test_file_not_found_returns_error(self, tmp_path: Path):
        svc = DiffService(tmp_path)
        rec_a = _make_record(relative_path="2025/03/05/missing.xml", filename="missing.xml")
        rec_b = _make_record(relative_path="2025/03/05/also_missing.xml", filename="also.xml")
        diff = svc.generate_text_diff(rec_a, rec_b)
        assert "Error" in diff
