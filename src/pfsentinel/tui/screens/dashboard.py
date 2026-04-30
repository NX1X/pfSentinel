"""Dashboard TUI screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Label, Static

from pfsentinel.models.config import AppConfig
from pfsentinel.services.backup import BackupService


class DashboardScreen(Widget):
    """Main dashboard showing device status and recent backups."""

    DEFAULT_CSS = """
    DashboardScreen {
        height: 1fr;
        padding: 1;
    }
    .section-title {
        color: $accent;
        text-style: bold;
        padding: 0 0 0 1;
    }
    .panel {
        border: solid $accent;
        margin: 0 0 1 0;
        padding: 1;
    }
    #btn-backup {
        dock: right;
        margin: 0 1;
    }
    """

    def __init__(self, config: AppConfig, backup_service: BackupService) -> None:
        super().__init__()
        self._config = config
        self._backup_service = backup_service

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Devices", classes="section-title")
            with Vertical(classes="panel"):
                yield DataTable(id="device-table")

            yield Label("Recent Backups", classes="section-title")
            with Vertical(classes="panel"):
                yield DataTable(id="recent-table")

            with Horizontal():
                yield Static("")  # spacer
                yield Button("Backup Now [b]", id="btn-backup", variant="success")

    def on_mount(self) -> None:
        self._setup_device_table()
        self._setup_recent_table()
        self._populate()

    def _setup_device_table(self) -> None:
        table = self.query_one("#device-table", DataTable)
        table.add_columns("ID", "Label", "Host", "Method", "Status")

    def _setup_recent_table(self) -> None:
        table = self.query_one("#recent-table", DataTable)
        table.add_columns("Type", "Device", "Filename", "Size", "Date", "Changes")

    def _populate(self) -> None:
        # Devices
        device_table = self.query_one("#device-table", DataTable)
        device_table.clear()
        for d in self._config.devices:
            status = "Enabled" if d.enabled else "Disabled"
            device_table.add_row(d.id, d.label, d.host, d.primary_method.value, status)

        # Recent backups (last 5)
        recent_table = self.query_one("#recent-table", DataTable)
        recent_table.clear()
        records = self._backup_service.list_backups()[:5]
        for r in records:
            size_str = f"{r.size_bytes / 1024:.1f} KB"
            date_str = r.created_at.strftime("%Y-%m-%d %H:%M")
            recent_table.add_row(
                r.type_label, r.device_id, r.filename, size_str, date_str, r.changes_label
            )

        if not records:
            recent_table.add_row("-", "-", "No backups yet", "-", "-", "-")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-backup":
            await self.app.action_backup_now()
            self._populate()
