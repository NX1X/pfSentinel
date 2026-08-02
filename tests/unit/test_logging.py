"""Tests for the stdlib logging setup."""

from __future__ import annotations

import io
import logging

import pytest

from pfsentinel.utils.logging import ROOT_LOGGER_NAME, configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_root_logger():
    root = logging.getLogger(ROOT_LOGGER_NAME)
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_propagate = root.propagate
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.setLevel(saved_level)
    root.propagate = saved_propagate


def test_configure_logging_adds_single_handler():
    configure_logging()
    root = logging.getLogger(ROOT_LOGGER_NAME)
    assert len(root.handlers) == 1


def test_configure_logging_is_idempotent():
    configure_logging()
    configure_logging()
    configure_logging("DEBUG")
    root = logging.getLogger(ROOT_LOGGER_NAME)
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG


def test_records_reach_the_stream():
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    get_logger("pfsentinel.test_records").info("hello from pfsentinel")
    assert "hello from pfsentinel" in stream.getvalue()
    assert "INFO" in stream.getvalue()


def test_debug_level_emits_debug_records():
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    get_logger("pfsentinel.test_debug").debug("verbose detail")
    assert "verbose detail" in stream.getvalue()


def test_warning_level_suppresses_info_records():
    stream = io.StringIO()
    configure_logging("WARNING", stream=stream)
    logger = get_logger("pfsentinel.test_warning")
    logger.info("should not appear")
    logger.warning("should appear")
    output = stream.getvalue()
    assert "should not appear" not in output
    assert "should appear" in output


def test_unknown_level_falls_back_to_info():
    stream = io.StringIO()
    configure_logging("NOT_A_LEVEL", stream=stream)
    assert logging.getLogger(ROOT_LOGGER_NAME).level == logging.INFO


def test_lowercase_level_accepted():
    configure_logging("debug")
    assert logging.getLogger(ROOT_LOGGER_NAME).level == logging.DEBUG


def test_reconfigure_switches_stream():
    first = io.StringIO()
    second = io.StringIO()
    configure_logging("INFO", stream=first)
    configure_logging("INFO", stream=second)
    get_logger("pfsentinel.test_stream_switch").info("second stream only")
    assert first.getvalue() == ""
    assert "second stream only" in second.getvalue()


def test_module_logger_inherits_root_config():
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    child = get_logger("pfsentinel.services.example")
    assert child.level == logging.NOTSET
    assert child.getEffectiveLevel() == logging.DEBUG
    assert child.handlers == []
    child.debug("inherited handler")
    assert "inherited handler" in stream.getvalue()


def test_does_not_propagate_to_python_root():
    configure_logging("INFO", stream=io.StringIO())
    assert logging.getLogger(ROOT_LOGGER_NAME).propagate is False


def test_service_modules_use_namespaced_loggers():
    from pfsentinel.services import backup, connection

    assert backup.logger.name.startswith("pfsentinel.")
    assert connection.logger.name.startswith("pfsentinel.")
