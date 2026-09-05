"""PATCH-02 deterministic AUTO click-boundary and journal tests."""

from types import SimpleNamespace

import pytest

from core.database import DatabaseService
from core.memory_cache import MemoryCache
from core.panel_service import AutoSubmitOutcome, AutoSubmitResult, PanelService
from core.queue_manager import QueueManager
from core.validator import Validator
from tests.test_patch00_auto_baseline import RULES, Sheet, row, run_worker, worker_dashboard


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector, self.first = page, selector, self

    def wait_for(self, state="visible", **_):
        self.page.events.append(("locator-wait", self.selector, state))
        if self.selector == "#success" and state == "hidden":
            if not self.page.stale_clears:
                raise TimeoutError("stale success remains visible")
            self.page.stale_visible = False
        if self.page.fail == f"wait:{self.selector}":
            raise TimeoutError(self.selector)

    def is_visible(self):
        return self.selector == "#success" and self.page.stale_visible

    def count(self): return 1
    def click(self):
        if self.page.fail == f"click:{self.selector}": raise RuntimeError("field click")
    def fill(self, value):
        if value and self.page.fail == f"fill:{self.selector}": raise RuntimeError("fill")
        self.page.events.append(("fill", self.selector, value))
    def evaluate(self, _): return ""
    def select_option(self, **_): return None
    def inner_text(self, **_):
        if self.page.fail == "text": raise TimeoutError("text")
        return self.page.alert_text


class FakePage:
    def __init__(self, *, fail="", stale_visible=False, stale_clears=False,
                 alert_text="Deposit successful"):
        self.fail, self.stale_visible, self.stale_clears = fail, stale_visible, stale_clears
        self.alert_text, self.events, self.clicks = alert_text, [], 0

    def is_closed(self): return self.fail == "closed"
    def locator(self, selector): return FakeLocator(self, selector)
    def wait_for_selector(self, selector, **_):
        self.events.append(("page-wait", selector))
        if self.fail == f"wait:{selector}" or (selector == "#success" and self.fail == "success"):
            raise TimeoutError(selector)
    def click(self, selector):
        self.events.append(("submit", selector)); self.clicks += 1
        if self.fail == "submit": raise RuntimeError("uncertain click")


def service(page, success_text="Deposit successful"):
    selectors = {
        "panel": {"username": "#user", "amount": "#amount", "remark": "#remark",
                  "submit": "#submit", "success_alert": "#success"},
        "success_text": success_text,
        "timeouts": {"field_wait_ms": 5, "success_wait_ms": 5},
    }
    panel = PanelService({}, selectors)
    panel._page = page
    panel._context = SimpleNamespace(pages=[page])
    panel._attached = True
    return panel


@pytest.mark.parametrize("failure", [
    "closed", "detached", "wait:#user", "fill:#user", "fill:#amount", "fill:#remark",
])
def test_pre_click_failures_are_proven_not_submitted(failure):
    page = FakePage(fail="" if failure == "detached" else failure)
    panel = service(page)
    if failure == "detached": panel._attached = False
    result = panel.submit_deposit_classified("alice", 5000, "BONUS RELOAD AUTO")
    assert result.outcome is AutoSubmitOutcome.FAILED_NOT_SUBMITTED
    assert result.click_crossed is False and page.clicks == 0


def test_stale_visible_alert_must_clear_before_click():
    blocked = FakePage(stale_visible=True)
    result = service(blocked).submit_deposit_classified("a", 5000, "r")
    assert result.outcome is AutoSubmitOutcome.FAILED_NOT_SUBMITTED
    assert result.evidence == "STALE_SUCCESS_NOT_CLEARED" and blocked.clicks == 0

    clears = FakePage(stale_visible=True, stale_clears=True)
    result = service(clears).submit_deposit_classified("a", 5000, "r")
    assert result.outcome is AutoSubmitOutcome.SUCCESS and clears.clicks == 1


def test_click_boundary_order_and_uncertain_exception():
    page, phases = FakePage(fail="submit"), []
    result = service(page).submit_deposit_classified("a", 5000, "r", phases.append)
    assert phases[-2:] == ["SUBMIT_CLICK_BOUNDARY", "CLICK_UNCERTAIN"]
    assert page.events[-1] == ("submit", "#submit")
    assert result.outcome is AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT
    assert result.phase == "CLICK_UNCERTAIN" and not result.click_crossed


@pytest.mark.parametrize(("fail", "text", "expected"), [
    ("success", "Deposit successful", AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT),
    ("text", "Deposit successful", AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT),
    ("", "wrong response", AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT),
    ("", "DEPOSIT SUCCESSFUL today", AutoSubmitOutcome.SUCCESS),
])
def test_post_click_requires_fresh_matching_verifiable_success(fail, text, expected):
    page, phases = FakePage(fail=fail, alert_text=text), []
    result = service(page).submit_deposit_classified("a", 5000, "r", phases.append)
    assert page.clicks == 1 and result.click_crossed
    assert result.outcome is expected
    assert phases.index("SUBMIT_CLICK_BOUNDARY") < phases.index("CLICK_RETURNED")


