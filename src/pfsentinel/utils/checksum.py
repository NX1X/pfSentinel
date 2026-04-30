"""SHA256 checksum utilities."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Calculate SHA256 hash of bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_string(text: str, encoding: str = "utf-8") -> str:
    """Calculate SHA256 hash of a string."""
    return sha256_bytes(text.encode(encoding))


def verify_file(path: Path, expected_hash: str) -> bool:
    """Verify a file's SHA256 hash matches expected."""
    if not path.exists():
        return False
    return sha256_file(path) == expected_hash.lower()
