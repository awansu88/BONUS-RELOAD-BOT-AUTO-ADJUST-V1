"""PATCH-04: AUTO-only, non-financial panel self-recovery."""

from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

import core.panel_service as panel_module
from core.panel_service import PanelService
from core.recovery import DEFAULT_LADDER
from ui.manual_adjust_state import OperatingMode
sys.path.insert(0, str(Path(__file__).parent))
from test_patch00_auto_baseline import (dashboard_method, queue, row, run_worker,
                                        worker_dashboard)


SELECTORS = {
    "panel": {"username": "#username", "amount": "#amount", "remark": "#remark",
              "submit": "#submit", "success_alert": "#success"},
    "timeouts": {"field_wait_ms": 17},
}
CONFIG = {"panel_url": "https://panel.example/deposit",
          "browser": {"user_data_dir": "the-profile", "headless": False}}


class Page:
    def __init__(self, *, closed=False, wait_error=None, goto_error=None):
        self.closed = closed
        self.wait_error = wait_error
        self.goto_error = goto_error
        self.events = []
        self.main_frame = object()

    def is_closed(self): return self.closed
    def goto(self, url, **kwargs):
        self.events.append(("goto", url))
        if self.goto_error: raise self.goto_error
    def wait_for_selector(self, selector, **kwargs):
        self.events.append(("wait", selector, kwargs))
        if self.wait_error: raise self.wait_error
    def fill(self, *_): raise AssertionError("recovery filled a field")
    def click(self, *_): raise AssertionError("recovery clicked")
    def on(self, *_): pass


class Context:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.closed = 0
    def new_page(self):
        page = Page(); self.pages.append(page); return page
    def close(self): self.closed += 1
    def expose_binding(self, *_): pass
    def add_init_script(self, **_): pass
    def on(self, *_): pass


class DeadContext:
    def __init__(self): self.closed = 0
    @property
    def pages(self): raise RuntimeError("context closed")
    def close(self): self.closed += 1


class PW:
    def __init__(self, contexts, pages=None):
        self.contexts = contexts; self.pages = pages or []; self.stopped = 0
        self.chromium = self
    def launch_persistent_context(self, **kwargs):
        context = Context(self.pages)
        context.launch_kwargs = kwargs
        self.contexts.append(context)
        return context
    def stop(self): self.stopped += 1


class Starter:
    def __init__(self, pw): self.pw = pw
    def start(self): return self.pw


def service(monkeypatch, *, pages=None):
    contexts = []
    pw = PW(contexts, pages)
    monkeypatch.setattr(panel_module, "sync_playwright", lambda: Starter(pw))
    result = PanelService(CONFIG, SELECTORS)
    return result, contexts, pw


