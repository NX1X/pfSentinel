"""Logs TUI screen - live log tail."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, RichLog, Static


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
        """Setup loguru sink to stream logs to this widget."""
        try:
            from loguru import logger

            log_view = self.query_one("#log-view", RichLog)

            def tui_sink(message) -> None:
                try:
                    level = message.record["level"].name
                    colors = {
                        "DEBUG": "dim",
                        "INFO": "blue",
                        "WARNING": "yellow",
                        "ERROR": "red",
                        "CRITICAL": "bold red",
                    }
                    color = colors.get(level, "white")
                    log_view.write(f"[{color}]{message}[/]")
                except Exception:
                    pass

            logger.add(tui_sink, format="{time:HH:mm:ss} | {level:<8} | {message}", level="DEBUG")
        except Exception:
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-clear":
            self.query_one("#log-view", RichLog).clear()
