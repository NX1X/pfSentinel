"""Devices TUI screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Label

from pfsentinel.models.config import AppConfig
from pfsentinel.services.credentials import CredentialService


class DevicesScreen(Widget):
    """Device list with management actions."""

    DEFAULT_CSS = """
    DevicesScreen {
        height: 1fr;
        padding: 1;
    }
    #action-row {
        height: 3;
        margin: 1 0 0 0;
    }
    Button {
        margin: 0 1 0 0;
    }
    """

    def __init__(self, config: AppConfig, creds: CredentialService) -> None:
        super().__init__()
        self._config = config
        self._creds = creds

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Configured Devices", id="title")
            yield DataTable(id="device-table", cursor_type="row")
            with Horizontal(id="action-row"):
                yield Button("Test Connection", id="btn-test", variant="default")
                yield Button("Refresh", id="btn-refresh", variant="default")
                yield Label(
                    "  [dim]Use CLI for add/remove: pfs device add[/]",
                    markup=True,
                )

    def on_mount(self) -> None:
        table = self.query_one("#device-table", DataTable)
        table.add_columns("ID", "Label", "Host", "Method", "Port", "SSL Verify", "Enabled")
        self._populate()

    def _populate(self) -> None:
        table = self.query_one("#device-table", DataTable)
        table.clear()
        for d in self._config.devices:
            table.add_row(
                d.id,
                d.label,
                d.host,
                d.primary_method.value,
                str(d.ssh_port if d.primary_method.value == "ssh" else d.https_port),
                "Yes" if d.verify_ssl else "No",
                "Yes" if d.enabled else "No",
            )
        if not self._config.devices:
            table.add_row("-", "No devices configured", "-", "-", "-", "-", "-")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-test":
            await self._test_selected()
        elif event.button.id == "btn-refresh":
            self._config = AppConfig.load()
            self._populate()
            self.notify("Refreshed")

    async def _test_selected(self) -> None:
        import asyncio

        table = self.query_one("#device-table", DataTable)
        if table.cursor_row is None or not self._config.devices:
            self.notify("Select a device first", severity="warning")
            return

        idx = table.cursor_row
        if idx >= len(self._config.devices):
            return

        device = self._config.devices[idx]
        self.notify(f"Testing {device.id}...", severity="information")

        from pfsentinel.services.connection import ConnectionManager

        cm = ConnectionManager(device, self._creds)
        try:
            status = await asyncio.to_thread(cm.test_all)
            if status.any_reachable:
                self.notify(
                    f"{device.id}: Reachable via {status.best_method.value}",
                    severity="information",
                )
            else:
                self.notify(f"{device.id}: Not reachable via any method", severity="error")
        except Exception as e:
            self.notify(f"Test failed: {e}", severity="error")