def test_recovery_relaunches_dead_context_and_reuses_url_and_profile(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    panel, contexts, _ = service(monkeypatch)
    dead = DeadContext()
    panel._context, panel._page, panel._pw = dead, Page(closed=True), SimpleNamespace(stop=lambda: None)
    panel.recover_auto_panel()
    assert dead.closed == 1
    assert len(contexts) == 1
    assert contexts[0].launch_kwargs["user_data_dir"] == "the-profile"
    assert panel._page.events == [("goto", CONFIG["panel_url"]),
                                  ("wait", "#username", {"timeout": 17, "state": "visible"})]
    assert panel.is_attached


def test_live_context_with_dead_page_creates_page_without_relaunch(monkeypatch):
    panel, contexts, _ = service(monkeypatch)
    context = Context([Page(closed=True)])
    panel._context, panel._page = context, context.pages[0]
    panel.recover_auto_panel()
    assert contexts == [] and len(context.pages) == 2 and panel._page is context.pages[-1]


@pytest.mark.parametrize("failure", ["navigation", "selector"])
def test_navigation_or_form_failure_is_not_recovered(monkeypatch, failure):
    page = Page(goto_error=RuntimeError("offline") if failure == "navigation" else None,
                wait_error=RuntimeError("login page") if failure == "selector" else None)
    panel, contexts, _ = service(monkeypatch, pages=[page])
    with pytest.raises(RuntimeError): panel.recover_auto_panel()
    assert not panel.is_attached
    assert len(contexts) == 1
    assert not any(event[0] in {"fill", "submit"} for event in page.events)


def test_failed_readiness_reuses_one_live_context_without_leak(monkeypatch):
    page = Page(wait_error=RuntimeError("missing form"))
    panel, contexts, _ = service(monkeypatch, pages=[page])
    for _ in range(3):
        with pytest.raises(RuntimeError): panel.recover_auto_panel()
    assert len(contexts) == 1


def test_recovery_navigation_failure_clears_previous_attachment(monkeypatch):
    panel, contexts, _ = service(monkeypatch)
    page = Page(goto_error=RuntimeError("navigation offline"))
    context = Context([page])
    panel._context, panel._page, panel._attached = context, page, True

    with pytest.raises(RuntimeError, match="navigation offline"):
        panel.recover_auto_panel()

    assert panel._attached is False and panel.is_attached is False
    assert panel._context is context and context.closed == 0 and contexts == []
    assert page.events == [("goto", CONFIG["panel_url"])]


def test_recovery_readiness_failure_never_leaves_panel_attached(monkeypatch):
    panel, contexts, _ = service(monkeypatch)
    page = Page(wait_error=RuntimeError("username form unavailable"))
    context = Context([page])
    panel._context, panel._page, panel._attached = context, page, True

    with pytest.raises(RuntimeError, match="username form unavailable"):
        panel.recover_auto_panel()

    assert panel._attached is False and panel.is_attached is False
    assert panel._context is context and context.closed == 0 and contexts == []
    assert page.events == [
        ("goto", CONFIG["panel_url"]),
        ("wait", "#username", {"timeout": 17, "state": "visible"}),
    ]


class Timer:
    def __init__(self): self.delays = []; self.active = False
    def start(self, delay): self.delays.append(delay); self.active = True
    def stop(self): self.active = False


class Label:
    def setText(self, text): self.text = text


class Dot: pass


def recovery_host(outcomes, state="running"):
    calls = []
    def recover():
        calls.append(len(calls) + 1)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception): raise outcome
    host = SimpleNamespace(
        state=state, manual_state=SimpleNamespace(mode=OperatingMode.AUTO),
        _recovery_active=False, _recovery_reason="", _recovery_resume_state=None,
        _recovery_attempt_index=0, _recovery_next_due=None, _recovery_last_error="",
        recovery_timer=Timer(), panel=SimpleNamespace(recover_auto_panel=recover, is_attached=True),
        logger=SimpleNamespace(warn=lambda *_: None, error=lambda *_: None, info=lambda *_: None),
        dot_panel=Dot(), dot_bot=Dot(), txt_panel=Label(), txt_bot=Label(),
        btn_start=SimpleNamespace(setEnabled=lambda *_: None),
        _panel_was_open=False, _set_dot=lambda *_: None,
        _auto_recover_panel=True,
    )
    globals_ = {"DEFAULT_LADDER": DEFAULT_LADDER, "OperatingMode": OperatingMode,
                "time": __import__("time")}
    bind = lambda name: dashboard_method(name, globals_).__get__(host)
    host._cancel_panel_recovery = bind("_cancel_panel_recovery")
    host._run_panel_recovery_attempt = bind("_run_panel_recovery_attempt")
    host._enter_panel_recovery = bind("_enter_panel_recovery")
    host._handle_active_panel_loss = bind("_handle_active_panel_loss")
    return host, calls


def test_first_attempt_immediate_and_fixed_nonblocking_ladder():
    host, calls = recovery_host([RuntimeError(str(i)) for i in range(6)])
    stopped = []
    host._on_panel_lost = lambda: None
    host._finalise_stop = lambda note: stopped.append(note)
    host._enter_panel_recovery("dead")
    while host.recovery_timer.active:
        host.recovery_timer.active = False
        host._run_panel_recovery_attempt()
    assert calls == [1, 2, 3, 4, 5, 6]
    assert host.recovery_timer.delays == [delay * 1000 for delay in DEFAULT_LADDER]
    assert stopped == ["Worker halted: panel recovery exhausted"]


