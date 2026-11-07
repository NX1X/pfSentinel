"""SSH host key pinning for pfSense devices.

pfSentinel keeps its own known_hosts file next to config.json
(``~/.pfsentinel/known_hosts``) instead of writing to ``~/.ssh/known_hosts``.
Devices use strict host key checking by default: a key must be trusted once
with ``pfs device trust-key <id>``, and any later change is refused as a
possible man-in-the-middle until the user re-trusts it (for example after a
pfSense reinstall).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import socket
import tempfile
from pathlib import Path

import paramiko

from pfsentinel.models.config import AppConfig
from pfsentinel.services.connection import HostKeyError
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)


def known_hosts_path() -> Path:
    return AppConfig.config_path().parent / "known_hosts"


def host_entry(host: str, port: int) -> str:
    """known_hosts host pattern, in the format OpenSSH and paramiko expect."""
    return host if port == 22 else f"[{host}]:{port}"


def fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH-style SHA256 fingerprint, e.g. 'SHA256:abc...'."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def fetch_host_key(host: str, port: int, timeout: int = 15) -> paramiko.PKey:
    """Connect far enough to read the server's host key. No authentication."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise HostKeyError(f"Cannot reach {host}:{port}: {e}") from e
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        return transport.get_remote_server_key()
    except paramiko.SSHException as e:
        raise HostKeyError(f"SSH handshake with {host}:{port} failed: {e}") from e
    finally:
        transport.close()


def trusted_key(host: str, port: int) -> paramiko.PKey | None:
    """The key pinned for host:port, or None."""
    path = known_hosts_path()
    if not path.is_file():
        return None
    keys = paramiko.HostKeys(str(path))
    entry = keys.lookup(host_entry(host, port))
    if not entry:
        return None
    return next(iter(entry.values()), None)


def trust(host: str, port: int, key: paramiko.PKey) -> Path:
    """Pin ``key`` for host:port, replacing every previous key for that entry.

    The file is written to a temporary file with owner-only permissions and
    then moved into place, so a crash cannot leave a half-written file and the
    pins are never readable by other users, even briefly.
    """
    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = paramiko.HostKeys(str(path)) if path.is_file() else paramiko.HostKeys()
    name = host_entry(host, port)
    # paramiko's `del keys[name]` removes only the first matching entry, which
    # would leave other key types for this host pinned. Rebuild the set without
    # this host instead, keeping every other host's keys.
    keys = paramiko.HostKeys()
    for hostname in existing:
        if hostname == name:
            continue
        for keytype, hostkey in existing[hostname].items():
            keys.add(hostname, keytype, hostkey)
    keys.add(name, key.get_name(), key)

    # mkstemp creates the file with mode 0600, and save() keeps that mode.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".known_hosts.", suffix=".tmp")
    try:
        os.close(fd)
        keys.save(tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    logger.info(f"Trusted {key.get_name()} host key {fingerprint(key)} for {name}")
    return path
