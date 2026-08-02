"""Logs TUI screen - live log tail."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, RichLog, Static

from pfsentinel.utils.logging import ROOT_LOGGER_NAME

LEVEL_COLORS = {
    "DEBUG": "dim",
    "INFO": "blue",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold red",
}


class RichLogHandler(logging.Handler):
    """Mirrors log records into a Textual RichLog widget."""

    def __init__(self, log_view: RichLog) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%H:%M:%S")
        )
        self._log_view = log_view

    def emit(self, record: logging.LogRecord) -> None:
        try:
            color = LEVEL_COLORS.get(record.levelname, "white")
            self._log_view.write(f"[{color}]{self.format(record)}[/]")
        except Exception:
            pass


class LogsScreen(Widget):
    """Live application log viewer."""

    DEFAULT_CSS = """
    LogsScreen {
        height: 1fr;
        padding: 1;
    }
    RichLog {
        height: 1fr;
        border: solid $accent;
    }
    #action-row {
        height: 3;
        margin: 1 0 0 0;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]Application Logs[/]", markup=True)
            yield RichLog(id="log-view", highlight=True, markup=True, max_lines=500)
            with Horizontal(id="action-row"):
                yield Button("Clear", id="btn-clear", variant="default")

    def on_mount(self) -> None:
        """Stream application log records into this widget."""
        try:
            log_view = self.query_one("#log-view", RichLog)
            app_logger = logging.getLogger(ROOT_LOGGER_NAME)
            app_logger.setLevel(logging.DEBUG)
            app_logger.addHandler(RichLogHandler(log_view))
        except Exception:
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-clear":
            self.query_one("#log-view", RichLog).clear()
