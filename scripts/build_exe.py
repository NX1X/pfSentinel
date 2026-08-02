"""Build script to produce pfs binary via PyInstaller (--onefile mode)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DIST = ROOT / "dist"
SPEC = ROOT / "pfs.spec"

# Output binary name: pfs.exe on Windows, pfs on Linux
EXE_NAME = "pfs"


def main() -> None:
    print("Building pfs binary...")

    # Always regenerate spec to ensure paths are correct
    write_spec()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--clean",
        "--noconfirm",
    ]

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("PyInstaller build FAILED")
        sys.exit(1)

    # On Windows PyInstaller adds .exe automatically
    exe = DIST / f"{EXE_NAME}.exe" if sys.platform == "win32" else DIST / EXE_NAME
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\nBuild successful!")
        print(f"  Output: {exe}")
        print(f"  Size: {size_mb:.1f} MB")

        # Quick smoke test
        result = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  Version check: {result.stdout.strip()}")
        else:
            print(f"  WARNING: Version check failed: {result.stderr}")
    else:
        print("ERROR: Output file not found")
        sys.exit(1)


def write_spec() -> None:
    src_path = str(ROOT / "src").replace("\\", "/")
    icon_file = ROOT / "assets" / "pfsentinel.ico"
    icon_line = (
        f"    icon='{str(icon_file).replace(chr(92), '/')}',\n"
        if icon_file.exists()
        else ""
    )
    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-
block_cipher = None

a = Analysis(
    ['src/pfsentinel/__main__.py'],
    pathex=['{src_path}'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'paramiko',
        'paramiko.transport',
        'paramiko.sftp_client',
        'paramiko.sftp_handle',
        'paramiko.sftp_server',
        'paramiko.server',
        'paramiko.auth_handler',
        'cryptography',
        'cryptography.hazmat.backends',
        'cryptography.hazmat.backends.openssl',
        'keyring.backends.Windows',
        'keyring.backends.fail',
        'rich',
        'pydantic',
        'lxml',
        'lxml.etree',
        'lxml._elementpath',
        'schedule',
        'loguru',
        'pfsentinel.cli.commands.backup',
        'pfsentinel.cli.commands.config',
        'pfsentinel.cli.commands.device',
        'pfsentinel.cli.commands.update',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'customtkinter', 'PyQt5', 'PyQt6', 'wx', 'matplotlib'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='{EXE_NAME}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
{icon_line})
"""
    SPEC.write_text(spec_content)
    print(f"Created spec file: {SPEC}")


if __name__ == "__main__":
    main()
