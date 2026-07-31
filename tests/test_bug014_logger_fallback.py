"""
BUG-014 regression tests — Logger startup must NEVER crash the app.

If the FileHandler can't be attached (permission denied, unwritable path,
etc.) the logger falls back to a ConsoleHandler and reports
`file_handler_ok=False` so `run_diagnostics()` can display a WARN item.
The application MUST continue in any case.
"""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.logger import AppLogger


@pytest.fixture(autouse=True)
def _reset_logger():
    AppLogger.reset()
    yield
    AppLogger.reset()


def test_logger_writes_file_when_writable(tmp_path):
    log_dir = tmp_path / "logs"
    log = AppLogger.get(log_dir=str(log_dir))
    log.info("hello world")
    assert log.file_handler_ok is True
    assert log.file_handler_error is None
    files = list(log_dir.glob("*.log"))
    assert files, "expected at least one .log file to be created"


def test_logger_falls_back_when_file_handler_raises(tmp_path, monkeypatch):
    """Force TimedRotatingFileHandler.__init__ to raise. Logger must
    still initialise with a ConsoleHandler and no exception must escape."""
    import core.logger as logger_module

    class ExplodingHandler(logging.Handler):
        def __init__(self, *a, **kw):
            raise PermissionError("simulated permission denied")

    monkeypatch.setattr(logger_module, "TimedRotatingFileHandler", ExplodingHandler)

    log = AppLogger.get(log_dir=str(tmp_path / "logs"))
    log.info("still alive")

    assert log.file_handler_ok is False
    assert log.file_handler_error is not None
    assert "PermissionError" in log.file_handler_error
    # At least one handler (the console fallback) is attached.
    assert any(
        isinstance(h, logging.StreamHandler) for h in log.logger.handlers
    )


def test_logger_survives_unwritable_dir(tmp_path):
    """Point log_dir at a nonexistent, uncreatable path (a file, not a
    directory). Logger must degrade gracefully."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    log_dir = blocker / "logs"    # can never be created — parent is a file

    log = AppLogger.get(log_dir=str(log_dir))
    # Depending on OS, mkdir may fail OR the file handler may fail.
    # Either way, the logger must still be usable.
    log.info("degraded ok")
    log.warn("still ok")
    log.error("also ok")
    # Console handler is present regardless.
    assert log.logger.handlers
