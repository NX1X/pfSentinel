"""A mock pfSense web UI on 127.0.0.1, for manual tests without a real firewall.

It reuses the fake HTTPS server of the e2e tests: the pfSense login page and
the config download of Diagnostics > Backup & Restore. It serves a small
made-up config.xml. Use it to test a pfs build (scheduled runs, the keyring,
the run history) on a PC that must not hold the real firewall password.

Run it from the repository root, with the development environment:

    .venv/bin/python scripts/mock_pfsense.py

It prints the port and a ``pfs device add`` command, then serves until
Ctrl+C. The port is new on each start, so keep it running for the whole
test. The password is a test value: it opens nothing but this mock.
"""

from __future__ import annotations

import argparse
import shlex
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.e2e.fake_https_server import FakeHTTPSServer  # noqa: E402

DEFAULT_PASSWORD = "mock-only-not-a-real-password"  # noqa: S105 (a test value)

# Small, but complete enough to pass the restore check: an admin user, two
# interfaces, a filter rule and a CA. No real data.
MOCK_XML = """<?xml version="1.0"?>
<pfsense>
  <version>23.3</version>
  <system>
    <hostname>mock-fw</hostname>
    <domain>home.arpa</domain>
    <group>
      <name>admins</name>
      <gid>1999</gid>
      <member>0</member>
      <priv>page-all</priv>
    </group>
    <user>
      <name>admin</name>
      <uid>0</uid>
      <groupname>admins</groupname>
      <scope>system</scope>
    </user>
  </system>
  <interfaces>
    <wan><if>vtnet0</if><ipaddr>dhcp</ipaddr></wan>
    <lan><if>vtnet1</if><ipaddr>192.0.2.1</ipaddr><subnet>24</subnet></lan>
  </interfaces>
  <filter>
    <rule>
      <type>pass</type>
      <interface>lan</interface>
      <source><network>lan</network></source>
      <destination><any/></destination>
      <descr>Default allow LAN to any rule</descr>
    </rule>
  </filter>
  <ca>
    <refid>mockca000001</refid>
    <descr>Mock CA</descr>
  </ca>
</pfsense>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--password",
        default=DEFAULT_PASSWORD,
        help="Test password (default: mock-only-not-a-real-password)",
    )
    parser.add_argument("--id", default="mock-fw", help="Device ID for the printed command")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

    server = FakeHTTPSServer.start(
        username=args.username, password=args.password, config_xml=MOCK_XML
    )
    print(f"Mock pfSense web UI: https://127.0.0.1:{server.port}/ (self-signed certificate)")
    print(f"Username: {args.username}")
    # Not printed: a password in output is a bad habit, even a test one.
    print("Password: the --password value (default: see --help)")
    print()
    print("In another terminal, add it as a device:")
    command = [
        "pfs", "device", "add", "--host", "127.0.0.1", "--id", args.id,
        "--label", "Mock pfSense", "--method", "https", "--https-port", str(server.port),
        "--username", args.username, "--no-verify-ssl",
    ]  # fmt: skip
    print("  " + shlex.join(command))
    print()
    print("Serving until Ctrl+C. Keep this terminal open for the whole test.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
