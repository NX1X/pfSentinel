"""CLI commands for backup operations."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import IO, cast

import typer

from pfsentinel.cli.formatters import (
    console,
    print_backup_table,
    print_error,
    print_info,
    print_progress,
    print_record_detail,
    print_run_history,
    print_success,
    print_warning,
)
from pfsentinel.models.backup import BackupIndexError, BackupRecord
from pfsentinel.models.config import EXTRA_TARGET_FIELDS, AppConfig
from pfsentinel.services.backup import BackupError, BackupService
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.orchestrator import BackupOrchestrator
from pfsentinel.utils.locking import LockTimeoutError

app = typer.Typer(help="Backup operations", no_args_is_help=True)

# Valid extra target names for --include
_VALID_EXTRAS = list(EXTRA_TARGET_FIELDS)


_EXTRAS_MENU = [
    ("1", "rrd", "RRD data (graphs & statistics)"),
    ("2", "pkg", "Package configs (/usr/local/etc)"),
    ("3", "dhcp", "DHCP leases"),
    ("4", "aliases", "Firewall aliases"),
    ("5", "certs", "SSL certificates"),
    ("6", "logs", "System logs"),
]

_ADVANCED_MENU = [
    ("7", "zfs", "ZFS snapshot"),
    ("8", "archive", "Filesystem archive (/cf/conf, /usr/local/etc, /var/db/rrd)"),
]


def _prompt_backup_types() -> tuple[list[str] | None, bool, bool]:
    """Interactive selector for backup types.

    Returns (include_extras, enable_zfs, enable_archive).
    """
    console.print()
    console.print("[bold cyan]What would you like to back up?[/]")
    console.print("[dim]  XML config is always included.[/]")
    console.print()
    console.print("  [bold]Extras:[/]")
    for num, _key, desc in _EXTRAS_MENU:
        console.print(f"    [cyan]{num}[/]) {desc}")
    console.print()
    console.print("  [bold]Advanced:[/]")
    for num, _key, desc in _ADVANCED_MENU:
        console.print(f"    [cyan]{num}[/]) {desc}")
    console.print()
    console.print("  [cyan]A[/]) All extras (1-6)")
    console.print("  [cyan]B[/]) All including advanced (1-8)")
    console.print()

    raw = typer.prompt("  Select (e.g. 1,2,3 or A/B for all, Enter for config only)", default="")
    raw = raw.strip().upper()

    if not raw:
        return None, False, False

    if raw == "A":
        return [key for _, key, _ in _EXTRAS_MENU], False, False

    if raw == "B":
        return [key for _, key, _ in _EXTRAS_MENU], True, True

    # Parse comma-separated numbers
    extras: list[str] = []
    enable_zfs = False
    enable_archive = False
    lookup = {num: key for num, key, _ in _EXTRAS_MENU + _ADVANCED_MENU}

    for token in raw.split(","):
        token = token.strip()
        if token in lookup:
            key = lookup[token]
            if key == "zfs":
                enable_zfs = True
            elif key == "archive":
                enable_archive = True
            else:
                extras.append(key)
        else:
            print_warning(f"Unknown selection '{token}' — skipped")

    return extras or None, enable_zfs, enable_archive


def _get_service(backup_dir: Path | None = None) -> tuple[AppConfig, BackupService]:
    config = AppConfig.load()
    if backup_dir:
        config.backup_policy.backup_root = backup_dir.expanduser().resolve()
    creds = CredentialService()
    return config, BackupService(config, creds)


def _resolve_backup_root(
    config: AppConfig, cli_override: Path | None, interactive: bool = True
) -> Path:
    """Return the backup root to use, prompting if not yet configured.

    If the user provides a path at the prompt, offer to save it permanently.
    The chosen root is set on ``config`` for this run (not saved), because
    the services read it from there. With ``interactive`` False it never
    prompts: it uses the default root.
    """
    if cli_override:
        # Before rc.3 the override was only printed; files went to the configured root.
        chosen = cli_override.expanduser().resolve()
        config.backup_policy.backup_root = chosen
        return chosen

    if config.backup_policy.backup_root is not None:
        return config.backup_policy.backup_root.expanduser()

    if not interactive:
        return config.backup_policy.resolved_root

    # Not configured yet - prompt
    default = config.backup_policy.resolved_root
    console.print()
    console.print("[bold yellow]Backup location not configured.[/]")
    console.print(f"[dim]Default: {default}[/]")
    console.print()
    raw = typer.prompt("Where should backups be saved?", default=str(default))
    chosen = Path(raw).expanduser().resolve()

    save = typer.confirm(f"Save '{chosen}' as the permanent default?", default=True)
    if save:
        config.backup_policy.backup_root = chosen
        config.save()
        console.print(f"[dim]Saved to config: {AppConfig.config_path()}[/]")
    else:
        config.backup_policy.backup_root = chosen  # use for this run only

    console.print()
    return chosen


_VALID_AREAS = [
    "aliases",
    "captiveportal",
    "cert",
    "dhcpd",
    "filter",
    "interfaces",
    "ipsec",
    "nat",
    "openvpn",
    "routes",
    "services",
    "shaper",
    "syslog",
    "system",
    "users",
    "wol",
]


@app.command("run")
def backup_run(
    device: str | None = typer.Option(None, "--device", "-d", help="Device ID (default: all)"),
    description: str | None = typer.Option(None, "--desc", help="Description label"),
    backup_dir: Path | None = typer.Option(
        None,
        "--backup-dir",
        "-o",
        help="Override backup directory (default: ~/Documents/pfSentinel)",
    ),
    no_notify: bool = typer.Option(False, "--no-notify", help="Suppress notifications"),
    area: str | None = typer.Option(
        None,
        "--area",
        help=f"Backup only a specific config section (HTTPS only). "
        f"Valid: {', '.join(_VALID_AREAS)}",
    ),
    no_packages: bool = typer.Option(
        False, "--no-packages", help="Exclude package config (HTTPS only)"
    ),
    include: list[str] | None = typer.Option(
        None,
        "--include",
        help=f"Extra targets (repeatable, or comma-separated): {', '.join(_VALID_EXTRAS)}",
    ),
    all_extras: bool = typer.Option(False, "--all-extras", help="Include all extra backup targets"),
    config_only: bool = typer.Option(
        False, "--config-only", help="Only back up XML config (skip extras)"
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Never prompt; use the backup policy from config (scheduled runs use this)",
    ),
) -> None:
    """Run a backup for one or all devices."""
    if area and area not in _VALID_AREAS:
        print_error(f"Invalid area '{area}'. Valid areas: {', '.join(_VALID_AREAS)}")
        raise typer.Exit(1)

    include_extras: list[str] | None = None
    if include:
        # Flatten: support both --include rrd --include pkg and --include rrd,pkg
        include_extras = [t.strip() for raw in include for t in raw.split(",") if t.strip()]
        invalid = [t for t in include_extras if t not in _VALID_EXTRAS]
        if invalid:
            print_error(
                f"Invalid extra targets: {', '.join(invalid)}. Valid: {', '.join(_VALID_EXTRAS)}"
            )
            raise typer.Exit(1)

    # Interactive selector when no explicit flags provided
    enable_zfs = False
    enable_archive = False
    # Scheduled runs (systemd, cron, Task Scheduler) have nobody to answer a
    # prompt, so they must never reach it: they pass --non-interactive, and a
    # run without a terminal on stdin is treated the same way.
    interactive = not non_interactive and sys.stdin is not None and sys.stdin.isatty()
    if include_extras is None and not all_extras and not config_only and interactive:
        include_extras, enable_zfs, enable_archive = _prompt_backup_types()

    config = AppConfig.load()
    # The location prompt also takes piped input; only --non-interactive
    # (every scheduled run) or no stdin at all skips it.
    may_prompt = not non_interactive and sys.stdin is not None
    backup_root = _resolve_backup_root(config, backup_dir, may_prompt)
    creds = CredentialService()

    # Temporarily enable ZFS/archive if selected interactively
    if enable_zfs:
        config.backup_policy.zfs.enabled = True
    if enable_archive:
        config.backup_policy.archive.enabled = True

    orchestrator = BackupOrchestrator(config, creds)

    print_info(f"Saving backups to: {backup_root}")
    if area:
        print_info(f"  Section: {area} only")
    if no_packages:
        print_info("  Packages: excluded")
    if all_extras:
        print_info("  Extras: all targets")
    elif include_extras:
        print_info(f"  Extras: {', '.join(include_extras)}")
    elif config_only:
        print_info("  Config only (extras skipped)")
    if enable_zfs:
        print_info("  ZFS snapshot: enabled")
    if enable_archive:
        print_info("  Filesystem archive: enabled")

    if no_notify:
        config.notifications.notify_on_success = False
        config.notifications.notify_on_failure = False

    def progress(msg: str, pct: int) -> None:
        print_progress(msg, pct)

    if device:
        if not config.get_device(device):
            print_error(f"Device '{device}' not found. Run: pfs device list")
            raise typer.Exit(1)
        try:
            records = orchestrator.run(
                device,
                include_extras=include_extras,
                all_extras=all_extras,
                config_only=config_only,
                description=description,
                progress=progress,
                on_warning=print_warning,
                area=area or "",
                no_packages=no_packages,
            )
            for record in records:
                print_success(f"[{record.type_label}] {record.filename}")
                print_info(f"  Saved to: {backup_root / device / record.relative_path}")
                if not record.verified:
                    print_warning("  Post-backup validation failed: the file may be corrupt.")
                    print_warning(f"  Run: pfs backup verify {record.filename}")
            print_success(f"Backup complete: {len(records)} file(s)")
        except (BackupError, BackupIndexError) as e:
            print_error(str(e))
            raise typer.Exit(1)
        except Exception as e:
            print_error(f"Unexpected error: {e}")
            raise typer.Exit(1)
    else:
        devices = config.enabled_devices()
        if not devices:
            print_error("No enabled devices. Run: pfs device add")
            raise typer.Exit(1)

        failed = 0
        total_records = 0
        for d in devices:
            print_info(f"Backing up {d.id} ({d.host})...")
            try:
                records = orchestrator.run(
                    d.id,
                    include_extras=include_extras,
                    all_extras=all_extras,
                    config_only=config_only,
                    description=description,
                    progress=progress,
                    on_warning=print_warning,
                    area=area or "",
                    no_packages=no_packages,
                )
                for record in records:
                    print_success(f"  [{record.type_label}] {record.filename}")
                total_records += len(records)
            except Exception as e:
                print_error(f"{d.id}: {e}")
                failed += 1

        if total_records:
            print_success(f"Total: {total_records} backup(s) across {len(devices)} device(s)")
        if failed:
            raise typer.Exit(1)


@app.command("list")
def backup_list(
    device: str | None = typer.Option(None, "--device", "-d", help="Filter by device ID"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List available backups."""
    _, svc = _get_service()
    records = svc.list_backups(device)

    if not records and not output_json:
        print_info("No backups found.")
        return

    if output_json:
        data = [json.loads(r.model_dump_json()) for r in records]
        console.print_json(json.dumps(data))
        return

    print_backup_table(records, title=f"Backups{' for ' + device if device else ''}")


