"""Backups TUI screen."""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label

from pfsentinel.models.backup import BackupRecord
from pfsentinel.models.config import AppConfig
from pfsentinel.services.backup import BackupService


class BackupsScreen(Widget):
    """Backup list with verify, restore, delete actions."""

    DEFAULT_CSS = """
    BackupsScreen {
        height: 1fr;
        padding: 1;
    }
    #filter-row {
        height: 3;
        margin: 0 0 1 0;
    }
    #filter-input {
        width: 30;
    }
    #action-row {
        height: 3;
        margin: 1 0 0 0;
    }
    Button {
        margin: 0 1 0 0;
    }
    """

    BINDINGS = [
        ("v", "verify", "Verify"),
        ("r", "restore", "Restore"),
        ("d", "delete", "Delete"),
        ("f5", "refresh", "Refresh"),
    ]

    def __init__(self, config: AppConfig, backup_service: BackupService) -> None:
        super().__init__()
        self._config = config
        self._backup_service = backup_service
        self._records: list[BackupRecord] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="filter-row"):
                yield Label("Filter: ")
                yield Input(placeholder="Search...", id="filter-input")
            yield DataTable(id="backup-table", cursor_type="row")
            with Horizontal(id="action-row"):
                yield Button("Verify [v]", id="btn-verify", variant="default")
                yield Button("Restore [r]", id="btn-restore", variant="default")
                yield Button("Delete [d]", id="btn-delete", variant="error")
                yield Button("Refresh [F5]", id="btn-refresh", variant="default")

    def on_mount(self) -> None:
        table = self.query_one("#backup-table", DataTable)
        table.add_columns("#", "Type", "Device", "Filename", "Size", "Date", "Changes", "Status")
        self._load_backups()

    def _load_backups(self, filter_str: str = "") -> None:
        self._records = self._backup_service.list_backups()
        if filter_str:
            self._records = [
                r
                for r in self._records
                if filter_str.lower() in r.filename.lower()
                or filter_str.lower() in r.device_id.lower()
            ]

        table = self.query_one("#backup-table", DataTable)
        table.clear()

        for i, r in enumerate(self._records, 1):
            size_str = f"{r.size_bytes / 1024:.1f} KB"
            date_str = r.created_at.strftime("%Y-%m-%d %H:%M")
            status = "OK" if r.verified else "?"
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

    def _selected_record(self) -> BackupRecord | None:
        table = self.query_one("#backup-table", DataTable)
        if table.cursor_row is None or not self._records:
            return None
        idx = table.cursor_row
        if idx < len(self._records):
            return self._records[idx]
        return None

    def on_input_changed(self, event: Input.Changed) -> None:
        self._load_backups(event.value)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-verify":
            await self.action_verify()
        elif event.button.id == "btn-restore":
            await self.action_restore()
        elif event.button.id == "btn-delete":
            await self.action_delete()
        elif event.button.id == "btn-refresh":
            self.action_refresh()

    def action_refresh(self) -> None:
        self._load_backups()
        self.notify("Refreshed")

    async def action_verify(self) -> None:
        record = self._selected_record()
        if not record:
            self.notify("Select a backup first", severity="warning")
            return
        self.notify(f"Verifying {record.filename}...")
        ok = await asyncio.to_thread(self._backup_service.verify_backup, record)
        if ok:
            self.notify("Integrity verified OK", severity="information")
        else:
            self.notify("Integrity check FAILED", severity="error")

    async def action_restore(self) -> None:
        record = self._selected_record()
        if not record:
            self.notify("Select a backup first", severity="warning")
            return
        # Restore to home dir for now
        from pathlib import Path

        target = Path.home()
        try:
            dest = await asyncio.to_thread(self._backup_service.restore_backup, record, target)
            self.notify(f"Restored to: {dest}", severity="information")
        except Exception as e:
            self.notify(f"Restore failed: {e}", severity="error")

    async def action_delete(self) -> None:
        record = self._selected_record()
        if not record:
            self.notify("Select a backup first", severity="warning")
            return
        try:
            await asyncio.to_thread(self._backup_service.delete_backup, record)
            self.notify(f"Deleted: {record.filename}", severity="information")
            self._load_backups()
        except Exception as e:
            self.notify(f"Delete failed: {e}", severity="error")
