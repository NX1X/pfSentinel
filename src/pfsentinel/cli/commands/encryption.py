"""CLI commands for backup encryption at rest."""

from __future__ import annotations

import typer

from pfsentinel.cli.formatters import console, print_error, print_info, print_success, print_warning
from pfsentinel.models.config import AppConfig
from pfsentinel.services.backup_crypto import MIN_PASSPHRASE, BackupCrypto, BackupCryptoError
from pfsentinel.services.credentials import CredentialService

app = typer.Typer(help="Encrypt backups at rest", no_args_is_help=True)


def _load() -> tuple[AppConfig, CredentialService, BackupCrypto]:
    config = AppConfig.load()
    creds = CredentialService()
    return config, creds, BackupCrypto(config.backup_policy.encryption, creds)


@app.command("setup")
def encryption_setup(
    mode: str = typer.Option(
        None, "--mode", help="keystore (random key + recovery key) or passphrase"
    ),
) -> None:
    """Turn on encryption for new backups, or change the key."""
    config, creds, crypto = _load()
    policy = config.backup_policy.encryption

    if not creds.is_persistent:
        print_error("This machine has no persistent keystore, so the key would be lost on exit.")
        raise typer.Exit(1)

    if policy.mode != "off":
        print_warning(
            f"Encryption is already on ({policy.mode}). A new key applies to new backups only; "
            "the old key stays in the keystore, so older backups remain readable."
        )
        if not typer.confirm("Set up a new key?", default=False):
            raise typer.Exit(0)

    if not mode:
        console.print()
        console.print(
            "[bold]keystore[/]   A random key in this PC's keystore (Credential Manager)."
        )
        console.print("           You get a recovery key ONCE: store it in your password manager.")
        console.print("[bold]passphrase[/] The key comes from a passphrase you choose.")
        console.print(
            "           It is also kept in the keystore, so scheduled runs need no input."
        )
        console.print()
        mode = typer.prompt("Mode", default="keystore")
    mode = mode.strip().lower()

    try:
        if mode == "keystore":
            recovery = crypto.setup_keystore()
            console.print()
            console.print(
                "[bold]Recovery key[/] (shown once; it decrypts every backup with this key):"
            )
            console.print()
            console.print(f"    [bold yellow]{recovery}[/]")
            console.print()
            print_warning("Without this key, a dead PC or a lost keystore means lost backups.")
            last = recovery.rsplit("-", 1)[-1]
            while (
                typer.prompt("Type the last group of 4 characters to confirm you saved it")
                .strip()
                .upper()
                != last
            ):
                print_warning("That does not match. Check the key you saved.")
        elif mode == "passphrase":
            while True:
                passphrase = typer.prompt(
                    f"Passphrase (at least {MIN_PASSPHRASE} characters)",
                    hide_input=True,
                    confirmation_prompt=True,
                )
                if len(passphrase) >= MIN_PASSPHRASE:
                    break
                print_warning(f"Use at least {MIN_PASSPHRASE} characters.")
            crypto.setup_passphrase(passphrase)
            print_warning("Store the passphrase in your password manager too.")
        else:
            print_error("Mode must be keystore or passphrase")
            raise typer.Exit(1)
    except BackupCryptoError as e:
        print_error(str(e))
        raise typer.Exit(1)

    config.save()
    print_success(f"Encryption is on ({mode}). New backups are written as <name>.enc.")
    print_info("Existing backups are still plain. Encrypt them: pfs encryption encrypt-existing")


@app.command("status")
def encryption_status() -> None:
    """Show whether new backups are encrypted, and how."""
    config, creds, _ = _load()
    policy = config.backup_policy.encryption
    if policy.mode == "off":
        print_warning(
            "Encryption is OFF: backups are plain .xml.gz files. Run: pfs encryption setup"
        )
    else:
        print_success(f"Encryption is ON ({policy.mode}), key ID {policy.key_id}")
    console.print(f"  Keystore: {creds.backend_name()}")

    from pfsentinel.services.backup import BackupService

    svc = BackupService(config, creds)
    records = svc.list_backups()
    plain = sum(1 for r in records if not r.encrypted)
    console.print(f"  Backups: {len(records) - plain} encrypted, {plain} plain")


@app.command("recovery-key")
def encryption_recovery_key() -> None:
    """Show the recovery key of the current keystore key again."""
    _, _, crypto = _load()
    if not typer.confirm("Show the recovery key on screen?", default=False):
        raise typer.Exit(0)
    try:
        console.print(f"    [bold yellow]{crypto.recovery_key()}[/]")
    except BackupCryptoError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("disable")
def encryption_disable(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Write new backups unencrypted. Keys stay, so encrypted backups remain readable."""
    config, _, _ = _load()
    if not yes and not typer.confirm(
        "New backups will be PLAIN files with password hashes and keys. Continue?",
        default=False,
    ):
        raise typer.Exit(0)
    config.backup_policy.encryption.mode = "off"
    config.save()
    print_warning("Encryption is off for new backups. Existing .enc files stay encrypted.")


@app.command("encrypt-existing")
def encryption_encrypt_existing(
    device: str | None = typer.Option(None, "--device", "-d", help="Device ID (default: all)"),
    wipe: bool = typer.Option(
        False, "--wipe", help="Overwrite each plain file before deleting it (slow)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Encrypt plain backups made before encryption was on."""
    from pfsentinel.services.backup import BackupError, BackupService

    config, creds, crypto = _load()
    if not crypto.enabled:
        print_error("Encryption is off. Run: pfs encryption setup")
        raise typer.Exit(1)
    devices = [device] if device else [d.id for d in config.devices]
    if not yes:
        console.print("Each plain file is encrypted, read back and checked, then deleted.")
        if not (wipe or config.backup_policy.secure_delete):
            print_warning(
                "Deleted plain files may still be recoverable from the disk. Add --wipe to "
                "overwrite them first (on an SSD, the drive may still keep old copies)."
            )
        if not typer.confirm("Encrypt existing backups now?", default=True):
            raise typer.Exit(0)

    svc = BackupService(config, creds)
    total = 0
    for device_id in devices:
        try:
            count = svc.encrypt_existing(
                device_id, progress=lambda n: print_info(f"  {n}"), wipe=wipe
            )
        except BackupError as e:
            print_error(f"{device_id}: {e}")
            raise typer.Exit(1)
        total += count
    print_success(f"Encrypted {total} backup file(s)")
