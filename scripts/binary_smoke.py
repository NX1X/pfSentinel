"""Smoke test of the built binary (dist/pfs or dist/pfs.exe) against a fake SSH server.

The unit and e2e tests run the code from source. This runs the frozen
PyInstaller build, which can break on its own (missing hidden imports, data
files, the bundled environment). It covers: host key pinning, passphrase
encryption (scrypt), two encrypted runs with run folders, the second copy to a
folder, verify, the restore check, check-recent and decrypt.

Run from the repository root after `python scripts/build_exe.py`:
    python scripts/binary_smoke.py
Exit code 0 when every step passes.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.e2e.fake_ssh_server import FakeSSHServer  # noqa: E402
from tests.unit.test_config_checks import BASE  # noqa: E402

BIN = ROOT / "dist" / ("pfs.exe" if sys.platform == "win32" else "pfs")
# A new random passphrase on every run: nothing to mistake for a real secret.
PASSPHRASE = secrets.token_urlsafe(18)


def main() -> int:
    if not BIN.is_file():
        print(f"Binary not found: {BIN}. Run: python scripts/build_exe.py")
        return 2
    home = Path(tempfile.mkdtemp(prefix="pfs-smoke-"))
    env = dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        # No OS keystore in CI: use pfSentinel's encrypted file store.
        PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
    )
    results: list[tuple[str, bool, str]] = []

    def run(name: str, args: list[str], stdin: str = "", expect: str | None = None) -> str:
        proc = subprocess.run(
            [str(BIN), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
            encoding="utf-8",
            errors="replace",
        )
        out = proc.stdout + proc.stderr
        ok = proc.returncode == 0 and (expect is None or expect in out)
        results.append((name, ok, "" if ok else out[-600:]))
        return out

    server = FakeSSHServer.start(
        username="admin",
        password="hunter2",  # noqa: S106 - the e2e fake server password
        files={
            "/cf/conf/config.xml": BASE.encode(),
            "/var/dhcpd/var/db/dhcpd.leases": b"lease 192.0.2.9 { }\n",
        },
    )
    try:
        backups, share, cfg = home / "backups", home / "share", home / ".pfsentinel"
        for folder in (backups, share, cfg):
            folder.mkdir()
        config = {
            "devices": [
                {
                    "id": "home-fw",
                    "label": "fw",
                    "host": server.host,
                    "ssh_port": server.port,
                    "username": "admin",
                    "strict_host_keys": True,
                }
            ],
            "backup_policy": {"backup_root": str(backups)},
            "notifications": {"windows_toast_enabled": False, "update_alerts": False},
        }
        (cfg / "config.json").write_text(json.dumps(config), encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from pfsentinel.services.credentials import CredentialService as C;"
                "C().store('home-fw','hunter2')",
            ],
            env=env,
            check=True,
        )

        run("version", ["--version"], expect="pfSentinel v")
        run("trust-key", ["device", "trust-key", "home-fw", "--yes"], expect="trusted")
        run("device test", ["device", "test", "-d", "home-fw"], expect="OK")
        run(
            "encryption setup (passphrase)",
            ["encryption", "setup", "--mode", "passphrase"],
            stdin=f"{PASSPHRASE}\n{PASSPHRASE}\n",
            expect="Encryption is on (passphrase)",
        )
        run("copy setup (folder)", ["copy", "setup"], stdin=f"folder\n{share}\n90\n")
        for n in (1, 2):
            run(f"backup run {n}", ["backup", "run", "-d", "home-fw", "--include", "dhcp"])
        encs = sorted(backups.rglob("*.enc"))
        plain = [p for p in backups.rglob("*") if p.is_file() and p.suffix in {".gz", ".xml"}]
        results.append(("4 encrypted files, none plain", len(encs) == 4 and not plain, ""))
        results.append(("second copy has 4 files", len(list(share.rglob("*.enc"))) == 4, ""))
        run("backup list", ["backup", "list"], expect="#2")
        run("backup verify", ["backup", "verify", encs[0].name])
        run("backup check", ["backup", "check"], expect="OK: 2 users")
        run("backup check-recent", ["backup", "check-recent", "--non-interactive"])
        run("encryption status", ["encryption", "status"], expect="4 encrypted, 0 plain")
        config_file = next(p for p in encs if ".xml.gz.enc" in p.name)
        restored = home / "restored.xml"
        run(
            "decrypt",
            ["backup", "decrypt", str(config_file), "--passphrase", "--decompress"]
            + ["-o", str(restored)],
            stdin=f"{PASSPHRASE}\n",
            expect="Decrypted to",
        )
        ok = restored.is_file() and "<pfsense>" in restored.read_text(encoding="utf-8")
        results.append(("decrypted XML", ok, ""))
        run("schedule status", ["schedule", "status"])
        run("notify status", ["notify", "status"])
    finally:
        server.stop()

    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
