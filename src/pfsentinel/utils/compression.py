"""Gzip compression helpers."""

from __future__ import annotations

import gzip
from pathlib import Path


def compress_file(source: Path, dest: Path) -> None:
    """Compress source file to dest with gzip."""
    with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
        f_out.write(f_in.read())


def decompress_file(source: Path, dest: Path) -> None:
    """Decompress gzip source file to dest."""
    with gzip.open(source, "rb") as f_in, open(dest, "wb") as f_out:
        f_out.write(f_in.read())


def compress_bytes(data: bytes) -> bytes:
    """Return gzip-compressed bytes."""
    return gzip.compress(data)


def decompress_bytes(data: bytes) -> bytes:
    """Return decompressed bytes from gzip data."""
    return gzip.decompress(data)


def decompress_to_string(source: Path, encoding: str = "utf-8") -> str:
    """Read a gzip file and return contents as string."""
    with gzip.open(source, "rt", encoding=encoding) as f:
        return f.read()


def read_xml(path: Path) -> str:
    """Read XML from either plain or gzip file."""
    if path.suffix == ".gz":
        return decompress_to_string(path)
    return path.read_text(encoding="utf-8")
