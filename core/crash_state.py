"""
Production hardening v1.2.0 — Runtime state persistence (B-7, B-8).

Category B (Infrastructure). Stores a small JSON checkpoint next to
the executable so the operator can:

    * Resume the last spreadsheet URL / window geometry / monitoring
      flag after a clean or crash exit.
    * See when the app last shut down cleanly vs. when it was killed
      (the `clean_exit` flag flips on graceful shutdown and is reset
      to `False` at every launch).

No business data lives here — the *processing* checkpoint is the
SQLite database. This file only stores UX-level state so restart is
frictionless (Production Hardening B-7 / B-8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class CrashState:
    schema: int = 1
    version: str = ""
    saved_at: str = ""
    clean_exit: bool = False
    spreadsheet_url: str = ""
    monitoring_active: bool = False
    window_geometry: str = ""   # hex-encoded QByteArray from Qt saveGeometry()
    window_state: str = ""      # hex-encoded QByteArray from saveState()
    last_panel_url: str = ""
    notes: Dict[str, Any] = field(default_factory=dict)


class CrashStateStore:
    """Load/save the crash-state file. All I/O is wrapped so a broken
    file never stops startup — a corrupt state simply falls back to a
    fresh `CrashState()`."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ load
    def load(self) -> CrashState:
        if not self.path.exists():
            return CrashState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return CrashState()
        cs = CrashState()
        for k, v in data.items():
            if hasattr(cs, k):
                try:
                    setattr(cs, k, v)
                except Exception:
                    pass
        return cs

    # ------------------------------------------------------------------ save
    def save(self, state: CrashState) -> bool:
        """Best-effort. Returns True when written, False otherwise."""
        try:
            state.saved_at = datetime.now().isoformat(timespec="seconds")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ helpers
    def mark_dirty(self, **overrides: Any) -> Optional[CrashState]:
        """Load, apply overrides, save with `clean_exit=False`.

        Called at startup so we know the app is currently running; on a
        clean exit `mark_clean_exit()` sets it back to True.
        """
        state = self.load()
        for k, v in overrides.items():
            if hasattr(state, k):
                setattr(state, k, v)
        state.clean_exit = False
        if self.save(state):
            return state
        return None

    def mark_clean_exit(self, **overrides: Any) -> bool:
        state = self.load()
        for k, v in overrides.items():
            if hasattr(state, k):
                setattr(state, k, v)
        state.clean_exit = True
        return self.save(state)
