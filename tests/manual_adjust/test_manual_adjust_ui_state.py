from __future__ import annotations

from core.manual_adjust_loader import classify_rows, snapshot_fingerprint
from core.manual_adjust_models import RawManualAdjustRow
from core.manual_adjust_repository import ManualAdjustRepository
from ui.manual_adjust_state import ManualPreviewState, OperatingMode


class Loader:
    def __init__(self, repository):
        self.repository = repository
        self.calls = 0

    def load(self):
        self.calls += 1
        rows = [
            RawManualAdjustRow(2, " Alice ", "1,000", "tx-1"),
            RawManualAdjustRow(3, "ALICE", "2000", "tx-2"),
            RawManualAdjustRow(4, "Bob", "bad input", "tx-3"),
        ]
        return self.repository.create_snapshot(
            "sheet-id", "MASTER", snapshot_fingerprint("sheet-id", "MASTER", rows),
            classify_rows(rows),
        )


def test_default_mode_and_auto_idle_switch_boundary():
    state = ManualPreviewState()
    assert state.mode is OperatingMode.AUTO
    assert state.select_mode(OperatingMode.MANUAL, "idle") == (True, "")
    assert state.mode is OperatingMode.MANUAL


def test_busy_auto_states_reject_manual_without_silent_switch():
    for auto_state in ("running", "monitoring", "stopping"):
        state = ManualPreviewState()
        allowed, message = state.select_mode(OperatingMode.MANUAL, auto_state)
        assert not allowed
        assert state.mode is OperatingMode.AUTO
        assert message == "Stop AUTO Bonus Reload before switching mode."


def test_manual_backend_is_lazy_initialized_once_and_reused():
    state = ManualPreviewState()
    calls = 0

    def initialize():
        nonlocal calls
        calls += 1

    # Constructing the default state performs no backend work.
    assert calls == 0 and state.mode is OperatingMode.AUTO
    assert state.select_mode(OperatingMode.MANUAL, "idle", initialize) == (True, "")
    assert calls == 1
    state.select_mode(OperatingMode.AUTO, "idle")
    assert state.select_mode(OperatingMode.MANUAL, "idle", initialize) == (True, "")
    assert calls == 1


def test_manual_backend_failure_keeps_auto_and_never_loads_snapshot():
    state = ManualPreviewState()
    loader_calls = 0

    def fail():
        raise OSError("schema unavailable")

    allowed, message = state.select_mode(OperatingMode.MANUAL, "idle", fail)
    assert not allowed
    assert message == "Full Manual Adjust is unavailable: schema unavailable"
    assert state.mode is OperatingMode.AUTO
    assert state.active_cycle_id is None
    assert loader_calls == 0


def test_one_load_and_sqlite_only_refresh_preserve_auditable_preview(tmp_path):
    repository = ManualAdjustRepository(tmp_path / "processed.db")
    repository.initialize_schema()
    try:
        state, loader = ManualPreviewState(), Loader(repository)
        cycle, summary, rows = state.load_snapshot(loader, repository)
        assert loader.calls == 1
        assert state.active_cycle_id == cycle["cycle_id"]
        assert (summary.source_rows, summary.unique_users, summary.ready,
                summary.duplicates, summary.invalid,
                summary.total_adjustment_amount) == (3, 2, 1, 1, 1, 1000)
        assert [row["classification"] for row in rows] == ["READY", "DUPLICATE", "INVALID"]
        assert rows[2]["amount_raw"] == "bad input"
        assert rows[1]["reason"] == "duplicate username"
        assert rows[1]["winner_source_row_id"] == rows[0]["source_row_id"]

        assert state.current_preview(repository)[1:] == (summary, rows)
        assert loader.calls == 1  # repaint/reopen performs no loader/Sheet call
    finally:
        repository.close()


def test_repeated_explicit_load_creates_new_cycle_without_mutating_first(tmp_path):
    repository = ManualAdjustRepository(tmp_path / "processed.db")
    repository.initialize_schema()
    try:
        state, loader = ManualPreviewState(), Loader(repository)
        first = state.load_snapshot(loader, repository)[0]
        first_rows = repository.get_source_rows(first["cycle_id"])
        second = state.load_snapshot(loader, repository)[0]
        assert loader.calls == 2
        assert first["cycle_id"] != second["cycle_id"]
        assert first["snapshot_fingerprint"] == second["snapshot_fingerprint"]
        assert repository.get_source_rows(first["cycle_id"]) == first_rows
        assert repository._conn.execute("SELECT COUNT(*) FROM manual_adjust_attempts").fetchone()[0] == 0
    finally:
        repository.close()
