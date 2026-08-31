"""Presentation-contract tests for the state-aware Manual dashboard."""

import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="Qt runtime libraries unavailable", exc_type=ImportError)
QApplication = QtWidgets.QApplication

from ui.manual_adjust_view import ManualAdjustView

_APP = QApplication.instance() or QApplication([])


def _view():
    view = ManualAdjustView()
    view.set_sheet_connected(True)
    return view


def _visible(view, key):
    # isVisible() is false while the unparented test window itself is hidden.
    return not view.actions[key].isHidden()


def test_manual_signals_and_preview_actions_remain_available():
    view = _view()
    for name in ("load_requested", "open_panel_requested", "attach_panel_requested",
                 "start_requested", "stop_requested", "resume_requested",
                 "retry_requested", "finalize_requested", "reconcile_requested",
                 "open_cycle_requested", "recover_requested"):
        assert hasattr(view, name)
    view.set_execution_state("PREVIEW", {"pending": 2}, execution_enabled=True,
                             panel_attached=True, has_current_cycle=True)
    assert all(_visible(view, key) for key in ("load", "open_panel", "attach_panel", "start"))
    assert view.actions["start"].isEnabled()
    assert view.actions["attach_panel"].text() == "READY"


def test_start_uses_current_cycle_not_recovery_selector_state():
    view = _view()
    view.display_nonterminal_cycles([
        {"status": "STOPPED", "created_at": "yesterday", "cycle_id": "old-cycle"}
    ])
    assert view.cycle_selector.currentData() is None
    view.set_execution_state("PREVIEW", {"pending": 2}, execution_enabled=True,
                             panel_attached=True, has_current_cycle=True)
    assert view.actions["start"].isEnabled()

    view.set_execution_state("PREVIEW", {"pending": 2}, execution_enabled=False,
                             panel_attached=True, has_current_cycle=True)
    assert not view.actions["start"].isEnabled()

    view.set_execution_state("PREVIEW", {"pending": 2}, execution_enabled=True,
                             panel_attached=True, has_current_cycle=False)
    assert not view.actions["start"].isEnabled()
    assert view.cycle_selector.count() == 2
    assert view.cycle_selector.placeholderText() == "SELECT PERSISTED CYCLE"
    assert view.open_cycle_button.text() == "OPEN"


def test_lifecycle_actions_are_taller_than_standard_actions():
    view = _view()
    for key in ("load", "open_panel", "attach_panel", "retry", "finalize", "reconcile"):
        assert view.actions[key].minimumHeight() == 38
    for key in ("start", "stop", "resume"):
        assert view.actions[key].minimumHeight() == 44


def test_running_pause_request_is_cooperative_and_locks_navigation():
    view = _view(); calls = []
    view.stop_requested.connect(lambda: calls.append("request_stop"))
    view.set_execution_state("RUNNING", {"pending": 3, "success": 2}, execution_enabled=True, panel_attached=True)
    assert _visible(view, "stop") and view.actions["stop"].text() == "PAUSE"
    assert not _visible(view, "open_panel") and not _visible(view, "attach_panel")
    assert not _visible(view, "load")
    assert not view.cycle_selector.isEnabled() and not view.open_cycle_button.isEnabled()
    view.actions["stop"].click()
    assert calls == ["request_stop"]
    assert view.actions["stop"].text() == "PAUSING..." and not view.actions["stop"].isEnabled()
    assert not _visible(view, "resume")
    # A RUNNING refresh cannot prematurely offer CONTINUE.
    view.set_execution_state("RUNNING", {"pending": 3, "success": 2}, execution_enabled=True, panel_attached=True)
    assert view.actions["stop"].text() == "PAUSING..." and not _visible(view, "resume")


