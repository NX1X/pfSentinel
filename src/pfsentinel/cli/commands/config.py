"""CLI commands for configuration management."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.panel import Panel
from rich.syntax import Syntax

from pfsentinel.cli.formatters import console, print_error, print_info, print_success
from pfsentinel.models.config import AppConfig

app = typer.Typer(help="Configuration management", no_args_is_help=True)


@app.command("show")
def config_show() -> None:
    """Show current configuration (passwords hidden)."""
    config = AppConfig.load()
    config_path = AppConfig.config_path()

    # Build safe display dict (no passwords)
    data = json.loads(config.model_dump_json())

    # Show resolved backup_root (with default if not explicitly set)
    if "backup_policy" in data:
        data["backup_policy"]["backup_root"] = str(config.backup_policy.resolved_root)
        if config.backup_policy.backup_root is None:
            data["backup_policy"]["backup_root"] += "  (default - not yet configured)"

    json_str = json.dumps(data, indent=2)
    syntax = Syntax(json_str, "json", theme="monokai", line_numbers=False)
    console.print(Panel(syntax, title=f"Config: {config_path}", border_style="cyan"))


@app.command("init")
def config_init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config"),
) -> None:
    """Create a default configuration file."""
    config_path = AppConfig.config_path()

    if config_path.exists() and not force:
        print_info(f"Config already exists at {config_path}")
        print_info("Use --force to overwrite")
        return

    config = AppConfig()

    default_root = config.backup_policy.resolved_root
    console.print(f"[dim]Default backup location: {default_root}[/]")
    raw = typer.prompt("Where should backups be saved?", default=str(default_root))
    config.backup_policy.backup_root = Path(raw).expanduser().resolve()

    config.save()
    print_success(f"Config created at {config_path}")
    print_info(f"Backup location: {config.backup_policy.backup_root}")
    print_info("Next step: pfs device add")


@app.command("validate")
def config_validate() -> None:
    """Validate configuration and check credential availability."""
    from pfsentinel.services.credentials import CredentialService

    config_path = AppConfig.config_path()
    if not config_path.exists():
        print_error(f"Config file not found at {config_path}")
        print_info("Run: pfs config init")
        raise typer.Exit(1)

    try:
        config = AppConfig.load()
        print_success("Config file is valid")
    except Exception as e:
        print_error(f"Config file is invalid: {e}")
        raise typer.Exit(1)

    if not config.devices:
        print_info("No devices configured")
        return

    creds = CredentialService()
    missing_password = False
    missing_key = False
    for device in config.devices:
        # A configured key is checked first: a stored password does not make a
        # missing key file work. A device with a key needs no password (SSH
        # only, no HTTPS fallback).
        if device.ssh_key_path is not None:
            if device.ssh_key_path.expanduser().is_file():
                status = "[green]✓ SSH key[/]"
            else:
                status = f"[red]✗ SSH key file not found: {device.ssh_key_path}[/]"
                missing_key = True
        elif creds.has_password(device.id):
            status = "[green]✓[/]"
        else:
            status = "[red]✗ missing password[/]"
            missing_password = True
        console.print(f"  Device [cyan]{device.id}[/]: {status}")

    if missing_password:
        print_info("Run: pfs device add  (or device edit) to set passwords")
    if missing_key:
        print_info("Run: pfs device edit  to fix the SSH key path")


@app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Setting key: backup-dir"),
    value: str = typer.Argument(..., help="New value"),
) -> None:
    """Change a configuration setting.

    Supported keys:
      backup-dir   Path where backups are saved
    """
    config = AppConfig.load()

    if key == "backup-dir":
        new_path = Path(value).expanduser().resolve()
        old_path = config.backup_policy.backup_root or config.backup_policy.resolved_root
        config.backup_policy.backup_root = new_path
        config.save()
        print_success(f"Backup directory changed: {old_path} → {new_path}")
        if not new_path.exists():
            print_info("Directory will be created on next backup run")
    else:
        print_error(f"Unknown setting '{key}'. Supported: backup-dir")
        raise typer.Exit(1)


@app.command("path")
def config_path() -> None:
    """Print the config file path."""
    console.print(str(AppConfig.config_path()))
