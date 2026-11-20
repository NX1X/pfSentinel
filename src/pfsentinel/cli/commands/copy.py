"""CLI commands for the second copy of the encrypted backups."""

from __future__ import annotations

from pathlib import Path

import typer

from pfsentinel.cli.formatters import console, print_error, print_info, print_success, print_warning
from pfsentinel.models.config import AppConfig
from pfsentinel.services.credentials import CredentialService

app = typer.Typer(
    help="Second copy of the encrypted backups (folder or SFTP)", no_args_is_help=True
)


def _load() -> tuple[AppConfig, CredentialService]:
    return AppConfig.load(), CredentialService()


def _trust_sftp_host(host: str, port: int) -> bool:
    from pfsentinel.services import host_keys
    from pfsentinel.services.connection import HostKeyError

    try:
        key = host_keys.fetch_host_key(host, port)
    except HostKeyError as e:
        print_error(str(e))
        return False
    fp = host_keys.fingerprint(key)
    current = host_keys.trusted_key(host, port)
    if current is not None and host_keys.fingerprint(current) == fp:
        print_success(f"Host key already trusted: {key.get_name()} {fp}")
        return True
    console.print(f"  Host:        {host}:{port}")
    console.print(f"  Key type:    {key.get_name()}")
    console.print(f"  Fingerprint: [bold]{fp}[/]")
    console.print("[dim]  Compare it on the server: ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub[/]")
    if not typer.confirm("Trust this host key?", default=False):
        return False
    host_keys.trust(host, port, key)
    return True


@app.command("setup")
def copy_setup() -> None:
    """Set up the second copy target (a folder or an SFTP server)."""
    from pfsentinel.services.second_copy import SecondCopyError, open_target

    config, creds = _load()
    if config.backup_policy.encryption.mode == "off":
        print_error("Turn on encryption first: only encrypted backups leave this PC.")
        print_info("Run: pfs encryption setup")
        raise typer.Exit(1)
    policy = config.backup_policy.second_copy

    console.print("[bold]folder[/] A local path: a network share or a Nextcloud-synced folder.")
    console.print("[bold]sftp[/]   An SSH server you run, with key login.")
    kind = typer.prompt("Target", default=policy.kind).strip().lower()
    if kind == "folder":
        path = Path(typer.prompt("Folder", default=policy.path or "")).expanduser()
        if not path.is_dir():
            print_error(f"Folder does not exist: {path}")
            raise typer.Exit(1)
        policy.kind, policy.path = "folder", str(path)
    elif kind == "sftp":
        policy.kind = "sftp"
        policy.host = typer.prompt("SSH host", default=policy.host or "")
        policy.port = int(typer.prompt("SSH port", default=str(policy.port)))
        policy.username = typer.prompt("SSH user", default=policy.username or "")
        policy.key_path = typer.prompt("Private key file", default=policy.key_path or "")
        policy.path = typer.prompt("Remote folder", default=policy.path or "pfsentinel")
        passphrase = typer.prompt(
            "Key passphrase (blank if none)", hide_input=True, default="", show_default=False
        )
        if passphrase:
            creds.store_secret("__second_copy_keypass__", passphrase)
        if not _trust_sftp_host(policy.host, policy.port):
            print_error("Host key not trusted; second copy not set up.")
            raise typer.Exit(1)
    else:
        print_error("Target must be folder or sftp")
        raise typer.Exit(1)
    policy.keep_days = int(typer.prompt("Keep copies for days", default=str(policy.keep_days)))

    try:
        open_target(policy, creds).close()
    except SecondCopyError as e:
        print_error(str(e))
        raise typer.Exit(1)
    policy.enabled = True
    config.save()
    print_success("Second copy is on: after each backup, encrypted files are copied there.")
    print_info("Copy the existing encrypted backups now: pfs copy now")


@app.command("now")
def copy_now(
    device: str | None = typer.Option(None, "--device", "-d", help="Device ID (default: all)"),
) -> None:
    """Copy every encrypted backup the target does not have yet."""
    from pfsentinel.services.second_copy import SecondCopyService

    config, creds = _load()
    if not config.backup_policy.second_copy.enabled:
        print_error("Second copy is off. Run: pfs copy setup")
        raise typer.Exit(1)
    svc = SecondCopyService(config, creds)
    failed = False
    for device_id in [device] if device else [d.id for d in config.devices]:
        result = svc.sync(device_id)
        print_info(
            f"{device_id}: {len(result.copied)} copied, "
            f"{len(result.removed_days)} old day folder(s) removed"
        )
        if result.skipped_plain:
            print_warning(
                f"  {len(result.skipped_plain)} plain file(s) not copied. "
                "Encrypt them: pfs encryption encrypt-existing"
            )
        for error in result.errors:
            print_error(f"  {error}")
            failed = True
    if failed:
        raise typer.Exit(1)


@app.command("status")
def copy_status() -> None:
    """Show the second copy target."""
    config, _ = _load()
    policy = config.backup_policy.second_copy
    if not policy.enabled:
        print_warning("Second copy is OFF. Run: pfs copy setup")
        return
    where = (
        policy.path
        if policy.kind == "folder"
        else f"sftp://{policy.username}@{policy.host}:{policy.port}/{policy.path}"
    )
    print_success(f"Second copy is ON: {where}")
    console.print(f"  Kept for: {policy.keep_days} days")


@app.command("disable")
def copy_disable() -> None:
    """Stop copying. Files already on the target stay there."""
    config, _ = _load()
    config.backup_policy.second_copy.enabled = False
    config.save()
    print_success("Second copy is off. Files already copied stay on the target.")
