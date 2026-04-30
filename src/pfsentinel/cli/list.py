"""pfs list — show configured devices."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from pfsentinel.utils.config_io import load_config

app = typer.Typer(help="List configured devices.")
console = Console()


@app.callback(invoke_without_command=True)
def list_devices() -> None:
    """Display all configured pfSense devices."""
    cfg = load_config()

    if not cfg.devices:
        console.print("[yellow]No devices configured.[/yellow]")
        raise typer.Exit()

    table = Table(title="Configured Devices", show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("Name", style="cyan")
    table.add_column("Host")
    table.add_column("Port", justify="right")
    table.add_column("User")
    table.add_column("Auth")
    table.add_column("Backup Types")

    for i, dev in enumerate(cfg.devices, 1):
        table.add_row(
            str(i),
            dev.name,
            dev.hostname,
            str(dev.ssh_port),
            dev.username,
            dev.auth_method,
            ", ".join(dev.backup_types),
        )

    console.print(table)
