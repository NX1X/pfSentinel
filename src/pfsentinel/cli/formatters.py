"""Rich formatters for CLI output."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pfsentinel.models.backup import BackupRecord
from pfsentinel.models.device import DeviceConfig, DeviceStatus

console = Console()
err_console = Console(stderr=True)


def print_backup_table(records: list[BackupRecord], title: str = "Backups") -> None:
    table = Table(title=title, show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("Type", style="magenta", width=8)
    table.add_column("Device", style="cyan")
    table.add_column("Filename")
    table.add_column("Size", justify="right")
    table.add_column("Date", style="green")
    table.add_column("Changes", style="yellow")
    table.add_column("Status")

    for i, r in enumerate(records, 1):
        date_str = r.created_at.strftime("%Y-%m-%d %H:%M")
        if r.size_bytes < 1048576:
            size_str = f"{r.size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{r.size_bytes / 1048576:.1f} MB"
        status = "[green]OK[/]" if r.verified else "[yellow]?[/]"
        table.add_row(
            str(i),
            r.type_label,
            r.device_id,
            r.filename,
            size_str,
            date_str,
            r.changes_label,
            status,
        )

    console.print(table)


def print_device_table(
    devices: list[DeviceConfig], statuses: dict[str, DeviceStatus] | None = None
) -> None:
    table = Table(title="Devices", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="cyan")
    table.add_column("Label")
    table.add_column("Host")
    table.add_column("Method")
    table.add_column("Enabled")
    table.add_column("Status")

    for d in devices:
        enabled_str = "[green]Yes[/]" if d.enabled else "[red]No[/]"
        status_str = "-"
        if statuses and d.id in statuses:
            st = statuses[d.id]
            if st.any_reachable:
                status_str = f"[green]Reachable ({st.best_method.value})[/]"
            else:
                status_str = "[red]Unreachable[/]"
                if st.error:
                    status_str += f" - {st.error}"

        table.add_row(
            d.id,
            d.label,
            d.host,
            d.primary_method.value,
            enabled_str,
            status_str,
        )

    console.print(table)


def print_record_detail(record: BackupRecord) -> None:
    lines = [
        f"[bold]ID:[/] {record.id}",
        f"[bold]Type:[/] {record.type_label}",
        f"[bold]Device:[/] {record.device_id}",
        f"[bold]Filename:[/] {record.filename}",
        f"[bold]Date:[/] {record.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"[bold]Size:[/] {record.size_human}",
        f"[bold]SHA256:[/] {record.sha256}",
        f"[bold]Method:[/] {record.connection_method}",
        f"[bold]pfSense:[/] {record.pfsense_version or 'unknown'}",
        f"[bold]Hostname:[/] {record.device_hostname or 'unknown'}",
        f"[bold]Changes:[/] {record.changes_label}",
        f"[bold]Compressed:[/] {'Yes' if record.compressed else 'No'}",
        f"[bold]Verified:[/] {'Yes' if record.verified else 'No'}",
        f"[bold]Description:[/] {record.description or '-'}",
    ]
    if record.source_paths:
        lines.append(f"[bold]Source Paths:[/] {', '.join(record.source_paths)}")
    if record.zfs_snapshot_name:
        lines.append(f"[bold]ZFS Snapshot:[/] {record.zfs_snapshot_name}")
        if record.zfs_incremental:
            lines.append(f"[bold]ZFS Base:[/] {record.zfs_base_snapshot}")
    content = "\n".join(lines)
    console.print(Panel(content, title="Backup Record", border_style="cyan"))


def print_success(msg: str) -> None:
    console.print(f"[green]✓[/] {msg}")


def print_error(msg: str) -> None:
    err_console.print(f"[red]✗ Error:[/] {msg}")


def print_warning(msg: str) -> None:
    console.print(f"[yellow]![/] {msg}")


def print_info(msg: str) -> None:
    console.print(f"[blue]ℹ[/] {msg}")


def print_progress(message: str, percent: int) -> None:
    bar_width = 30
    filled = int(bar_width * percent / 100)
    bar = "=" * filled + "-" * (bar_width - filled)
    console.print(f"\r[{bar}] {percent:3d}% {message}", end="")
    if percent == 100:
        console.print()
