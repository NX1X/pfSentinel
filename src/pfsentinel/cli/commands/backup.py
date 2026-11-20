"""CLI commands for backup operations."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from pfsentinel.cli.formatters import (
    console,
    print_backup_table,
    print_error,
    print_info,
    print_progress,
    print_record_detail,
    print_success,
    print_warning,
)
from pfsentinel.models.backup import BackupRecord
from pfsentinel.models.config import AppConfig
from pfsentinel.services.backup import BackupError, BackupService
from pfsentinel.services.credentials import CredentialService
from pfsentinel.services.orchestrator import BackupOrchestrator

app = typer.Typer(help="Backup operations", no_args_is_help=True)

# Valid extra target names for --include
_VALID_EXTRAS = ["rrd", "pkg", "dhcp", "aliases", "certs", "logs"]


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


def _resolve_backup_root(config: AppConfig, cli_override: Path | None) -> Path:
    """Return the backup root to use, prompting if not yet configured.

    If the user provides a path at the prompt, offer to save it permanently.
    """
    if cli_override:
        return cli_override.expanduser().resolve()

    if config.backup_policy.backup_root is not None:
        return config.backup_policy.backup_root.expanduser()

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
    backup_root = _resolve_backup_root(config, backup_dir)
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
                    print_warning("  Post-backup validation failed — file may be corrupt.")
                    print_warning(f"  Run: pfs backup verify {record.filename}")
            print_success(f"Backup complete: {len(records)} file(s)")
        except BackupError as e:
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


@app.command("verify")
def backup_verify(
    filename: str = typer.Argument(..., help="Backup filename to verify"),
) -> None:
    """Verify backup file integrity."""
    _, svc = _get_service()
    records = svc.list_backups()
    record = next((r for r in records if r.filename == filename), None)

    if not record:
        print_error(f"Backup not found: {filename}")
        raise typer.Exit(1)

    print_info(f"Verifying {filename}...")
    try:
        svc.verify_backup(record)
        print_success("Backup integrity verified OK")
    except BackupError as e:
        print_error(f"Backup integrity check FAILED: {e}")
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

    svc.delete_backup(record)
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
    off, or a schedule that stopped, gives no error on its own.
    """
    from datetime import datetime, timedelta

    from pfsentinel.models.backup import BackupType
    from pfsentinel.services.notifications import AlertKind

    config, svc = _get_service()
    limit = days or config.notifications.stale_after_days
    cutoff = datetime.now() - timedelta(days=limit)
    stale = 0
    for device in config.enabled_devices():
        latest = next(
            (r for r in svc.list_backups(device.id) if r.backup_type == BackupType.CONFIG), None
        )
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
    if stale:
        raise typer.Exit(1)


def _alert_new_release(svc: BackupService) -> None:
    """Alert once per new release. Any error here is ignored: it is a courtesy."""
    from pfsentinel.services.notifications import AlertKind
    from pfsentinel.services.updater import UpdateService

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
            "Update with: pfs update install",
        )
        updater.state["alerted_version"] = result["latest"]
        updater.save_state()
    except Exception as e:  # no network, rate limit, ...
        from pfsentinel.utils.logging import get_logger

        get_logger(__name__).debug(f"Update check skipped: {e}")


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
    decompress: bool = typer.Option(False, "--decompress", help="Also un-gzip the result"),
) -> None:
    """Decrypt one backup file. Works without the backup index, for example on a new PC."""
    from pfsentinel.services import backup_crypto
    from pfsentinel.services.backup_crypto import BackupCrypto, BackupCryptoError
    from pfsentinel.utils import compression

    file = file.expanduser()
    if not file.is_file() or not backup_crypto.is_encrypted(file):
        print_error(f"Not an encrypted backup file (.enc): {file}")
        raise typer.Exit(1)
    key_text = typer.prompt("Recovery key", hide_input=True) if recovery_key else None
    phrase = typer.prompt("Passphrase", hide_input=True) if passphrase else None

    config = AppConfig.load()
    try:
        crypto = BackupCrypto(
            config.backup_policy.encryption,
            CredentialService(),
            recovery_key=key_text,
            passphrase=phrase,
        )
        data = crypto.read_bytes(file)
    except BackupCryptoError as e:
        print_error(str(e))
        raise typer.Exit(1)

    name = backup_crypto.plain_name(file.name)
    if decompress and name.endswith(".gz"):
        data = compression.decompress_bytes(data)
        name = name.removesuffix(".gz")
    target = output.expanduser() if output else file.with_name(name)
    if target.exists():
        print_error(f"Refusing to overwrite: {target}")
        raise typer.Exit(1)
    target.write_bytes(data)
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
        console.print(diff)


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

    try:
        record = zfs_svc.run_snapshot_backup(device, progress=progress, force_full=full)

        # Add to backup index
        from pfsentinel.services.retention import RetentionService

        retention = RetentionService(config.backup_policy.resolved_root, config.backup_policy)
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

        # Add to backup index
        from pfsentinel.services.retention import RetentionService

        retention = RetentionService(config.backup_policy.resolved_root, config.backup_policy)
        index = retention.load_index(device)
        index.add(record)
        retention.save_index(index)

        print_success(f"Archive backup: {record.filename}")
        print_info(f"  Size: {record.size_human}")
        print_info(f"  Directories: {', '.join(record.source_paths)}")
    except ArchiveBackupError as e:
        print_error(f"Archive backup failed: {e}")
        raise typer.Exit(1)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        raise typer.Exit(1)
