"""Tests for SHA256 checksum utilities."""

from __future__ import annotations

from pathlib import Path

from pfsentinel.utils.checksum import sha256_bytes, sha256_file, sha256_string, verify_file


def test_sha256_bytes_consistent():
    data = b"hello world"
    h1 = sha256_bytes(data)
    h2 = sha256_bytes(data)
    assert h1 == h2
    assert len(h1) == 64


def test_sha256_string():
    h = sha256_string("hello")
    assert len(h) == 64


def test_sha256_file(tmp_path: Path):
    f = tmp_path / "test.txt"
    f.write_bytes(b"hello world")
    h = sha256_file(f)
    assert h == sha256_bytes(b"hello world")


def test_verify_file_ok(tmp_path: Path):
    f = tmp_path / "test.txt"
    f.write_bytes(b"hello")
    h = sha256_file(f)
    assert verify_file(f, h) is True


def test_verify_file_bad_hash(tmp_path: Path):
    f = tmp_path / "test.txt"
    f.write_bytes(b"hello")
    assert verify_file(f, "wrong_hash") is False


def test_verify_file_missing(tmp_path: Path):
    assert verify_file(tmp_path / "nonexistent.txt", "abc") is False
