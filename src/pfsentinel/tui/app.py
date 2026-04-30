"""Textual TUI application root."""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

from pfsentinel import __version__
from pfsentinel.models.config import AppConfig
from pfsentinel.services.backup import BackupService
from pfsentinel.services.credentials import CredentialService
from pfsentinel.tui.screens.backups import BackupsScreen
from pfsentinel.tui.screens.dashboard import DashboardScreen
from pfsentinel.tui.screens.devices import DevicesScreen
from pfsentinel.tui.screens.logs import LogsScreen
from pfsentinel.tui.screens.settings import SettingsScreen


class GuardianApp(App):
    """pfSentinel TUI application."""

    CSS_PATH = None  # Inline CSS below
    TITLE = f"pfSentinel v{__version__}"
    SUB_TITLE = "pfSense Backup Solution"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("b", "backup_now", "Backup Now", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("?", "help", "Help", show=True),
    ]

    CSS = """
    Screen {
        background: $surface;
    }
    TabbedContent {
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._config = AppConfig.load()
        self._creds = CredentialService()
        self._backup_service = BackupService(self._config, self._creds)

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield DashboardScreen(self._config, self._backup_service)
            with TabPane("Backups", id="backups"):
                yield BackupsScreen(self._config, self._backup_service)
            with TabPane("Devices", id="devices"):
                yield DevicesScreen(self._config, self._creds)
            with TabPane("Settings", id="settings"):
                yield SettingsScreen(self._config)
            with TabPane("Logs", id="logs"):
                yield LogsScreen()
        yield Footer()

    async def action_backup_now(self) -> None:
        """Trigger immediate backup for all devices."""
        devices = self._config.enabled_devices()
        if not devices:
            self.notify("No enabled devices configured", severity="warning")
            return

        self.notify("Starting backup...", severity="information")
        for device in devices:
            try:
                record = await asyncio.to_thread(self._backup_service.run_backup, device.id)
                self.notify(f"Backup complete: {record.filename}", severity="information")
            except Exception as e:
                self.notify(f"Backup failed for {device.id}: {e}", severity="error")

    async def action_refresh(self) -> None:
        """Reload config and refresh current view."""
        self._config = AppConfig.load()
        self._backup_service = BackupService(self._config, self._creds)
        self.notify("Refreshed", severity="information")
