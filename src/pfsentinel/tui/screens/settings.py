"""Settings TUI screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Label, Static


class SettingsScreen(Widget):
    """Configuration editor."""

    DEFAULT_CSS = """
    SettingsScreen {
        height: 1fr;
        padding: 1;
    }
    .field-label {
        padding: 0 0 0 1;
        color: $text-muted;
    }
    Input {
        margin: 0 0 1 0;
    }
    Button {
        margin: 1 0 0 0;
    }
    """

    def __init__(self, config) -> None:
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]Backup Settings[/]", markup=True)
            yield Label("Backup Directory:", classes="field-label")
            yield Input(
                value=str(self._config.backup_policy.backup_root),
                id="backup-root",
                placeholder="~/Documents/pfSentinel",
            )
            yield Label("Max Backups Per Device:", classes="field-label")
            yield Input(
                value=str(self._config.backup_policy.max_backups_per_device),
                id="max-backups",
            )
            yield Label("Keep Days:", classes="field-label")
            yield Input(
                value=str(self._config.backup_policy.keep_days),
                id="keep-days",
            )
            yield Checkbox(
                "Compress backups (gzip)",
                value=self._config.backup_policy.compress,
                id="compress",
            )
            yield Checkbox(
                "Validate after backup",
                value=self._config.backup_policy.validate_after_backup,
                id="validate",
            )

            yield Static("")
            yield Static("[bold]Notifications[/]", markup=True)
            yield Checkbox(
                "Windows toast notifications",
                value=self._config.notifications.windows_toast_enabled,
                id="windows-toast",
            )
            yield Checkbox(
                "Telegram notifications",
                value=self._config.notifications.telegram_enabled,
                id="telegram",
            )
            yield Label("Telegram Chat ID:", classes="field-label")
            yield Input(
                value=self._config.notifications.telegram_chat_id or "",
                id="telegram-chat-id",
                placeholder="Your Telegram chat ID",
            )

            yield Button("Save Settings", id="btn-save", variant="success")
            yield Static(
                "[dim]Note: Use 'pfs schedule' CLI commands to manage scheduling[/]",
                markup=True,
            )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._save()

    def _save(self) -> None:
        from pathlib import Path

        try:
            self._config.backup_policy.backup_root = Path(
                self.query_one("#backup-root", Input).value
            )
            self._config.backup_policy.max_backups_per_device = int(
                self.query_one("#max-backups", Input).value
            )
            self._config.backup_policy.keep_days = int(self.query_one("#keep-days", Input).value)
            self._config.backup_policy.compress = self.query_one("#compress", Checkbox).value
            self._config.backup_policy.validate_after_backup = self.query_one(
                "#validate", Checkbox
            ).value
            self._config.notifications.windows_toast_enabled = self.query_one(
                "#windows-toast", Checkbox
            ).value
            self._config.notifications.telegram_enabled = self.query_one(
                "#telegram", Checkbox
            ).value
            chat_id = self.query_one("#telegram-chat-id", Input).value.strip()
            self._config.notifications.telegram_chat_id = chat_id or None

            self._config.save()
            self.notify("Settings saved", severity="information")
        except Exception as e:
            self.notify(f"Failed to save: {e}", severity="error")
