"""Second copy of the encrypted backups on a self-hosted target.

After each run, every encrypted backup in the 0.3.0 run-folder layout that
the target does not have yet is copied there, so a failed copy is simply
retried on the next run. Plain (unencrypted) files are never copied.

Targets:
- ``folder``: a local path, for example a share or a Nextcloud-synced folder.
- ``sftp``: an SSH server, with key login and a pinned host key (the same
  strict checking as for pfSense devices).

The target keeps its own retention: run folders whose day is older than
``keep_days`` are removed. Only the layout pfSentinel writes there
(``<device>/YYYY/MM/DD/<run>/``) is touched.
"""

from __future__ import annotations

import posixpath
import shutil
import stat
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pfsentinel.models.backup import BackupRecord
from pfsentinel.models.config import AppConfig, SecondCopyPolicy
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.retention import RetentionService
from pfsentinel.utils.logging import get_logger

logger = get_logger(__name__)


class SecondCopyError(Exception):
    """The second copy target is not usable."""


@dataclass
class CopyResult:
    copied: list[str] = field(default_factory=list)
    skipped_plain: list[str] = field(default_factory=list)
    removed_days: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CopyTarget(Protocol):
    def size(self, rel: str) -> int | None: ...
    def put(self, local: Path, rel: str) -> None: ...
    def day_folders(self, device_id: str) -> list[str]: ...
    def remove_tree(self, rel: str) -> None: ...
    def close(self) -> None: ...


def _parts_ok(rel: str) -> bool:
    parts = rel.split("/")
    return bool(rel) and not rel.startswith("/") and ".." not in parts and "." not in parts


class FolderTarget:
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser()
        if not self._root.is_dir():
            raise SecondCopyError(f"Second copy folder does not exist: {self._root}")

    def _path(self, rel: str) -> Path:
        if not _parts_ok(rel):
            raise SecondCopyError(f"Unsafe path: {rel}")
        return self._root / rel

    def size(self, rel: str) -> int | None:
        path = self._path(rel)
        return path.stat().st_size if path.is_file() else None

    def put(self, local: Path, rel: str) -> None:
        dest = self._path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        shutil.copyfile(local, tmp)
        tmp.replace(dest)

    def day_folders(self, device_id: str) -> list[str]:
        base = self._path(device_id)
        if not base.is_dir():
            return []
        return [
            "/".join(p.relative_to(self._root).parts)
            for p in base.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]")
            if p.is_dir() and not p.is_symlink()
        ]

    def remove_tree(self, rel: str) -> None:
        shutil.rmtree(self._path(rel))

    def close(self) -> None:
        pass