def test_success_cancels_ladder_and_restores_monitoring():
    host, calls = recovery_host([RuntimeError("login"), None], "monitoring")
    host._enter_panel_recovery("dead")
    host._run_panel_recovery_attempt()
    assert calls == [1, 2] and host.state == "monitoring"
    assert not host._recovery_active and not host.recovery_timer.active
    assert host._panel_was_open


def test_duplicate_entry_does_not_restart_or_overlap():
    host, calls = recovery_host([RuntimeError("offline"), None])
    host._enter_panel_recovery("first")
    index = host._recovery_attempt_index
    host._enter_panel_recovery("duplicate")
    assert calls == [1] and host._recovery_attempt_index == index


def test_stop_during_recovery_cancels_immediately_and_once():
    host, _ = recovery_host([RuntimeError("offline")])
    host.stop_requested = False
    final = []
    host._finalise_stop = lambda note: final.append(note)
    host._on_stop = dashboard_method("_on_stop", {"OperatingMode": OperatingMode}).__get__(host)
    host._enter_panel_recovery("dead")
    host._on_stop()
    assert not host._recovery_active and not host.recovery_timer.active
    assert final == ["STOP requested during panel recovery"]


def test_recovery_source_has_no_blocking_sleep_or_financial_calls():
    names = dashboard_method("_run_panel_recovery_attempt", {"DEFAULT_LADDER": DEFAULT_LADDER}).__code__.co_names
    assert "sleep" not in names
    source_names = PanelService.recover_auto_panel.__code__.co_names
    assert not {"submit_deposit", "submit_deposit_classified", "submit_adjustment", "_fill"} & set(source_names)


def attach_recovery(worker, *, enabled=True):
    """Bind the shipped recovery methods to the established worker test host."""
    worker._auto_recover_panel = enabled
    worker._recovery_active = False
    worker._recovery_reason = ""
    worker._recovery_resume_state = None
    worker._recovery_attempt_index = 0
    worker._recovery_next_due = None
    worker._recovery_last_error = ""
    worker.recovery_timer = Timer()
    worker.dot_panel = worker.dot_bot = Dot()
    worker.txt_panel = worker.txt_bot = Label()
    worker.btn_start = SimpleNamespace(setEnabled=lambda *_: None)
    worker._set_dot = lambda *_: None
    worker._panel_was_open = True
    globals_ = {"DEFAULT_LADDER": DEFAULT_LADDER, "OperatingMode": OperatingMode,
                "time": __import__("time")}
    for name in ("_cancel_panel_recovery", "_run_panel_recovery_attempt",
                 "_enter_panel_recovery", "_handle_active_panel_loss"):
        setattr(worker, name, dashboard_method(name, globals_).__get__(worker))


def test_worker_pre_transaction_loss_recovers_before_queue_or_reservation(tmp_path):
    db, manager = queue(tmp_path, [row("TX-A", "alice", 50_000)])
    manager.refill()
    alive = {"value": False}
    submits, recoveries = [], []
    panel = SimpleNamespace(
        is_alive=lambda: alive["value"], is_attached=True,
        recover_auto_panel=lambda: recoveries.append(1),
        submit_deposit_classified=lambda **kw: submits.append(kw),
    )
    worker, _, _ = worker_dashboard(db, manager, panel=panel)
    attach_recovery(worker)
    run_worker(worker)
    assert worker.state == "running" and recoveries == [1]
    assert manager.next_ready().tx_id == "TX-A"
    assert db.get_auto_transaction("TX-A") is None and submits == []

    alive["value"] = True
    panel.submit_deposit_classified = lambda phase_hook=None, **kw: (
        submits.append(kw) or phase_hook("CLICK_RETURNED") or
        SimpleNamespace(outcome="SUCCESS", detail="", phase="SUCCESS_OBSERVED",
                        evidence="", click_crossed=True, accounting_error=False)
    )
    run_worker(worker)
    assert len(submits) == 1
    assert db.get_auto_transaction("TX-A")["attempt_count"] == 1