@app.command("history")
def backup_history(
    device: str | None = typer.Option(None, "--device", "-d", help="Filter by device ID"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="Show the newest N runs"),
    problems: bool = typer.Option(
        False, "--problems", help="Only runs that failed, failed in part or have no end"
    ),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show past backup runs, failed runs included.

    'pfs backup list' shows the files that exist. A run that failed made no
    file, so only this command shows it. The history starts with 1.0.0.
    """
    from pfsentinel.services import run_history

    entries = run_history.load(device=device, limit=limit, problems_only=problems)

    if output_json:
        console.print_json(json.dumps([e.to_dict() for e in entries]))
        return

    if not entries:
        if problems:
            print_info("No run with a problem in the history.")
        else:
            print_info("No runs in the history yet. It starts with the next 'pfs backup run'.")
        return

    print_run_history(entries)
    print_info(f"History file: {run_history.history_path()}")


@app.command("verify")
def backup_verify(
    filename: str | None = typer.Argument(
        None, help="Backup filename to verify (default: every backup not verified yet)"
    ),
    device: str | None = typer.Option(
        None, "--device", "-d", help="Without a filename: only this device"
    ),
) -> None:
    """Verify backup file integrity and save the result.

    Checks the checksum, then decrypts and reads the file (config XML or tar
    archive). The result shows in the Status column of `pfs backup list`:
    OK after a passed check, ? after a failed one. Without a filename, every
    backup with Status ? is checked, for example after `pfs backup reindex`.
    """
    config, svc = _get_service()
    if filename:
        record = next((r for r in svc.list_backups() if r.filename == filename), None)
        if not record:
            print_error(f"Backup not found: {filename}")
            raise typer.Exit(1)
        records = [record]
    else:
        if device and not config.get_device(device):
            print_error(f"Device '{device}' not found. Run: pfs device list")
            raise typer.Exit(1)
        records = [r for r in svc.list_backups(device) if not r.verified]
        if not records:
            print_success("Every backup is verified, nothing to check")
            return

    failed = 0
    for record in records:
        print_info(f"Verifying {record.filename}...")
        try:
            svc.verify_backup(record)
        except BackupError as e:
            print_error(f"Backup integrity check FAILED: {e}")
            failed += 1
            ok = False
        else:
            print_success("Backup integrity verified OK")
            ok = True
        try:
            svc.save_verified(record, ok)
        except (OSError, LockTimeoutError) as e:
            # LockTimeoutError: a long backup run holds the device's index.
            print_warning(f"  Could not save the result in the backup index: {e}")

    if len(records) > 1:
        summary = f"{len(records) - failed} of {len(records)} backups verified OK"
        (print_error if failed else print_success)(summary)
    if failed:
        raise typer.Exit(1)


@app.command("delete")
def backup_delete(
    filename: str = typer.Argument(..., help="Backup filename to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a backup file."""
    _, svc = _get_service()
    records = svc.list_backups()
    record = next((r for r in records if r.filename == filename), None)

    if not record:
        print_error(f"Backup not found: {filename}")
        raise typer.Exit(1)

    if not yes:
        confirm = typer.confirm(f"Delete {filename}?", default=False)
        if not confirm:
            print_info("Aborted.")
            return

    try:
        svc.delete_backup(record)
    except (BackupError, LockTimeoutError, OSError) as e:
        print_error(f"Could not delete {filename}: {e}")
        raise typer.Exit(1)
    print_success(f"Deleted: {filename}")


@app.command("restore")
def backup_restore(
    filename: str = typer.Argument(..., help="Backup filename to restore"),
    target: Path = typer.Option(Path("."), "--target", "-t", help="Target path or directory"),
) -> None:
    """Restore a backup to a local path."""
    _, svc = _get_service()
    records = svc.list_backups()
    record = next((r for r in records if r.filename == filename), None)

    if not record:
        print_error(f"Backup not found: {filename}")
        raise typer.Exit(1)

    try:
        dest = svc.restore_backup(record, target)
        print_success(f"Restored to: {dest}")
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("check")
def backup_check(
    filename: str | None = typer.Argument(None, help="Config backup filename (default: latest)"),
    device: str | None = typer.Option(None, "--device", "-d", help="Device ID (with no filename)"),
) -> None:
    """Restore check: would this config backup restore a working firewall?

    Parses config.xml and checks for users, an admin user, interfaces and the
    firewall filter section. Encrypted backups are decrypted in memory.
    """
    from pfsentinel.models.backup import BackupType
    from pfsentinel.services.config_checks import restore_check

    _, svc = _get_service()
    configs = [r for r in svc.list_backups(device) if r.backup_type == BackupType.CONFIG]
    if filename:
        configs = [r for r in configs if r.filename == filename]
    if not configs:
        print_error(f"Backup not found: {filename}" if filename else "No config backups found")
        raise typer.Exit(1)
    record = configs[0]
    try:
        result = restore_check(svc.read_xml(svc.backup_path(record)))
    except Exception as e:
        print_error(f"Cannot read {record.filename}: {e}")
        raise typer.Exit(1)
    console.print(f"{record.filename}")
    if result.ok:
        print_success(f"Restore check {result.summary}")
    else:
        print_error(f"Restore check {result.summary}")
        raise typer.Exit(1)


@app.command("check-recent")
def backup_check_recent(
    days: int | None = typer.Option(
        None, "--days", help="Alert after this many days (default: notifications.stale_after_days)"
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="For scheduled runs (this command never prompts)"
    ),
) -> None:
    """Alert when a device has had no successful backup for some days.

    Scheduled at sign-in and daily by 'pfs schedule enable': a PC that was
    off, or a schedule that stopped, gives no error on its own. With the
    daily digest on, the first run at or after its time also sends the digest.
    """
    from datetime import datetime, timedelta

    from pfsentinel.models.backup import BackupType
    from pfsentinel.services.notifications import AlertKind

    config, svc = _get_service()
    limit = days or config.notifications.stale_after_days
    cutoff = datetime.now() - timedelta(days=limit)
    stale = 0
    for device in config.enabled_devices():
        try:
            records = svc.list_backups(device.id)
        except BackupIndexError as e:
            # Same alert kind as a missing backup (event 1003): the check
            # cannot confirm a recent backup, and backups of this device stop.
            stale += 1
            print_error(str(e))
            svc.notifications.alert(
                AlertKind.NO_RECENT_BACKUP,
                f"pfSentinel - Backup index of {device.id} cannot be read",
                f"Device: {device.id}\nThe backup index cannot be read ({e.reason}), so the "
                f"last backup is unknown and new backups of this device stop.\n"
                f"File: {e.path}\nRebuild it: pfs backup reindex -d {device.id}",
            )
            continue
        latest = next((r for r in records if r.backup_type == BackupType.CONFIG), None)
        if latest is not None and latest.created_at >= cutoff:
            print_success(f"{device.id}: last backup {latest.created_at:%Y-%m-%d %H:%M}")
            continue
        stale += 1
        when = f"{latest.created_at:%Y-%m-%d %H:%M}" if latest else "never"
        print_warning(f"{device.id}: no backup in {limit} day(s) (last: {when})")
        svc.notifications.alert(
            AlertKind.NO_RECENT_BACKUP,
            f"pfSentinel - No recent backup of {device.id}",
            f"Device: {device.id}\nLast successful backup: {when}\n"
            f"Limit: {limit} day(s). Check that the PC was on and run: pfs schedule status",
        )
    if config.notifications.update_alerts:
        _alert_new_release(svc)
    if config.notifications.digest_enabled:
        _send_digest_if_due(config, svc)
    if stale:
        raise typer.Exit(1)


def _send_digest_if_due(config: AppConfig, svc: BackupService) -> None:
    """Send the daily digest on the first check run of the day at or after its time."""
    from pfsentinel.services.digest import send_digest_if_due

    try:
        results = send_digest_if_due(config, svc.list_backups, svc.notifications)
    except OSError as e:  # the state file could not be written
        print_warning(f"Daily digest not sent: {e}")
        return
    if results is None:
        return
    failed = [channel for channel, err in results.items() if err]
    if results and len(failed) == len(results):
        print_warning(f"Daily digest failed on every channel ({', '.join(failed)})")
    else:
        print_info("Daily digest sent")


def _alert_new_release(svc: BackupService) -> None:
    """Alert once per new release. Any error here is ignored: it is a courtesy."""
    from pfsentinel.services.notifications import AlertKind
    from pfsentinel.services.updater import UpdateService, update_command

    try:
        updater = UpdateService()
        result = updater.check(force=True)
        if result is None or updater.state.get("alerted_version") == result["latest"]:
            return
        print_info(f"Update available: v{result['latest']} (current: v{result['current']})")
        svc.notifications.alert(
            AlertKind.UPDATE_AVAILABLE,
            f"pfSentinel - Update available: v{result['latest']}",
            f"Installed: v{result['current']}\nRelease notes: {result['release_url']}\n"
            f"Update with: {update_command(result.get('install_method', ''))}",
        )
        updater.state["alerted_version"] = result["latest"]
        updater.save_state()
    except Exception as e:  # no network, rate limit, ...
        from pfsentinel.utils.logging import get_logger

        get_logger(__name__).debug(f"Update check skipped: {e}")


def _read_identity_file(path: Path) -> list[str]:
    """age identities (AGE-SECRET-KEY-1...) from an age-keygen file."""
    from pfsentinel.services import age_format
    from pfsentinel.services.backup_crypto import BackupCryptoError

    try:
        text = path.read_text(encoding="utf-8")
        return [str(i) for i in age_format.parse_identity_file(text)]
    except (OSError, UnicodeDecodeError) as e:
        raise BackupCryptoError(f"Cannot read the identity file: {e}") from e
    except age_format.AgeError as e:
        raise BackupCryptoError(str(e)) from e


@app.command("decrypt")
def backup_decrypt(
    file: Path = typer.Argument(..., help="Encrypted backup file (.enc)"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Output file (default: next to it, without .enc)"
    ),
    recovery_key: bool = typer.Option(
        False, "--recovery-key", help="Ask for the recovery key (keystore mode, other PC)"
    ),
    passphrase: bool = typer.Option(
        False, "--passphrase", help="Ask for the passphrase (passphrase mode, other PC)"
    ),
    identity: bool = typer.Option(
        False, "--identity", help="Ask for an age identity, AGE-SECRET-KEY-1... (age mode)"
    ),
    identity_file: Path | None = typer.Option(
        None, "--identity-file", help="Read age identities from a file, as made by age-keygen"
    ),
    decompress: bool = typer.Option(False, "--decompress", help="Also un-gzip the result"),
    passphrase_stdin: bool = typer.Option(
        False, "--passphrase-stdin", help="Read the passphrase from the first line of stdin"
    ),
) -> None:
    """Decrypt one backup file. Works without the backup index, for example on a new PC."""
    from pfsentinel.services import backup_crypto
    from pfsentinel.services.backup_crypto import BackupCrypto, BackupCryptoError

    file = file.expanduser()
    if not file.is_file() or not backup_crypto.is_encrypted(file):
        print_error(f"Not an encrypted backup file (.enc): {file}")
        raise typer.Exit(1)
    key_text = typer.prompt("Recovery key", hide_input=True) if recovery_key else None
    if passphrase_stdin:
        from pfsentinel.utils.stdin import read_secret_line

        phrase: str | None = read_secret_line()
    else:
        phrase = typer.prompt("Passphrase", hide_input=True) if passphrase else None
    identities = [typer.prompt("age identity", hide_input=True)] if identity else []

    config = AppConfig.load()
    try:
        if identity_file is not None:
            identities += _read_identity_file(identity_file.expanduser())
        crypto = BackupCrypto(
            config.backup_policy.encryption,
            CredentialService(),
            recovery_key=key_text,
            passphrase=phrase,
            identities=identities,
        )
    except BackupCryptoError as e:
        print_error(str(e))
        raise typer.Exit(1)

    name = backup_crypto.plain_name(file.name)
    unzip = decompress and name.endswith(".gz")
    if unzip:
        name = name.removesuffix(".gz")
    target = output.expanduser() if output else file.with_name(name)
    if target.exists():
        print_error(f"Refusing to overwrite: {target}")
        raise typer.Exit(1)
    # Streamed in 1 MiB steps: a ZFS send stream can be several GB. A file
    # that fails half way (wrong key, cut short) leaves no partial copy.
    from pfsentinel.services.backup import write_plain_copy

    try:
        with crypto.open_read(file) as raw:
            stream = cast(IO[bytes], gzip.GzipFile(fileobj=raw)) if unzip else raw
            write_plain_copy(stream, target)
    except (BackupCryptoError, OSError, EOFError) as e:
        print_error(f"Could not decrypt {file.name}: {e}")
        raise typer.Exit(1)
    print_success(f"Decrypted to: {target}")
    print_warning("This copy is NOT encrypted. Delete it when you are done.")


@app.command("diff")
def backup_diff(
    file_a: str = typer.Argument(..., help="First backup filename"),
    file_b: str = typer.Argument(..., help="Second backup filename"),
) -> None:
    """Show unified diff between two backups."""
    _, svc = _get_service()
    diff_svc = svc.diff_service
    records = svc.list_backups()

    def find_record(name: str) -> BackupRecord:
        r = next((r for r in records if r.filename == name), None)
        if not r:
            print_error(f"Backup not found: {name}")
            raise typer.Exit(1)
        return r

    rec_a = find_record(file_a)
    rec_b = find_record(file_b)

    diff = diff_svc.generate_text_diff(rec_a, rec_b)
    if not diff:
        print_info("No differences found between the two backups.")
    else:
        # Plain text: XML descriptions may hold "[...]", which Rich reads as markup.
        console.print(diff, markup=False, highlight=False)


@app.command("info")
def backup_info(
    filename: str = typer.Argument(..., help="Backup filename"),
) -> None:
    """Show detailed info about a backup."""
    _, svc = _get_service()
    records = svc.list_backups()
    record = next((r for r in records if r.filename == filename), None)

    if not record:
        print_error(f"Backup not found: {filename}")
        raise typer.Exit(1)

    print_record_detail(record)


@app.command("search")
def backup_search(
    name: str | None = typer.Option(None, "--name", "-n", help="Filename substring"),
    device: str | None = typer.Option(None, "--device", "-d", help="Filter by device ID"),
    date: str | None = typer.Option(None, "--date", help="Filter by date (YYYY-MM-DD)"),
    changes: str | None = typer.Option(None, "--changes", help="Filter by changes label substring"),
    min_size: int | None = typer.Option(None, "--min-size", help="Minimum size in KB"),
    max_size: int | None = typer.Option(None, "--max-size", help="Maximum size in KB"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Search backups by name, date, size, or change type."""
    _, svc = _get_service()
    records = svc.list_backups(device)

    if name:
        records = [r for r in records if name.lower() in r.filename.lower()]
    if date:
        records = [r for r in records if r.created_at.strftime("%Y-%m-%d") == date]
    if changes:
        records = [r for r in records if changes.lower() in r.changes_label.lower()]
    if min_size is not None:
        records = [r for r in records if r.size_bytes >= min_size * 1024]
    if max_size is not None:
        records = [r for r in records if r.size_bytes <= max_size * 1024]

    if not records:
        print_info("No matching backups found.")
        return

    if output_json:
        data = [json.loads(r.model_dump_json()) for r in records]
        console.print_json(json.dumps(data))
        return

    print_backup_table(records, title=f"Search Results ({len(records)} found)")


@app.command("watch")
def backup_watch(
    device: str | None = typer.Option(None, "--device", "-d", help="Device ID (default: all)"),
    interval: int = typer.Option(
        300, "--interval", "-i", help="Poll interval in seconds (default: 300)"
    ),
    description: str | None = typer.Option(
        None, "--desc", help="Description for triggered backups"
    ),
) -> None:
    """Watch pfSense config for changes and auto-backup when changed.

    Polls the device at the specified interval and triggers a backup
    whenever the configuration checksum differs from the last known state.
    Press Ctrl+C to stop.
    """
    import hashlib
    import time

    config = AppConfig.load()
    creds = CredentialService()

    devices_to_watch = []
    if device:
        dev = config.get_device(device)
        if not dev:
            print_error(f"Device '{device}' not found.")
            raise typer.Exit(1)
        devices_to_watch = [dev]
    else:
        devices_to_watch = config.enabled_devices()
        if not devices_to_watch:
            print_error("No enabled devices configured.")
            raise typer.Exit(1)

    from pfsentinel.services.connection import ConnectionManager

    # Track last known hash per device
    last_hashes: dict[str, str] = {}
    svc = BackupService(config, creds)

    names = ", ".join(d.id for d in devices_to_watch)
    print_info(f"Watching {names} (interval: {interval}s). Press Ctrl+C to stop.")
    console.print()

    try:
        while True:
            for dev in devices_to_watch:
                try:
                    cm = ConnectionManager(dev, creds)
                    xml, _ = cm.download_config()
                    current_hash = hashlib.sha256(xml.encode()).hexdigest()

                    prev = last_hashes.get(dev.id)
                    if prev is None:
                        last_hashes[dev.id] = current_hash
                        print_info(f"[{dev.id}] Initial hash captured. Watching for changes...")
                    elif current_hash != prev:
                        print_warning(f"[{dev.id}] Config changed! Triggering backup...")
                        record = svc.run_backup(
                            dev.id, description=description or "watch-triggered"
                        )
                        print_success(f"[{dev.id}] Backup saved: {record.filename}")
                        last_hashes[dev.id] = current_hash
                    else:
                        console.print(
                            f"[dim][{dev.id}] No change at "
                            f"{__import__('datetime').datetime.now().strftime('%H:%M:%S')}[/]"
                        )
                except Exception as e:
                    print_error(f"[{dev.id}] Poll error: {e}")

            time.sleep(interval)
    except KeyboardInterrupt:
        print_info("Watch stopped.")


@app.command("snapshot")
def backup_snapshot(
    device: str = typer.Option(..., "--device", "-d", help="Device ID"),
    full: bool = typer.Option(False, "--full", help="Force full snapshot (skip incremental)"),
) -> None:
    """Create a ZFS snapshot backup of a pfSense device."""
    from pfsentinel.services.zfs_backup import ZfsBackupService, ZfsError

    config = AppConfig.load()
    if not config.get_device(device):
        print_error(f"Device '{device}' not found. Run: pfs device list")
        raise typer.Exit(1)

    creds = CredentialService()
    zfs_svc = ZfsBackupService(config, creds)

    def progress(msg: str, pct: int) -> None:
        print_progress(msg, pct)

    from pfsentinel.services.retention import RetentionService
    from pfsentinel.utils.locking import LockTimeoutError

    retention = RetentionService(config.backup_policy.resolved_root, config.backup_policy)
    try:
        # Same lock as a backup run: both change the device's index.
        with retention.device_lock(device):
            record = zfs_svc.run_snapshot_backup(device, progress=progress, force_full=full)
            index = retention.load_index(device)
            index.add(record)
            retention.save_index(index)

        label = "full" if not record.zfs_incremental else "incremental"
        print_success(f"ZFS snapshot ({label}): {record.filename}")
        print_info(f"  Size: {record.size_human}")
        print_info(f"  Snapshot: {record.zfs_snapshot_name}")
        if record.zfs_base_snapshot:
            print_info(f"  Base: {record.zfs_base_snapshot}")
    except ZfsError as e:
        print_error(f"ZFS snapshot failed: {e}")
        raise typer.Exit(1)
    except (BackupIndexError, LockTimeoutError) as e:
        print_error(str(e))
        raise typer.Exit(1)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        raise typer.Exit(1)


@app.command("snapshot-list")
def snapshot_list(
    device: str = typer.Option(..., "--device", "-d", help="Device ID"),
) -> None:
    """List ZFS snapshots for a device."""
    from pfsentinel.services.zfs_backup import ZfsBackupService

    config = AppConfig.load()
    creds = CredentialService()
    zfs_svc = ZfsBackupService(config, creds)
    snap_index = zfs_svc.load_snapshot_index(device)

    if not snap_index.snapshots:
        print_info("No ZFS snapshots found.")
        return

    from rich.table import Table

    table = Table(title=f"ZFS Snapshots — {device}", header_style="bold cyan")
    table.add_column("Snapshot", style="cyan")
    table.add_column("Date", style="green")
    table.add_column("Transferred")
    table.add_column("Size", justify="right")

    for s in sorted(snap_index.snapshots, key=lambda s: s.created_at, reverse=True):
        transferred = "[green]Yes[/]" if s.transferred else "[yellow]No[/]"
        size = f"{s.size_bytes / 1024:.1f} KB" if s.size_bytes else "-"
        table.add_row(
            s.name,
            s.created_at.strftime("%Y-%m-%d %H:%M"),
            transferred,
            size,
        )

    console.print(table)


@app.command("archive")
def backup_archive(
    device: str = typer.Option(..., "--device", "-d", help="Device ID"),
    dirs: str | None = typer.Option(None, "--dirs", help="Comma-separated directories to archive"),
) -> None:
    """Create a filesystem tar archive backup of a pfSense device."""
    from pfsentinel.services.archive_backup import ArchiveBackupError, ArchiveBackupService

    config = AppConfig.load()
    if not config.get_device(device):
        print_error(f"Device '{device}' not found. Run: pfs device list")
        raise typer.Exit(1)

    directories = None
    if dirs:
        directories = [d.strip() for d in dirs.split(",")]
        for d in directories:
            if not d.startswith("/"):
                print_error(f"Directory must be an absolute path: {d}")
                raise typer.Exit(1)
            if ".." in d.split("/"):
                print_error(f"Directory must not contain '..': {d}")
                raise typer.Exit(1)

    creds = CredentialService()
    archive_svc = ArchiveBackupService(config, creds)

    def progress(msg: str, pct: int) -> None:
        print_progress(msg, pct)

    try:
        record = archive_svc.run_archive_backup(device, directories=directories, progress=progress)

        # Add to backup index, under the device lock like a backup run.
        from pfsentinel.services.retention import RetentionService

        retention = RetentionService(config.backup_policy.resolved_root, config.backup_policy)
        with retention.device_lock(device):
            index = retention.load_index(device)
            index.add(record)
            retention.save_index(index)

        print_success(f"Archive backup: {record.filename}")
        print_info(f"  Size: {record.size_human}")
        print_info(f"  Directories: {', '.join(record.source_paths)}")
    except ArchiveBackupError as e:
        print_error(f"Archive backup failed: {e}")
        raise typer.Exit(1)
    except BackupIndexError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        raise typer.Exit(1)


@app.command("reindex")
def backup_reindex(
    device: str | None = typer.Option(None, "--device", "-d", help="Device ID (default: all)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only show what would change"),
) -> None:
    """Rebuild the backup index from the backup files on disk (recovery).

    Keeps every record of a readable index and adds the files it is missing.
    An unreadable index is moved aside as backup_index.json.broken-<time>,
    never deleted. Backup files are only read, never changed or deleted.
    """
    from pfsentinel.services.reindex import ReindexError, reindex_device
    from pfsentinel.services.retention import RetentionService

    config = AppConfig.load()
    if device and not config.get_device(device):
        print_error(f"Device '{device}' not found. Run: pfs device list")
        raise typer.Exit(1)
    retention = RetentionService(config.backup_policy.resolved_root, config.backup_policy)
    failed = False
    for device_id in [device] if device else [d.id for d in config.devices]:
        try:
            result = reindex_device(retention, device_id, dry_run=dry_run)
        except ReindexError as e:
            print_error(f"{device_id}: {e}")
            failed = True
            continue
        except (OSError, LockTimeoutError) as e:
            print_error(f"{device_id}: reindex failed, nothing changed: {e}")
            failed = True
            continue

        if result.broken_reason:
            print_warning(f"{device_id}: the backup index cannot be read ({result.broken_reason})")
            moved = "Would move" if dry_run else "Moved"
            print_info(f"  {moved} it aside to: {result.moved_to}")
        for record in result.added:
            print_info(f"  {'Would add' if dry_run else 'Added'}: {record.relative_path}")
        for rel in result.skipped_unsafe:
            print_warning(f"  Skipped (a link or outside the device folder): {rel}")
        for rel in result.unreadable:
            print_error(f"{device_id}: could not read {rel}")
            failed = True
        counts = f"{len(result.added)} to add, {result.kept} kept"
        next_run = f"next run #{result.last_run_number + 1:03d}"
        if dry_run:
            print_info(f"{device_id}: {counts}, {next_run}. Dry run: nothing changed.")
        elif result.saved:
            print_success(f"{device_id}: {len(result.added)} added, {result.kept} kept, {next_run}")
        else:
            print_success(f"{device_id}: nothing to add, the index is up to date")
    if failed:
        raise typer.Exit(1)