class SftpTarget:
    def __init__(self, policy: SecondCopyPolicy, creds: CredentialService) -> None:
        import paramiko

        from pfsentinel.services import host_keys

        if not (policy.host and policy.username and policy.key_path and policy.path):
            raise SecondCopyError("SFTP target needs host, username, key_path and path")
        if host_keys.trusted_key(policy.host, policy.port) is None:
            raise SecondCopyError(
                f"SSH host key of {policy.host} is not trusted yet. Run: pfs copy setup"
            )
        client = paramiko.SSHClient()
        client.get_host_keys().load(str(host_keys.known_hosts_path()))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())  # nosec B507
        try:
            client.connect(
                hostname=policy.host,
                port=policy.port,
                username=policy.username,
                key_filename=str(Path(policy.key_path).expanduser()),
                passphrase=creds.get_secret("__second_copy_keypass__"),
                look_for_keys=False,
                allow_agent=False,
                timeout=20,
            )
        except paramiko.BadHostKeyException as e:
            raise SecondCopyError(
                f"SSH host key of {policy.host} has CHANGED: refusing to copy. If you "
                "replaced the server, verify the new key and run: pfs copy setup"
            ) from e
        except Exception as e:
            raise SecondCopyError(f"Cannot connect to {policy.host}: {e}") from e
        self._client = client
        self._sftp = client.open_sftp()
        self._root = policy.path.rstrip("/") or "/"

    def _path(self, rel: str) -> str:
        if not _parts_ok(rel):
            raise SecondCopyError(f"Unsafe path: {rel}")
        return posixpath.join(self._root, rel)

    def size(self, rel: str) -> int | None:
        try:
            return self._sftp.stat(self._path(rel)).st_size
        except OSError:
            return None

    def _mkdirs(self, remote_dir: str) -> None:
        parts = remote_dir.split("/")
        current = "/" if remote_dir.startswith("/") else ""
        for part in parts:
            if not part:
                continue
            current = posixpath.join(current, part) if current else part
            try:
                self._sftp.stat(current)
            except OSError:
                self._sftp.mkdir(current, mode=0o700)

    def put(self, local: Path, rel: str) -> None:
        dest = self._path(rel)
        self._mkdirs(posixpath.dirname(dest))
        tmp = dest + ".part"
        self._sftp.put(str(local), tmp, confirm=True)
        self._sftp.posix_rename(tmp, dest)

    def day_folders(self, device_id: str) -> list[str]:
        out = []
        base = self._path(device_id)
        for year in self._dirs(base):
            for month in self._dirs(posixpath.join(base, year)):
                for day in self._dirs(posixpath.join(base, year, month)):
                    out.append(f"{device_id}/{year}/{month}/{day}")
        return out

    def _dirs(self, remote: str) -> list[str]:
        try:
            entries = self._sftp.listdir_attr(remote)
        except OSError:
            return []
        return [
            e.filename
            for e in entries
            if e.filename.isdigit() and e.st_mode is not None and stat.S_ISDIR(e.st_mode)
        ]

    def remove_tree(self, rel: str) -> None:
        self._rmtree(self._path(rel))

    def _rmtree(self, remote: str) -> None:
        for entry in self._sftp.listdir_attr(remote):
            child = posixpath.join(remote, entry.filename)
            if entry.st_mode is not None and stat.S_ISDIR(entry.st_mode):
                self._rmtree(child)
            else:
                self._sftp.remove(child)
        self._sftp.rmdir(remote)

    def close(self) -> None:
        self._sftp.close()
        self._client.close()


def open_target(policy: SecondCopyPolicy, creds: CredentialService) -> CopyTarget:
    if policy.kind == "sftp":
        return SftpTarget(policy, creds)
    if not policy.path:
        raise SecondCopyError("No second copy folder set. Run: pfs copy setup")
    return FolderTarget(Path(policy.path))


def _day_of(folder: str) -> date | None:
    try:
        _, year, month, day = folder.split("/")
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


class SecondCopyService:
    def __init__(self, config: AppConfig, creds: CredentialService) -> None:
        self._config = config
        self._policy = config.backup_policy.second_copy
        self._creds = creds
        self._root = config.backup_policy.resolved_root

    def sync(self, device_id: str) -> CopyResult:
        """Copy what the target is missing, then apply the target's retention."""
        result = CopyResult()
        if not self._policy.enabled:
            return result
        index = RetentionService(self._root, self._config.backup_policy).load_index(device_id)
        cutoff = datetime.now() - timedelta(days=self._policy.keep_days)
        wanted: list[BackupRecord] = [
            r for r in index.records if r.run_number is not None and r.created_at >= cutoff
        ]
        try:
            target = open_target(self._policy, self._creds)
        except SecondCopyError as e:
            result.errors.append(str(e))
            return result
        try:
            for record in wanted:
                rel = f"{device_id}/{record.relative_path}".replace("\\", "/")
                if not record.encrypted:
                    result.skipped_plain.append(record.filename)
                    continue
                local = self._root / device_id / record.relative_path
                if not local.is_file():
                    continue
                try:
                    if target.size(rel) == local.stat().st_size:
                        continue
                    target.put(local, rel)
                    if target.size(rel) != local.stat().st_size:
                        raise SecondCopyError("size differs after the copy")
                    result.copied.append(rel)
                except Exception as e:
                    result.errors.append(f"{record.filename}: {e}")
            for folder in target.day_folders(device_id):
                day = _day_of(folder)
                if day is not None and day < cutoff.date():
                    try:
                        target.remove_tree(folder)
                        result.removed_days.append(folder)
                    except Exception as e:
                        result.errors.append(f"cleanup {folder}: {e}")
        finally:
            target.close()
        return result