def test_worker_disabled_recovery_stops_before_consuming_ready_item(tmp_path):
    db, manager = queue(tmp_path, [row("TX-A", "alice", 50_000)])
    manager.refill()
    panel = SimpleNamespace(is_alive=lambda: False, is_attached=False,
                            recover_auto_panel=lambda: pytest.fail("recovery ran"))
    worker, _, finalised = worker_dashboard(db, manager, panel=panel)
    attach_recovery(worker, enabled=False)
    worker._on_panel_lost = lambda: None
    worker._finalise_stop = lambda note="": (finalised.append(note), setattr(worker, "state", "idle"))
    run_worker(worker)
    assert worker.state == "idle"
    assert finalised and db.get_auto_transaction("TX-A") is None
    assert manager.next_ready().tx_id == "TX-A" and not worker._recovery_active


def test_monitoring_loss_recovers_without_refill_or_metric_reset(tmp_path):
    db, manager = queue(tmp_path, [])
    manager.refill()
    calls = []
    panel = SimpleNamespace(is_alive=lambda: False, is_attached=True,
                            recover_auto_panel=lambda: calls.append("recover"))
    worker, _, _ = worker_dashboard(db, manager, panel=panel, state="monitoring")
    worker._processed_count = 7
    attach_recovery(worker)
    worker._handle_active_panel_loss("dead")
    assert worker.state == "monitoring" and calls == ["recover"]
    assert worker._processed_count == 7 and manager.ready_count() == 0


def test_fns_is_durable_before_recovery_and_has_no_same_step_retry(tmp_path):
    db, manager = queue(tmp_path, [row("TX-A", "alice", 50_000)])
    manager.refill()
    events, submits = [], []
    original = db.finalize_auto_failed_not_submitted
    db.finalize_auto_failed_not_submitted = lambda *a, **kw: (
        events.append("finalize") or original(*a, **kw))
    panel = SimpleNamespace(
        is_alive=lambda: True, is_attached=True,
        recover_auto_panel=lambda: events.append("recover"),
        submit_deposit_classified=lambda **kw: submits.append(1) or
            SimpleNamespace(outcome="FAILED_NOT_SUBMITTED", detail="selector timeout",
                            phase="FAILED_PRE_CLICK", evidence="", click_crossed=False,
                            accounting_error=False),
    )
    worker, _, _ = worker_dashboard(db, manager, panel=panel)
    attach_recovery(worker)
    run_worker(worker)
    assert events == ["finalize", "recover"] and submits == [1]
    assert db.get_auto_transaction("TX-A")["attempt_count"] == 1
    assert db.is_auto_retry_eligible("TX-A")


@pytest.mark.parametrize("outcome", ["FAILED_NOT_SUBMITTED", "UNKNOWN_AFTER_SUBMIT"])
def test_accounting_error_hard_stops_without_recovery(tmp_path, outcome):
    db, manager = queue(tmp_path, [row("TX-A", "alice", 50_000)])
    manager.refill()
    recoveries = []
    alive = {"value": True}
    def accounting_result(**kw):
        alive["value"] = False
        return SimpleNamespace(
            outcome=outcome, detail="database phase failed", phase="FAILED_PRE_CLICK",
            evidence="", click_crossed=outcome != "FAILED_NOT_SUBMITTED",
            accounting_error=True)
    panel = SimpleNamespace(
        is_alive=lambda: alive["value"], is_attached=True,
        recover_auto_panel=lambda: recoveries.append(1),
        submit_deposit_classified=accounting_result,
    )
    worker, _, finalised = worker_dashboard(db, manager, panel=panel)
    attach_recovery(worker)
    run_worker(worker)
    assert finalised and recoveries == []


