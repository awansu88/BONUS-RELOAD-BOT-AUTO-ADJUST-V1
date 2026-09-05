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
from test_patch00_auto_baseline import dashboard_method


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

    def is_closed(self): return self.closed
    def goto(self, url, **kwargs):
        self.events.append(("goto", url))
        if self.goto_error: raise self.goto_error
    def wait_for_selector(self, selector, **kwargs):
        self.events.append(("wait", selector, kwargs))
        if self.wait_error: raise self.wait_error
    def fill(self, *_): raise AssertionError("recovery filled a field")
    def click(self, *_): raise AssertionError("recovery clicked")


class Context:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.closed = 0
    def new_page(self):
        page = Page(); self.pages.append(page); return page
    def close(self): self.closed += 1


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
    )
    globals_ = {"DEFAULT_LADDER": DEFAULT_LADDER, "OperatingMode": OperatingMode,
                "time": __import__("time")}
    bind = lambda name: dashboard_method(name, globals_).__get__(host)
    host._cancel_panel_recovery = bind("_cancel_panel_recovery")
    host._run_panel_recovery_attempt = bind("_run_panel_recovery_attempt")
    host._enter_panel_recovery = bind("_enter_panel_recovery")
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
