"""Application logging setup built on the standard library."""

from __future__ import annotations

import logging
import re
import sys
from typing import TextIO

ROOT_LOGGER_NAME = "pfsentinel"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
DATE_FORMAT = "%H:%M:%S"

_HANDLER_TAG = "pfsentinel_console"

# Credentials that travel inside URLs, so any error message that quotes the
# URL would print them: a Telegram bot token sits in the path
# (/bot<id>:<secret>/...), and a Slack incoming-webhook URL is itself the secret.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), "bot<redacted>"),
    (
        re.compile(r"(https?://(?:[\w-]+\.)*hooks\.slack\.com/services/)[^\s'\"<>]+"),
        r"\1<redacted>",
    ),
)


def redact_secrets(text: str) -> str:
    """Replace Telegram bot tokens and Slack webhook paths in ``text``."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts secrets from the whole record, traceback included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """Attach a console handler to the ``pfsentinel`` logger.

    Safe to call repeatedly: the existing console handler is reused and only its
    level and stream are updated, so handlers never stack up.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    resolved = logging.getLevelNamesMapping().get(str(level).upper(), logging.INFO)
    logger.setLevel(resolved)
    logger.propagate = False

    target = stream if stream is not None else sys.stderr
    handler = _find_console_handler(logger)
    if handler is None:
        handler = logging.StreamHandler(target)
        setattr(handler, _HANDLER_TAG, True)
        handler.setFormatter(RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        logger.addHandler(handler)
    else:
        handler.setStream(target)
    handler.setLevel(resolved)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger parented under the ``pfsentinel`` root logger."""
    return logging.getLogger(name)


def _find_console_handler(logger: logging.Logger) -> logging.StreamHandler[TextIO] | None:
    for handler in logger.handlers:
        if getattr(handler, _HANDLER_TAG, False):
            return handler  # type: ignore[return-value]  # tagged at construction
    return None