def test_unknown_is_durable_and_nonretryable_before_future_recovery(tmp_path):
    db, manager = queue(tmp_path, [row("TX-A", "alice", 100_000)])
    manager.refill()
    alive, events = {"value": True}, []
    def submit(**kw):
        alive["value"] = False
        events.append("submit")
        return SimpleNamespace(outcome="UNKNOWN_AFTER_SUBMIT", detail="ambiguous",
                               phase="CLICK_UNCERTAIN", evidence="", click_crossed=False,
                               accounting_error=False)
    panel = SimpleNamespace(is_alive=lambda: alive["value"], is_attached=True,
                            recover_auto_panel=lambda: events.append("recover"),
                            submit_deposit_classified=submit)
    worker, _, _ = worker_dashboard(db, manager, panel=panel)
    attach_recovery(worker)
    run_worker(worker)
    tx = db.get_auto_transaction("TX-A")
    assert events == ["submit", "recover"]
    assert tx["status"] == "UNKNOWN" and tx["resolved_at"] is None
    assert tx["attempt_count"] == 1 and not db.is_auto_retry_eligible("TX-A")


@pytest.mark.parametrize("delay_index", [1, 4])
def test_stop_at_recovery_ladder_positions_cancels_without_finance(delay_index):
    host, calls = recovery_host([RuntimeError("offline")] * 6)
    final = []
    host.stop_requested = False
    host._finalise_stop = lambda note: final.append(note)
    host._on_stop = dashboard_method("_on_stop", {"OperatingMode": OperatingMode}).__get__(host)
    host._enter_panel_recovery("dead")
    while host._recovery_attempt_index < delay_index:
        host._run_panel_recovery_attempt()
    host._on_stop()
    before = len(calls)
    host._run_panel_recovery_attempt()
    assert len(calls) == before and final == ["STOP requested during panel recovery"]


def test_recovering_is_active_for_database_and_maintenance(monkeypatch):
    seen = []
    class Dialog:
        def __init__(self, *a, **kw): seen.append(kw["worker_running"])
        def exec(self): pass
    host = SimpleNamespace(state="recovering", db=object(), maintenance_service=object(),
        health_monitor=object(), app_dir=Path("."), resource_dir=Path("."),
        config_path=Path("config.json"), credentials_path=Path("credentials.json"),
        config={"browser": {}}, selectors={})
    dashboard_method("_open_database", {"DatabaseDialog": Dialog})(host)
    monkeypatch.setitem(sys.modules, "ui.maintenance_center",
                        SimpleNamespace(MaintenanceCenter=Dialog))
    host.db = SimpleNamespace(path="db.sqlite")
    dashboard_method("_open_maintenance_center", {"Path": Path})(host)
    assert seen == [True, True]


def test_close_event_cancels_recovery_before_checkpoint_and_panel_close():
    events = []
    timer = Timer(); timer.start(5000)
    host = SimpleNamespace(
        recovery_timer=timer, _recovery_active=True, _recovery_reason="dead",
        _recovery_resume_state="running", _recovery_attempt_index=1,
        _recovery_next_due=1.0, _recovery_last_error="offline",
        manual_controller=None,
        db=SimpleNamespace(checkpoint_wal=lambda *_: events.append("checkpoint")),
        panel=SimpleNamespace(close=lambda: events.append("panel-close")),
        _persist_crash_state=lambda **kw: events.append("persist"),
    )
    for name in ("manual_timer", "worker_timer", "panel_timer", "metrics_timer",
                 "watchdog_timer", "manual_worker_timer", "manual_heartbeat_timer"):
        setattr(host, name, Timer())
    host._cancel_panel_recovery = dashboard_method("_cancel_panel_recovery").__get__(host)
    event = SimpleNamespace(accept=lambda: events.append("accept"))
    with pytest.raises(RuntimeError, match="__class__ cell"):
        dashboard_method("closeEvent", {"QTimer": Timer})(host, event)
    assert not timer.active and not host._recovery_active
    assert events[:3] == ["checkpoint", "panel-close", "persist"]
