"""Fill in the winget manifest template for one stable release.

Usage (the CI job publish-winget runs it after the GitHub release exists):

    python scripts/winget_manifest.py --version 1.0.0 --date 2026-12-04 \
        --checksums checksums-sha256.txt --out build/winget

Writes the three files to <out>/manifests/n/NX1X/pfSentinel/<version>/ and
prints that folder. Stops with an error for a release candidate, a missing
or malformed pfs.exe checksum, or a placeholder left in a file.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "packaging" / "winget"
PACKAGE_ID = "NX1X.pfSentinel"
FILES = (
    f"{PACKAGE_ID}.yaml",
    f"{PACKAGE_ID}.installer.yaml",
    f"{PACKAGE_ID}.locale.en-US.yaml",
)
_STABLE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class ManifestError(ValueError):
    pass


def exe_sha256(checksums: str, name: str = "pfs.exe") -> str:
    """The SHA-256 of ``name`` from a sha256sum file, in upper case."""
    for line in checksums.splitlines():
        parts = line.split()
        # sha256sum writes "<hash>  <name>", or "<hash> *<name>" in binary mode.
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            if not _SHA256.match(parts[0]):
                raise ManifestError(f"Malformed checksum for {name}: {parts[0]!r}")
            return parts[0].upper()
    raise ManifestError(f"No checksum for {name} in the checksums file")


def render(version: str, release_date: str, sha256: str, template_dir: Path = TEMPLATE_DIR) -> dict:
    """File name -> filled-in text for the three manifest files."""
    if not _STABLE.match(version):
        raise ManifestError(
            f"{version!r} is not a stable version (X.Y.Z). winget gets no release candidates."
        )
    date.fromisoformat(release_date)  # ValueError for a bad date
    if not _SHA256.match(sha256):
        raise ManifestError(f"Malformed SHA-256: {sha256!r}")
    values = {
        "{{VERSION}}": version,
        "{{RELEASE_DATE}}": release_date,
        "{{SHA256}}": sha256.upper(),
    }
    out = {}
    for name in FILES:
        text = (template_dir / name).read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace(key, value)
        if "{{" in text or "}}" in text:
            raise ManifestError(f"A placeholder is left in {name}")
        out[name] = text
    return out


def write(files: dict, version: str, out: Path) -> Path:
    folder = out / "manifests" / "n" / "NX1X" / "pfSentinel" / version
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8", newline="\n")
    return folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="release version without v, e.g. 1.0.0")
    parser.add_argument("--date", required=True, help="release date, YYYY-MM-DD")
    parser.add_argument("--checksums", required=True, type=Path, help="checksums-sha256.txt")
    parser.add_argument("--out", required=True, type=Path, help="output folder")
    args = parser.parse_args(argv)
    try:
        sha = exe_sha256(args.checksums.read_text(encoding="utf-8"))
        folder = write(render(args.version, args.date, sha), args.version, args.out)
    except (ManifestError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    # Forward slashes: the CI job reads this path in bash on Windows too.
    print(folder.as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
