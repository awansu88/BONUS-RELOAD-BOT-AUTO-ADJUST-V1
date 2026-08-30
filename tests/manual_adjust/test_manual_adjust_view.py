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
    view.set_execution_state("PREVIEW", {"pending": 2}, execution_enabled=True, panel_attached=True)
    assert all(_visible(view, key) for key in ("load", "open_panel", "attach_panel", "start"))
    assert view.actions["start"].isEnabled()


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
