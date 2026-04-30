"""pfSentinel CLI application."""

from __future__ import annotations

from pathlib import Path

import typer

from pfsentinel import __version__
from pfsentinel.cli.commands.backup import app as backup_app
from pfsentinel.cli.commands.config import app as config_app
from pfsentinel.cli.commands.device import app as device_app
from pfsentinel.cli.commands.notify import app as notify_app
from pfsentinel.cli.commands.schedule import app as schedule_app
from pfsentinel.cli.commands.update import app as update_app

app = typer.Typer(
    name="pfs",
    help="pfSentinel - pfSense Backup Automation Tool",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

app.add_typer(backup_app, name="backup")
app.add_typer(config_app, name="config")
app.add_typer(device_app, name="device")
app.add_typer(notify_app, name="notify")
app.add_typer(schedule_app, name="schedule")
app.add_typer(update_app, name="update")


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-v", help="Show version"),
) -> None:
    """pfSentinel - pfSense Backup Automation Tool."""
    if version:
        typer.echo(f"pfSentinel v{__version__}")
        raise typer.Exit()


@app.command("status")
def status() -> None:
    """Show overview of devices, backups, and configuration."""
    from rich.console import Console
    from rich.table import Table

    from pfsentinel.models.config import AppConfig

    console = Console()

    console.print()
    console.print("[bold cyan]pfSentinel Status[/]")
    console.print("[dim]─────────────────────────────────────────────[/]")
    console.print()

    try:
        config = AppConfig.load()

        console.print(f"  Config: {AppConfig.config_path()}")
        if config.backup_policy.backup_root:
            console.print(f"  Backup dir: {config.backup_policy.backup_root}")
        console.print()

        if not config.devices:
            console.print("[yellow]No devices configured.[/]")
            console.print("Run: [bold]pfs setup[/] or [bold]pfs device add[/]")
            return

        # Devices table
        table = Table(title="Devices", header_style="bold cyan")
        table.add_column("ID", style="cyan")
        table.add_column("Label")
        table.add_column("Host")
        table.add_column("Method")
        table.add_column("User")

        for d in config.devices:
            table.add_row(d.id, d.label, d.host, d.primary_method.value, d.username)

        console.print(table)
        console.print()

        # Backup summary
        if config.backup_policy.backup_root:
            root = Path(config.backup_policy.backup_root)
            if root.exists():
                xml_count = len(list(root.rglob("*.xml*")))
                console.print(f"  Total backups on disk: {xml_count}")
            else:
                console.print(f"  Backup directory not found: {root}")

    except Exception as exc:
        console.print(f"[red]Error: {exc}[/]")


@app.command("list")
def list_backups(
    device: str | None = typer.Option(None, "--device", "-d", help="Filter by device name"),
) -> None:
    """List available backups (shortcut for 'pfs backup list')."""
    from pfsentinel.cli.commands.backup import backup_list as _backup_list

    _backup_list(device=device, output_json=False)


@app.command("setup")
def setup() -> None:
    """Guided first-time setup: create config and add your first device."""
    import re

    from rich.console import Console

    from pfsentinel.models.config import AppConfig
    from pfsentinel.models.device import ConnectionMethod, DeviceConfig
    from pfsentinel.services.credentials import CredentialService

    console = Console()

    console.print()
    console.print("[bold cyan]pfSentinel Setup[/]")
    console.print("[dim]─────────────────────────────────────────────[/]")
    console.print()

    config = AppConfig.load()
    creds = CredentialService()

    if config.devices:
        console.print(f"[green]Config already exists with {len(config.devices)} device(s).[/]")
        console.print()
        console.print("[bold cyan]Next Steps[/]")
        console.print("[dim]─────────────────────────────────────────────[/]")
        console.print("  pfs backup run -d <device>  Run a backup")
        console.print("  pfs device list             List configured devices")
        console.print("  pfs device add              Add another device")
        console.print("  pfs status                  View system overview")
        console.print()
        return

    # --- Collect device info ---
    host = typer.prompt("pfSense IP address or hostname")
    suggested_id = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:62] or "pfsense"
    device_id = typer.prompt("Device ID (short slug for commands)", default=suggested_id)
    label = typer.prompt("Display name", default=device_id)
    username = typer.prompt("pfSense username", default="admin")

    console.print()
    console.print("[dim]Connection methods:[/]")
    console.print("[dim]  ssh   - Recommended. Reads config via SSH/SFTP.[/]")
    console.print("[dim]  https - Uses web GUI over HTTPS.[/]")
    console.print("[dim]  http  - Same as https without TLS (not recommended).[/]")
    method = typer.prompt("Connection method", default="ssh")

    try:
        conn_method = ConnectionMethod(method.lower().strip())
    except ValueError:
        console.print(f"[red]Invalid method '{method}'. Choose: ssh, https, http[/]")
        raise typer.Exit(1)

    # --- Authentication ---
    ssh_key_path: Path | None = None
    if conn_method == ConnectionMethod.SSH:
        console.print()
        ssh_key_raw = typer.prompt(
            "SSH private key path (leave blank for password auth)", default=""
        )
        if ssh_key_raw.strip():
            ssh_key_path = Path(ssh_key_raw.strip()).expanduser()

    console.print()
    if ssh_key_path:
        password = typer.prompt(
            "Password (for HTTPS fallback, or Enter to skip)", hide_input=True, default=""
        )
    else:
        password = typer.prompt("Password", hide_input=True)

    # --- Port ---
    if conn_method == ConnectionMethod.SSH:
        ssh_port = int(typer.prompt("SSH port", default="22"))
    else:
        ssh_port = 22
    if conn_method == ConnectionMethod.HTTPS:
        https_port = int(typer.prompt("HTTPS port", default="443"))
    else:
        https_port = 443

    # --- Save ---
    try:
        device = DeviceConfig(
            id=device_id,
            label=label,
            host=host,
            primary_method=conn_method,
            username=username,
            ssh_port=ssh_port,
            https_port=https_port,
            verify_ssl=True,
            ssh_key_path=ssh_key_path,
        )
        config.add_device(device)
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)

    config.save()

    if password:
        creds.store(device_id, password)
        if not creds.is_persistent:
            console.print()
            console.print("[yellow]Warning: No persistent keyring found.[/]")
            console.print("[yellow]Password will be lost when process exits.[/]")
            console.print(
                "[dim]Install keyrings.alt for persistent storage: pip install keyrings.alt[/]"
            )

    if ssh_key_path:
        passphrase = typer.prompt(
            "SSH key passphrase (Enter if unencrypted)", hide_input=True, default=""
        )
        if passphrase:
            creds.store_ssh_key_passphrase(device_id, passphrase)

    console.print(f"\n[green]Device '{device_id}' added successfully.[/]")
    console.print(f"[dim]Config saved to {AppConfig.config_path()}[/]")

    console.print()
    console.print("[bold cyan]Next Steps[/]")
    console.print("[dim]─────────────────────────────────────────────[/]")
    console.print(f"  pfs backup run -d {device_id}  Run your first backup")
    console.print("  pfs device add               Add another device")
    console.print("  pfs status                   View system overview")
    console.print()
    return None


def main_entry() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main_entry()
