"""CLI commands for device management."""

from __future__ import annotations

import re
from pathlib import Path

import typer

from pfsentinel.cli.formatters import (
    console,
    print_device_table,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from pfsentinel.models.config import AppConfig
from pfsentinel.models.device import DEVICE_ID_PATTERN, ConnectionMethod, DeviceConfig
from pfsentinel.services.credentials import CredentialService

app = typer.Typer(help="Device management", no_args_is_help=True)


def _get_config_and_creds() -> tuple[AppConfig, CredentialService]:
    return AppConfig.load(), CredentialService()


def _host_to_id(host: str) -> str:
    """Generate a valid device ID slug from a host/IP string.

    Examples:
        192.168.0.1           -> 192-168-0-1
        firewall.homelab.local -> firewall-homelab-local
        pfsense-1              -> pfsense-1
    """
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower())
    slug = slug.strip("-")
    return slug[:62] if slug else "pfsense"


@app.command("add")
def device_add(
    device_id: str | None = typer.Option(None, "--id", help="Device ID slug (e.g. home-fw)"),
    label: str | None = typer.Option(None, "--label", help="Display name"),
    host: str | None = typer.Option(None, "--host", help="IP address or hostname"),
    method: str | None = typer.Option(None, "--method", help="ssh / https / http"),
    username: str | None = typer.Option(None, "--username", "-u", help="pfSense username"),
    ssh_port: int = typer.Option(22, "--ssh-port"),
    https_port: int = typer.Option(443, "--https-port"),
    http_port: int = typer.Option(80, "--http-port"),
    no_verify_ssl: bool = typer.Option(False, "--no-verify-ssl", help="Skip SSL certificate check"),
) -> None:
    """Add a new pfSense device interactively."""
    config, creds = _get_config_and_creds()

    console.print()
    console.print("[bold cyan]Add pfSense Device[/]")
    console.print("[dim]─────────────────────────────────────────────[/]")

    # --- Host first so we can suggest an ID ---
    if not host:
        host = typer.prompt("pfSense IP address or hostname (e.g. 192.168.1.1)")

    suggested_id = _host_to_id(host)

    if not device_id:
        console.print()
        console.print(
            "[dim]Device ID is a short identifier used in filenames and commands.[/]\n"
            "[dim]Use lowercase letters, numbers, and hyphens only (e.g. home-fw, vpn-fw).[/]"
        )
        _id_pattern = re.compile(DEVICE_ID_PATTERN)
        while True:
            device_id = typer.prompt("Device ID", default=suggested_id).strip().lower()
            if _id_pattern.match(device_id):
                break
            print_warning(
                "Invalid ID — use only lowercase letters, numbers, and hyphens (e.g. home-fw)"
            )

    if not label:
        label = typer.prompt("Display name", default=device_id)

    if not username:
        username = typer.prompt("pfSense username", default="admin")

    if not method:
        console.print()
        console.print("[dim]Connection methods:[/]")
        console.print("[dim]  ssh   - Recommended. Reads config directly via SSH/SFTP.[/]")
        console.print("[dim]         Requires: pfSense > System > Advanced > Enable SSH[/]")
        console.print("[dim]  https - Uses the pfSense web GUI over HTTPS to download backup.[/]")
        console.print("[dim]         Requires: web GUI reachable (works without SSH).[/]")
        console.print("[dim]  http  - Same as https but without TLS (not recommended).[/]")
        method = typer.prompt("\nConnection method", default="ssh")

    try:
        conn_method = ConnectionMethod(method.lower().strip())
    except ValueError:
        print_error(f"Invalid method '{method}'. Choose: ssh, https, http")
        raise typer.Exit(1)

    # SSH key authentication (optional, SSH only)
    ssh_key_path_str: str | None = None
    if conn_method == ConnectionMethod.SSH:
        console.print()
        console.print("[dim]SSH Authentication:[/]")
        console.print("[dim]  Key-based auth is more secure than password auth.[/]")
        console.print("[dim]  Leave blank to use password authentication (default).[/]")
        ssh_key_raw = typer.prompt("SSH private key path (optional)", default="")
        if ssh_key_raw.strip():
            ssh_key_path_str = ssh_key_raw.strip()
            key_path = Path(ssh_key_path_str).expanduser()
            if not key_path.exists():
                print_warning(f"Key file not found: {key_path} (proceeding anyway)")

    console.print()
    if ssh_key_path_str:
        password = typer.prompt(
            "pfSense password (for HTTPS fallback, or press Enter to skip)",
            hide_input=True,
            default="",
        )
        if not password:
            print_info("No password set — SSH key auth only. HTTPS fallback will not work.")
    else:
        password = typer.prompt("pfSense password", hide_input=True, confirmation_prompt=True)

    # Non-default ports
    if conn_method == ConnectionMethod.SSH and ssh_port == 22:
        custom_port = typer.prompt("SSH port", default=str(ssh_port))
        ssh_port = int(custom_port)
    elif conn_method == ConnectionMethod.HTTPS and https_port == 443:
        custom_port = typer.prompt("HTTPS port", default=str(https_port))
        https_port = int(custom_port)
    elif conn_method == ConnectionMethod.HTTP and http_port == 80:
        custom_port = typer.prompt("HTTP port", default=str(http_port))
        http_port = int(custom_port)

    console.print()

    try:
        device = DeviceConfig(
            id=device_id,
            label=label,
            host=host,
            primary_method=conn_method,
            username=username,
            ssh_port=ssh_port,
            https_port=https_port,
            http_port=http_port,
            verify_ssl=not no_verify_ssl,
            ssh_key_path=Path(ssh_key_path_str).expanduser() if ssh_key_path_str else None,
        )
        config.add_device(device)
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(1)

    if password:
        creds.store(device_id, password)
        if not creds.is_persistent:
            print_warning("  Credential storage: in-memory only (no persistent keyring found).")
            print_warning("  Install keyrings.alt for persistent storage: pip install keyrings.alt")

    # Handle SSH key passphrase
    if ssh_key_path_str:
        import click

        try:
            passphrase = typer.prompt(
                "SSH key passphrase (leave blank if key is unencrypted)",
                hide_input=True,
                default="",
            )
            if passphrase:
                creds.store_ssh_key_passphrase(device_id, passphrase)
        except (click.Abort, KeyboardInterrupt):
            pass  # User pressed Ctrl+C — skip passphrase storage
        except Exception as e:
            print_warning(f"Could not store SSH key passphrase: {e}")

    config.save()

    print_success(f"Device '{device_id}' added")
    print_info(f"  Host:     {host}")
    print_info(f"  Method:   {conn_method.value}")
    print_info(f"  Username: {username}")
    if ssh_key_path_str:
        print_info(f"  SSH key:  {ssh_key_path_str}")
    elif password:
        print_info("  Password: stored in system keyring")
    console.print()

    if typer.confirm("Test connection now?", default=True):
        _test_device(device, creds)


