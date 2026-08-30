from __future__ import annotations

from core.manual_adjust_loader import classify_rows, snapshot_fingerprint
from core.manual_adjust_models import RawManualAdjustRow
from core.manual_adjust_repository import ManualAdjustRepository
from ui.manual_adjust_state import (ManualPreviewState, OperatingMode,
                                    manual_execution_blocks_auto)
from pathlib import Path

import pytest


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
    allowed, message = state.select_mode(OperatingMode.MANUAL, "idle")
    assert not allowed and "backend is not ready" in message
    assert state.select_mode(OperatingMode.MANUAL, "idle", lambda: None) == (True, "")
    assert state.mode is OperatingMode.MANUAL


def test_phase4_dashboard_wires_review_and_recovery_actions():
    source = (Path(__file__).parents[2] / "ui" / "dashboard.py").read_text(encoding="utf-8")
    for connection in (
        "retry_requested.connect(self._on_manual_retry)",
        "finalize_requested.connect(self._on_manual_finalize)",
        "reconcile_requested.connect(self._on_manual_reconcile)",
        "open_cycle_requested.connect(self._on_manual_open_cycle)",
        "recover_requested.connect(self._on_manual_recover)",
    ):
        assert connection in source
    assert '"manual_worker_timer", "manual_heartbeat_timer"' in source


def test_manual_entry_resets_view_with_shared_context_and_auto_controls_stay_frozen():
    source = (Path(__file__).parents[2] / "ui" / "dashboard.py").read_text(encoding="utf-8")
    mode_method = source[source.index("    def _on_mode_selected"):
                         source.index("    def _manual_live_cycle_id")]
    assert "self.manual_state.active_cycle_id = None" in mode_method
    assert "self.manual_view.reset_unselected_state(" in mode_method
    assert "panel_attached=self.panel.is_attached" in mode_method
    assert "panel_open=self.panel.is_alive()" in mode_method
    assert '"execution_enabled", False) is True' in mode_method
    # Manual corrections must not rename the accepted AUTO controls.
    assert 'self.btn_start = QPushButton("START")' in source
    assert 'self.btn_stop = QPushButton("STOP")' in source


def test_mode_selector_keeps_options_dimensions_font_and_clear_chevron():
    source = (Path(__file__).parents[2] / "ui" / "dashboard.py").read_text(encoding="utf-8")
    assert "class HeaderModeSelector(QComboBox):" in source
    assert "self.mode_selector = HeaderModeSelector()" in source
    assert "self.mode_selector.setFixedSize(205, 36)" in source
    assert 'self.mode_selector.setFont(QFont("Segoe UI", 9, QFont.DemiBold))' in source
    assert "self.mode_selector.view().setFont(self.mode_selector.font())" in source
    assert "self.mode_selector.addItems([mode.value for mode in OperatingMode])" in source
    assert 'QColor("#F5B301")' in source
    assert "painter.drawLine(center_x - 4" in source
    assert "QComboBox#operating-mode-selector::down-arrow { image: none; }" in source
    assert "QComboBox::down-arrow { image: none; }" not in source


def test_manual_running_panel_controls_and_handlers_are_locked():
    root = Path(__file__).parents[2]
    view_source = (root / "ui" / "manual_adjust_view.py").read_text(encoding="utf-8")
    running_actions = view_source[view_source.index('elif status == "RUNNING"'):
                                  view_source.index('elif status == "STOPPED"')]
    assert 'self.actions["stop"].show()' in running_actions
    assert 'self.actions["open_panel"]' not in running_actions
    assert 'self.actions["attach_panel"]' not in running_actions

    dashboard = (root / "ui" / "dashboard.py").read_text(encoding="utf-8")
    open_method = dashboard[dashboard.index("    def _on_open_panel"):dashboard.index("    def _on_ready")]
    ready_method = dashboard[dashboard.index("    def _on_ready"):dashboard.index("    def _poll_panel_alive")]
    for method, mutation in ((open_method, "self.panel.open_panel"),
                             (ready_method, "self.panel.attach")):
        guard = method.index("self.manual_state.mode is OperatingMode.MANUAL")
        rejection = method.index("Stop Manual Adjust before opening or re-attaching the panel.")
        call = method.index(mutation)
        assert guard < rejection < call
    # The condition is Manual-only, preserving the frozen AUTO path.
    assert "self._manual_live_cycle_id() is not None" in open_method
    assert "self._manual_live_cycle_id() is not None" in ready_method


def test_running_cycle_pins_load_and_persisted_selection():
    state = ManualPreviewState(manual_backend_ready=True, active_cycle_id="cycle-a")
    class Loader:
        calls = 0
        def load(self): self.calls += 1; return "cycle-b"
    loader = Loader()
    with pytest.raises(RuntimeError, match="running Manual cycle"):
        state.load_snapshot(loader, object(), live_cycle_id="cycle-a")
    assert loader.calls == 0 and state.active_cycle_id == "cycle-a"
    with pytest.raises(RuntimeError, match="opening another cycle"):
        state.select_persisted_cycle("cycle-b", live_cycle_id="cycle-a")
    assert state.active_cycle_id == "cycle-a"


def test_authoritative_manual_execution_blocks_auto_without_ui_selection():
    assert manual_execution_blocks_auto(controller_cycle_status="RUNNING",
        current_transaction=False, worker_active=False, heartbeat_active=False)
    assert manual_execution_blocks_auto(controller_cycle_status="STOPPED",
        current_transaction=True, worker_active=False, heartbeat_active=False)
    assert manual_execution_blocks_auto(controller_cycle_status="STOPPED",
        current_transaction=False, worker_active=True, heartbeat_active=False)
    assert not manual_execution_blocks_auto(controller_cycle_status="STOPPED",
        current_transaction=False, worker_active=False, heartbeat_active=False)


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
