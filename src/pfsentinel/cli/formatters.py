"""Rich formatters for CLI output."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from pfsentinel.models.backup import BackupRecord, human_size
from pfsentinel.models.device import DeviceConfig, DeviceStatus
from pfsentinel.utils.logging import set_before_console_line

if TYPE_CHECKING:
    from pfsentinel.services.run_history import RunEntry

console = Console()
err_console = Console(stderr=True)

# True while a progress bar line is drawn without its newline.
_progress_line_open = False
_progress_width = 0


def end_progress_line() -> None:
    """End an open progress bar line, so the next output starts on a new line."""
    global _progress_line_open
    if _progress_line_open:
        _progress_line_open = False
        console.file.write("\n")
        console.file.flush()


set_before_console_line(end_progress_line)


def print_backup_table(records: list[BackupRecord], title: str = "Backups") -> None:
    # One device: its ID is the same in every row and at the start of every
    # file name, so the column only takes room. In a narrow window the file
    # name is folded over two lines, never cut: the name is what you copy.
    show_device = len({r.device_id for r in records}) > 1
    table = Table(title=title, show_header=True, header_style="bold cyan")
    table.add_column("Run", style="dim", justify="right", no_wrap=True)
    table.add_column("Type", style="magenta", no_wrap=True)
    if show_device:
        table.add_column("Device", style="cyan", overflow="fold")
    table.add_column("Filename", overflow="fold")
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Date", style="green", no_wrap=True)
    table.add_column("Changes", style="yellow", overflow="fold")
    table.add_column("Status", no_wrap=True)

    for r in records:
        date_str = r.created_at.strftime("%Y-%m-%d %H:%M")
        if r.size_bytes < 1048576:
            size_str = f"{r.size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{r.size_bytes / 1048576:.1f} MB"
        status = "[green]OK[/]" if r.verified else "[yellow]?[/]"
        row = [
            f"#{r.run_number}" if r.run_number is not None else "-",
            r.type_label,
            r.filename,
            size_str,
            date_str,
            r.changes_label,
            status,
        ]
        if show_device:
            row.insert(2, r.device_id)
        table.add_row(*row)

    console.print(table)


_RUN_RESULT_STYLE = {
    "ok": "[green]OK[/]",
    "partial": "[yellow]Partial[/]",
    "failed": "[red]Failed[/]",
    "unfinished": "[yellow]No end[/]",
}


def _took(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def print_run_history(entries: list[RunEntry], title: str = "Backup runs") -> None:
    """One row per backup run, newest first, failed runs included."""
    devices = {e.device for e in entries}
    show_device = len(devices) > 1
    if len(devices) == 1:
        # No Device column, so the title must say whose runs these are.
        only = escape(next(iter(devices)))
        title = title if title.endswith(only) else f"{title} for {only}"
    table = Table(title=title, show_header=True, header_style="bold cyan")
    table.add_column("Started", style="green", no_wrap=True)
    if show_device:
        table.add_column("Device", style="cyan", overflow="fold")
    table.add_column("Run", style="dim", justify="right", no_wrap=True)
    table.add_column("Result", no_wrap=True)
    table.add_column("Files", justify="right", no_wrap=True)
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Took", justify="right", no_wrap=True)
    table.add_column("Changes", style="yellow", overflow="fold")
    table.add_column("Detail", overflow="fold")

    for e in entries:
        row = [
            e.started.strftime("%Y-%m-%d %H:%M"),
            f"#{e.run}" if e.run is not None else "-",
            _RUN_RESULT_STYLE.get(e.result, escape(e.result)),
            str(e.files) if e.files else "-",
            human_size(e.size_bytes) if e.files else "-",
            _took(e.seconds),
            e.changes or "-",
            escape(e.detail),
        ]
        if show_device:
            row.insert(1, e.device)
        table.add_row(*row)

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
                method = st.best_method.value if st.best_method else "?"
                status_str = f"[green]Reachable ({method})[/]"
            else:
                status_str = "[red]Unreachable[/]"
                if st.error:
                    status_str += f" - {escape(st.error)}"

        table.add_row(
            d.id,
            escape(d.label),
            escape(d.host),
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
        f"[bold]Filename:[/] {escape(record.filename)}",
        f"[bold]Date:[/] {record.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"[bold]Size:[/] {record.size_human}",
        f"[bold]SHA256:[/] {record.sha256}",
        f"[bold]Method:[/] {record.connection_method}",
        f"[bold]pfSense:[/] {escape(record.pfsense_version or 'unknown')}",
        f"[bold]Hostname:[/] {escape(record.device_hostname or 'unknown')}",
        f"[bold]Changes:[/] {record.changes_label}",
        f"[bold]Compressed:[/] {'Yes' if record.compressed else 'No'}",
        f"[bold]Verified:[/] {'Yes' if record.verified else 'No'}",
        f"[bold]Description:[/] {escape(record.description or '-')}",
    ]
    if record.source_paths:
        lines.append(f"[bold]Source Paths:[/] {escape(', '.join(record.source_paths))}")
    if record.zfs_snapshot_name:
        lines.append(f"[bold]ZFS Snapshot:[/] {record.zfs_snapshot_name}")
        if record.zfs_incremental:
            lines.append(f"[bold]ZFS Base:[/] {record.zfs_base_snapshot}")
    content = "\n".join(lines)
    console.print(Panel(content, title="Backup Record", border_style="cyan"))


# The message is escaped by default: it often carries text from pfSense, a
# path or an exception, and Rich reads "[b]" as markup ("[/lan]" even
# raised MarkupError). Pass markup=True only for a message you wrote.


def _text(msg: str, markup: bool) -> str:
    return msg if markup else escape(msg)


def print_success(msg: str, *, markup: bool = False) -> None:
    end_progress_line()
    console.print(f"[green]✓[/] {_text(msg, markup)}")


def print_error(msg: str, *, markup: bool = False) -> None:
    end_progress_line()
    err_console.print(f"[red]✗ Error:[/] {_text(msg, markup)}")


def print_warning(msg: str, *, markup: bool = False) -> None:
    end_progress_line()
    console.print(f"[yellow]![/] {_text(msg, markup)}")


def print_info(msg: str, *, markup: bool = False) -> None:
    end_progress_line()
    console.print(f"[blue]ℹ[/] {_text(msg, markup)}")


def _progress_enabled() -> bool:
    """Draw the bar only on a terminal: in a log file it is just noise."""
    return console.is_terminal


def print_progress(message: str, percent: int) -> None:
    global _progress_line_open, _progress_width
    if not _progress_enabled():
        return
    bar_width = 30
    filled = int(bar_width * max(0, min(percent, 100)) / 100)
    bar = "=" * filled + "-" * (bar_width - filled)
    width = max(20, console.width - 1)
    line = f"[{bar}] {percent:3d}% {message}"[:width]
    # Pad to the previous width, so a shorter message leaves no tail of the
    # previous one. Written to the stream directly: Rich's print() strips the
    # "\r" that returns to the start of the line, so the bars ran together
    # (seen in PowerShell 7 in the 0.3.0 field test).
    padded = line.ljust(_progress_width) if _progress_line_open else line
    _progress_width = len(line)
    console.file.write("\r" + padded)
    console.file.flush()
    _progress_line_open = True
    if percent == 100:
        end_progress_line()