@app.command("list")
def device_list() -> None:
    """List configured devices."""
    config, _ = _get_config_and_creds()
    if not config.devices:
        print_info("No devices configured. Run: pfs device add")
        return
    print_device_table(config.devices)


@app.command("test")
def device_test(
    device: str | None = typer.Option(None, "--device", "-d", help="Device ID (default: all)"),
) -> None:
    """Test connectivity to one or all devices."""
    config, creds = _get_config_and_creds()

    devices = [config.get_device(device)] if device else config.enabled_devices()
    if not devices or (len(devices) == 1 and devices[0] is None):
        print_error(f"Device '{device}' not found" if device else "No enabled devices")
        raise typer.Exit(1)

    for dev in devices:
        if dev is None:
            continue
        _test_device(dev, creds)


def _test_device(device: DeviceConfig, creds: CredentialService) -> None:
    from pfsentinel.services.connection import ConnectionManager

    print_info(f"Testing {device.id} ({device.host})...")

    has_creds = creds.has_password(device.id) or bool(device.ssh_key_path)
    if not has_creds:
        print_warning(f"  No credentials stored for '{device.id}'")
        return

    cm = ConnectionManager(device, creds)
    status = cm.test_all()

    def _fmt_method(ok: bool, err: str | None, label: str) -> None:
        if ok:
            console.print(f"  {label}: [green]✓ OK[/]")
        else:
            console.print(f"  {label}: [red]✗ Failed[/]")
            if err:
                console.print(f"         [dim]{err}[/]")

    _fmt_method(status.ssh_reachable, status.ssh_error, "SSH  ")
    _fmt_method(status.https_reachable, status.https_error, "HTTPS")
    _fmt_method(status.http_reachable, status.http_error, "HTTP ")

    console.print()
    if status.any_reachable:
        print_success(f"  Best method: {status.best_method.value}")
    else:
        print_error(f"  Cannot reach {device.host} via any method")
        console.print()
        console.print("[dim]Troubleshooting:[/]")
        if device.primary_method == ConnectionMethod.SSH:
            console.print("[dim]  SSH: Enable via pfSense > System > Advanced > Secure Shell[/]")
            console.print("[dim]  Key auth: Ensure your public key is added to the pfSense user[/]")
            console.print(
                "[dim]  Key path: Use a Linux path (e.g. ~/.ssh/id_rsa), not a Windows path[/]"
            )
        else:
            console.print("[dim]  HTTPS: Ensure pfSense web GUI is reachable from this machine[/]")