def test_stopped_is_the_only_paused_state_and_continue_keeps_progress():
    view = _view(); calls = []
    view.resume_requested.connect(lambda: calls.append("resume"))
    summary = {"success": 37, "pending": 63, "total_adjusted_successfully": 3700}
    view.set_execution_state("STOPPED", summary, execution_enabled=True, panel_attached=True)
    assert view.execution_status.text() == "PAUSED" and view.status_values["MANUAL CYCLE"].text() == "PAUSED"
    assert _visible(view, "resume") and view.actions["resume"].text() == "CONTINUE"
    assert view.progress_text.text() == "Processed 37 / 100"
    view.actions["resume"].click()
    assert calls == ["resume"] and view.progress_text.text() == "Processed 37 / 100"
    for state in ("FAILURE_REVIEW", "REVIEW_REQUIRED", "HARD_STOPPED"):
        view.set_execution_state(state, summary, execution_enabled=True, panel_attached=True)
        assert view.execution_status.text() != "PAUSED"


def test_submitting_remains_in_invariant_progress_total():
    view = _view()
    view.set_execution_state("RUNNING", {"success": 37, "submitting": 1, "pending": 62})
    assert view.progress_text.text() == "Processed 37 / 100"
    assert view.execution_values["SUBMITTING"].text() == "1"


def test_current_execution_uses_no_nonexistent_summary_fields():
    view = _view()
    view.set_execution_state("RUNNING", {"success": 1, "pending": 2})
    assert set(view.current_values) == {"STATE", "ACTIVE TRANSACTION"}
    assert view.current_values["STATE"].text() == "Running"
    assert view.current_values["ACTIVE TRANSACTION"].text() == "—"


@pytest.mark.parametrize("old_state", ["STOPPED", "FAILURE_REVIEW", "REVIEW_REQUIRED", "COMPLETED"])
def test_reenter_manual_clears_old_selection_and_actions(old_state):
    view = _view()
    view.set_execution_state(old_state, {"success": 37, "pending": 63},
                             execution_enabled=True, panel_attached=True)
    view.table.setRowCount(3)
    view.values["SOURCE ROWS"].setText("100")
    view.status_values["LAST SNAPSHOT"].setText("yesterday")
    view.reset_unselected_state(execution_enabled=False, panel_attached=False)
    assert view.status_values["MANUAL CYCLE"].text() == "No snapshot"
    assert view.execution_status.text() == "NO SNAPSHOT"
    assert view.status_values["LAST SNAPSHOT"].text() == "Never"
    assert view.table.rowCount() == 0
    assert all(value.text() == "0" for value in view.values.values())
    assert not any(_visible(view, key) for key in
                   ("stop", "resume", "retry", "finalize", "reconcile"))


def test_unselected_shared_context_is_immediately_authoritative():
    view = _view()
    view.reset_unselected_state(execution_enabled=True, panel_attached=True, panel_open=True)
    assert view.status_values["PANEL"].text() == "Attached"
    assert view.status_values["EXECUTION GATE"].text() == "ENABLED"
    view.reset_unselected_state(execution_enabled=False, panel_attached=False, panel_open=True)
    assert view.status_values["PANEL"].text() == "Open"
    assert view.status_values["EXECUTION GATE"].text() == "DISABLED"


def test_review_and_completed_actions_are_state_specific():
    view = _view()
    view.set_execution_state("FAILURE_REVIEW")
    assert _visible(view, "retry") and _visible(view, "finalize") and not _visible(view, "resume")
    view.set_execution_state("REVIEW_REQUIRED")
    assert _visible(view, "reconcile") and not _visible(view, "retry")
    view.set_execution_state("COMPLETED")
    assert not any(_visible(view, key) for key in ("start", "stop", "resume", "retry", "finalize", "reconcile"))


def test_signal_button_bindings_are_unchanged():
    view = _view(); calls = []
    bindings = (("start", view.start_requested), ("resume", view.resume_requested),
                ("retry", view.retry_requested), ("finalize", view.finalize_requested),
                ("reconcile", view.reconcile_requested))
    for key, signal in bindings:
        signal.connect(lambda key=key: calls.append(key))
        view.actions[key].setEnabled(True); view.actions[key].click()
    view.recover_requested.connect(lambda: calls.append("recover"))
    view.recover_button.setEnabled(True); view.recover_button.click()
    assert calls == ["start", "resume", "retry", "finalize", "reconcile", "recover"]