def test_empty_success_text_accepts_fresh_visible_alert():
    result = service(FakePage(), "").submit_deposit_classified("a", 5000, "r")
    assert result.outcome is AutoSubmitOutcome.SUCCESS


def claim(db, tx="tx"):
    reserved = db.reserve_auto_transaction(tx, "alice", "2025-08-01", 50_000,
                                           5_000, "MASTER", "2025-08-01")
    db.mark_auto_submitting(tx, reserved["attempt_id"])
    return reserved


def test_journal_maps_all_three_outcomes_and_success_needs_click_proof(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed = claim(db, "failed")
    db.finalize_auto_failed_not_submitted("failed", failed["attempt_id"],
                                         "FAILED_NOT_SUBMITTED", "form", "FAILED_PRE_CLICK")
    assert db.get_auto_transaction("failed")["resolved_at"] is not None
    assert db.daily_bonus_exposure_for_transaction_date("alice", "2025-08-01") == 0

    unknown = claim(db, "unknown")
    db.record_auto_attempt_phase("unknown", unknown["attempt_id"], "SUBMIT_CLICK_BOUNDARY")
    db.mark_auto_unknown("unknown", "click raised", "CLICK_UNCERTAIN", "maybe dispatched")
    assert db.get_auto_transaction("unknown")["resolved_at"] is None
    assert db.daily_bonus_exposure_for_transaction_date("alice", "2025-08-01") == 5000

    success = db.reserve_auto_transaction("success", "bob", "2025-08-01", 50_000,
                                          5_000, "MASTER", "2025-08-01")
    db.mark_auto_submitting("success", success["attempt_id"])
    with pytest.raises(Exception): db.finalize_auto_success("success", "SUCCESS")
    assert not db.has_tx("success")
    db.record_auto_attempt_phase("success", success["attempt_id"], "CLICK_RETURNED")
    db.finalize_auto_success("success", "SUCCESS")
    attempt = db.get_auto_attempts("success")[0]
    assert attempt["click_crossed"] == 1 and attempt["submit_clicked_at"]
    assert db._conn.execute("SELECT count(*) FROM processed_transactions WHERE tx_id='success'").fetchone()[0] == 1


@pytest.mark.parametrize(("phase", "crossed", "expected"), [
    ("FORM_STARTED", False, "FAILED_NOT_SUBMITTED"),
    ("READY_TO_CLICK", False, "FAILED_NOT_SUBMITTED"),
    ("SUBMIT_CLICK_BOUNDARY", False, "UNKNOWN"),
    ("CLICK_UNCERTAIN", False, "UNKNOWN"),
    ("CLICK_RETURNED", True, "UNKNOWN"),
    ("WAITING_RESULT", True, "UNKNOWN"),
])
def test_startup_recovery_uses_persisted_click_phase(tmp_path, phase, crossed, expected):
    path = tmp_path / phase
    db = DatabaseService(str(path)); reserved = claim(db)
    db.record_auto_attempt_phase("tx", reserved["attempt_id"], phase)
    db.close(); reopened = DatabaseService(str(path))
    tx = reopened.get_auto_transaction("tx")
    assert tx["status"] == expected
    assert (tx["resolved_at"] is None) == (expected == "UNKNOWN")
    assert reopened.recover_auto_journal() == {"failed_not_submitted": 0, "unknown": 0}


def test_phase_failure_before_click_sets_accounting_error_and_zero_clicks():
    page = FakePage()
    def hook(_): raise OSError("database unavailable")
    result = service(page).submit_deposit_classified("a", 5000, "r", hook)
    assert result.outcome is AutoSubmitOutcome.FAILED_NOT_SUBMITTED
    assert result.accounting_error and page.clicks == 0


def test_click_returned_persistence_failure_is_unknown_accounting_error():
    page = FakePage()
    def hook(phase):
        if phase == "CLICK_RETURNED": raise OSError("disk")
    result = service(page).submit_deposit_classified("a", 5000, "r", hook)
    assert page.clicks == 1
    assert result.outcome is AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT
    assert result.accounting_error and result.phase == "CLICK_RETURNED"


def test_worker_failed_not_submitted_stop_boundary(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    manager = QueueManager(Sheet([row("one", "alice", 50_000), row("two", "bob", 50_000)]),
                           MemoryCache(), Validator(RULES), db); manager.refill()
    fake = None
    def submit(**_):
        fake.stop_requested = True
        return AutoSubmitResult(AutoSubmitOutcome.FAILED_NOT_SUBMITTED, False,
                                "FAILED_PRE_CLICK", "form failed")
    fake, _, finalised = worker_dashboard(
        db, manager, panel=SimpleNamespace(is_alive=lambda: True,
                                           submit_deposit_classified=submit))
    run_worker(fake)
    assert db.get_auto_transaction("one")["status"] == "FAILED_NOT_SUBMITTED"
    assert finalised == ["Worker stopped"] and manager.next_ready().tx_id == "two"