@app.command("remove")
def device_remove(
    device_id: str = typer.Argument(..., help="Device ID to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove a device from config."""
    config, creds = _get_config_and_creds()

    if not config.get_device(device_id):
        print_error(f"Device '{device_id}' not found")
        raise typer.Exit(1)

    if not yes:
        confirm = typer.confirm(f"Remove device '{device_id}'?", default=False)
        if not confirm:
            print_info("Aborted.")
            return

    config.remove_device(device_id)
    creds.delete(device_id)
    config.save()
    print_success(f"Device '{device_id}' removed")


@app.command("edit")
def device_edit(
    device_id: str = typer.Argument(..., help="Device ID to edit"),
) -> None:
    """Edit device configuration interactively."""
    config, creds = _get_config_and_creds()
    device = config.get_device(device_id)

    if not device:
        print_error(f"Device '{device_id}' not found")
        raise typer.Exit(1)

    label = typer.prompt("Display name", default=device.label)
    host = typer.prompt("Host/IP", default=device.host)
    username = typer.prompt("Username", default=device.username)
    method = typer.prompt("Connection method", default=device.primary_method.value)

    try:
        conn_method = ConnectionMethod(method.lower())
    except ValueError:
        print_error(f"Invalid method: {method}")
        raise typer.Exit(1)

    change_password = typer.confirm("Change password?", default=False)
    if change_password:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
        creds.store(device_id, password)

    if conn_method == ConnectionMethod.SSH:
        current_key = str(device.ssh_key_path) if device.ssh_key_path else ""
        change_key = typer.confirm(
            f"Change SSH key? (current: {current_key or 'none'})", default=False
        )
        if change_key:
            new_key_raw = typer.prompt("New SSH private key path (blank to clear)", default="")
            device.ssh_key_path = Path(new_key_raw).expanduser() if new_key_raw.strip() else None

        # Offer passphrase prompt whenever an SSH key is configured
        if device.ssh_key_path:
            has_passphrase = creds.get_ssh_key_passphrase(device_id) is not None
            pp_label = "Change" if has_passphrase else "Set"
            set_passphrase = typer.confirm(
                f"{pp_label} SSH key passphrase?", default=not has_passphrase
            )
            if set_passphrase:
                passphrase = typer.prompt(
                    "SSH key passphrase", hide_input=True, confirmation_prompt=True
                )
                creds.store_ssh_key_passphrase(device_id, passphrase)
                print_success("Passphrase saved")

    # SSL verification toggle
    if conn_method in (ConnectionMethod.HTTPS, ConnectionMethod.SSH):
        ssl_status = "enabled" if device.verify_ssl else "disabled"
        toggle_ssl = typer.confirm(
            f"Toggle SSL verification? (currently {ssl_status})", default=False
        )
        if toggle_ssl:
            device.verify_ssl = not device.verify_ssl
            new_status = "enabled" if device.verify_ssl else "disabled"
            print_info(f"SSL verification {new_status}")

    device.label = label
    device.host = host
    device.primary_method = conn_method
    device.username = username

    config.save()
    print_success(f"Device '{device_id}' updated")
