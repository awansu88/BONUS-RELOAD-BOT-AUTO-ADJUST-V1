"""
Central JSON-file logger.

- Writes one log file per day: logs/YYYY-MM-DD.log
- Also emits messages to a Qt signal so the dashboard can render live logs.
- Simple, human-readable format:  HH:MM  message

Startup-safe behaviour (BUG-014):
- The logs/ directory is created automatically.
- If the FileHandler cannot be attached (permission denied, read-only
  volume, path too long on Windows, etc.) we fall back to a plain
  ConsoleHandler and set `file_handler_ok = False`. The application MUST
  NEVER crash because logging could not attach to a file.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Callable, List, Optional


class AppLogger:
    _instance: "AppLogger | None" = None
    MAX_BUFFER = 500

    def __init__(self, log_dir: str = "logs") -> None:
        self.log_dir = Path(log_dir)
        self.file_handler_ok: bool = False
        self.file_handler_error: Optional[str] = None
        self.log_file_path: Optional[Path] = None

        # Best-effort create the directory. If this raises we still keep
        # going and rely on the console handler.
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - depends on FS state
            self.file_handler_error = f"mkdir failed: {exc}"

        self._listeners: List[Callable[[str], None]] = []
        self._buffer: List[str] = []

        self.logger = logging.getLogger("bonus_reload")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        # Always configure our own handlers when a fresh AppLogger is
        # constructed. We do NOT gate on `logger.handlers` because pytest
        # (and some plugin systems) may have already attached their own
        # handler to this named logger — gating on that would silently
        # skip our fallback logic and leave `file_handler_error` unset.
        fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M")

        # ---- FileHandler (best effort) ----
        file_path = self.log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        try:
            fh = TimedRotatingFileHandler(
                file_path, when="midnight", backupCount=30, encoding="utf-8"
            )
            fh.suffix = "%Y-%m-%d"
            fh.setFormatter(fmt)
            self.logger.addHandler(fh)
            self.file_handler_ok = True
            self.log_file_path = file_path
        except Exception as exc:
            # Never crash startup on logging failure — the console
            # handler below still gives us visibility.
            self.file_handler_ok = False
            self.file_handler_error = f"{type(exc).__name__}: {exc}"

        # ---- ConsoleHandler (always attached, guaranteed fallback) ----
        try:
            ch = logging.StreamHandler(stream=sys.stderr)
        except Exception:
            ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        self.logger.addHandler(ch)

    @classmethod
    def get(cls, log_dir: str = "logs") -> "AppLogger":
        if cls._instance is None:
            cls._instance = AppLogger(log_dir=log_dir)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Test-only: drop the singleton so a new one can be built."""
        if cls._instance is not None:
            for h in list(cls._instance.logger.handlers):
                try:
                    cls._instance.logger.removeHandler(h)
                    h.close()
                except Exception:
                    pass
        cls._instance = None

    def add_listener(self, listener: Callable[[str], None]) -> None:
        self._listeners.append(listener)

    def buffer(self) -> List[str]:
        return list(self._buffer)

    def _emit(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M")
        line = f"{stamp}  {msg}"
        self._buffer.append(line)
        if len(self._buffer) > self.MAX_BUFFER:
            # FIFO trim so RAM stays constant during long runs.
            self._buffer = self._buffer[-self.MAX_BUFFER :]
        for listener in self._listeners:
            try:
                listener(line)
            except Exception:
                pass

    def info(self, msg: str) -> None:
        self.logger.info(msg)
        self._emit(msg)

    def warn(self, msg: str) -> None:
        self.logger.warning(msg)
        self._emit(f"WARN  {msg}")

    def error(self, msg: str) -> None:
        self.logger.error(msg)
        self._emit(f"ERROR  {msg}")
