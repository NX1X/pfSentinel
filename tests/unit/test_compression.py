"""Tests for gzip compression helpers."""

from __future__ import annotations

from pathlib import Path

from pfsentinel.utils.compression import (
    compress_bytes,
    compress_file,
    decompress_bytes,
    decompress_file,
    decompress_to_string,
    read_xml,
)


class TestCompressDecompressFile:
    def test_round_trip(self, tmp_path: Path):
        src = tmp_path / "input.txt"
        compressed = tmp_path / "input.txt.gz"
        output = tmp_path / "output.txt"

        src.write_text("hello world", encoding="utf-8")
        compress_file(src, compressed)
        decompress_file(compressed, output)

        assert output.read_text(encoding="utf-8") == "hello world"

    def test_compressed_is_smaller(self, tmp_path: Path):
        src = tmp_path / "big.txt"
        compressed = tmp_path / "big.txt.gz"
        src.write_text("A" * 10000, encoding="utf-8")
        compress_file(src, compressed)
        assert compressed.stat().st_size < src.stat().st_size


class TestCompressDecompressBytes:
    def test_round_trip(self):
        data = b"some binary content here"
        assert decompress_bytes(compress_bytes(data)) == data


class TestDecompressToString:
    def test_returns_string(self, sample_xml_gz: Path):
        result = decompress_to_string(sample_xml_gz)
        assert isinstance(result, str)
        assert "<pfsense" in result


class TestReadXml:
    def test_reads_gzip_file(self, sample_xml_gz: Path):
        result = read_xml(sample_xml_gz)
        assert "<pfsense" in result

    def test_reads_plain_xml(self, tmp_path: Path, sample_xml: str):
        plain = tmp_path / "config.xml"
        plain.write_text(sample_xml, encoding="utf-8")
        result = read_xml(plain)
        assert "<pfsense" in result
