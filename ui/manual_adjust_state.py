"""Display-only state for Phase 3 Full Manual Adjust mode.

This module deliberately has no Qt or AUTO engine dependency, allowing the
mode boundary and persisted-preview flow to be tested in headless builds.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OperatingMode(str, Enum):
    AUTO = "AUTO BONUS RELOAD"
    MANUAL = "FULL MANUAL ADJUST"


@dataclass
class ManualPreviewState:
    mode: OperatingMode = OperatingMode.AUTO
    active_cycle_id: str | None = None

    def select_mode(self, requested: OperatingMode, auto_state: str) -> tuple[bool, str]:
        if requested is OperatingMode.MANUAL and auto_state != "idle":
            return False, "Stop AUTO Bonus Reload before switching mode."
        self.mode = requested
        return True, ""

    def load_snapshot(self, loader, repository):
        """One explicit action means one load, followed only by SQLite reads."""
        cycle_id = loader.load()
        summary = repository.get_cycle_summary(cycle_id)
        rows = repository.get_source_rows(cycle_id)
        cycle = repository.get_cycle(cycle_id)
        self.active_cycle_id = cycle_id
        return cycle, summary, rows

    def current_preview(self, repository):
        """Repaint the current snapshot without consulting Sheets/loader."""
        if self.active_cycle_id is None:
            return None
        cycle_id = self.active_cycle_id
        return (
            repository.get_cycle(cycle_id),
            repository.get_cycle_summary(cycle_id),
            repository.get_source_rows(cycle_id),
        )
