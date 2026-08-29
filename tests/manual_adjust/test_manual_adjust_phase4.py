from datetime import datetime, timedelta, timezone

import pytest

from core.manual_adjust_controller import ManualAdjustController
from core.manual_adjust_models import ClassifiedSourceRow, SourceClassification
from core.manual_adjust_repository import ManualAdjustRepository
from core.panel_service import ManualSubmitOutcome, ManualSubmitResult
from core.panel_service import PanelService
from playwright.sync_api import TimeoutError as PWTimeout


def row(n, user, amount):
    return ClassifiedSourceRow(n, user, str(amount), "", user, user.lower(), amount,
                               SourceClassification.READY)


@pytest.fixture
def repo(tmp_path):
    value = ManualAdjustRepository(tmp_path / "manual.db"); value.initialize_schema()
    yield value; value.close()


class Panel:
    is_attached = True
    def __init__(self, results): self.results = list(results); self.calls = []
    def is_alive(self): return True
    def submit_adjustment(self, user, amount, remark, hook):
        self.calls.append((user, amount, remark)); hook("FORM_STARTED"); hook("SUBMIT_CLICK_BOUNDARY")
        return self.results.pop(0)
    def screenshot(self, path): pass


def snapshot(repo, count=2):
    return repo.create_snapshot("sheet", "MASTER", "fp", [row(i + 2, f"u{i}", 101 + i) for i in range(count)])


def config(enabled=True):
    return {"manual_adjust": {"execution_enabled": enabled, "remark": "MANUAL ADJUST",
                              "lease_timeout_sec": 120, "heartbeat_interval_sec": 10}}


def test_execution_gate_blocks_before_claim(repo):
    cid = snapshot(repo, 1); panel = Panel([]); ctl = ManualAdjustController(repo, panel, config(False))
    with pytest.raises(RuntimeError, match="disabled"):
        ctl.start(cid, confirmed=True)
    assert repo.get_pending_transactions(cid)[0].attempt_count == 0
    assert not panel.calls


def test_finite_success_and_failure_review(repo):
    cid = snapshot(repo)
    panel = Panel([ManualSubmitResult(ManualSubmitOutcome.SUCCESS, True, "SUCCESS_OBSERVED"),
                   ManualSubmitResult(ManualSubmitOutcome.FAILED_NOT_SUBMITTED, False, "AMOUNT_FILLED", "field")])
    ctl = ManualAdjustController(repo, panel, config()); ctl.start(cid, confirmed=True)
    assert ctl.step().state == "SUCCESS"
    assert ctl.step().state == "FAILED_NOT_SUBMITTED"
    assert ctl.step().state == "FAILURE_REVIEW"
    assert panel.calls == [("u0", 101, "MANUAL ADJUST"), ("u1", 102, "MANUAL ADJUST")]


def test_lease_exclusion_and_wrong_heartbeat(repo):
    cid = snapshot(repo, 1); repo.confirm_and_start(cid, "one", 120)
    with pytest.raises(ValueError): repo.heartbeat_cycle(cid, "two")
    with pytest.raises(ValueError): repo.confirm_and_start(cid, "two", 120)


def test_stale_submitting_is_unknown(repo):
    cid = snapshot(repo, 1); repo.confirm_and_start(cid, "one", 120)
    tx = repo.get_pending_transactions(cid)[0]; repo.claim_pending(tx.transaction_id, "one")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=500)).isoformat(timespec="seconds")
    repo._conn.execute("UPDATE manual_adjust_cycles SET lease_heartbeat_at=? WHERE cycle_id=?", (stale, cid))
    assert repo.recover_stale_cycle(cid, 120) == "REVIEW_REQUIRED"
    assert repo.get_transaction(tx.transaction_id).status.value == "UNKNOWN"


@pytest.mark.parametrize("phase,expected", [
    ("SUBMIT_CLICK_BOUNDARY", None), ("CLICK_RETURNED", 1),
])
def test_stale_recovery_uses_proven_click_phase(repo, phase, expected):
    cid = snapshot(repo, 1); repo.confirm_and_start(cid, "one", 120)
    tx = repo.get_pending_transactions(cid)[0]; attempt = repo.claim_pending(tx.transaction_id, "one")
    if phase == "SUBMIT_CLICK_BOUNDARY":
        repo.record_attempt_phase(attempt["attempt_id"], "one", "SUBMIT_CLICK_BOUNDARY")
    else:
        repo.record_attempt_phase(attempt["attempt_id"], "one", "SUBMIT_CLICK_BOUNDARY")
        repo.record_attempt_phase(attempt["attempt_id"], "one", "CLICK_RETURNED")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=500)).isoformat(timespec="seconds")
    repo._conn.execute("UPDATE manual_adjust_cycles SET lease_heartbeat_at=? WHERE cycle_id=?", (stale, cid))
    repo.recover_stale_cycle(cid, 120)
    assert repo.get_attempt_history(tx.transaction_id)[0]["click_crossed"] == expected


def test_retry_selected_creates_new_attempt(repo):
    cid = snapshot(repo, 1); repo.confirm_and_start(cid, "one", 120)
    tx = repo.get_pending_transactions(cid)[0]; attempt = repo.claim_pending(tx.transaction_id, "one")
    from core.manual_adjust_models import AttemptResult
    repo.finish_attempt(attempt["attempt_id"], AttemptResult.FAILED_NOT_SUBMITTED,
                        click_crossed=False, submission_phase="FORM_STARTED")
    assert repo.evaluate_cycle_destination(cid, "one") == "FAILURE_REVIEW"
    repo.prepare_failure_retries(cid, [tx.transaction_id]); repo.resume_cycle(cid, "two", 120)
    second = repo.claim_pending(tx.transaction_id, "two")
    assert second["attempt_no"] == 2
    assert repo.get_attempt_history(tx.transaction_id)[0]["attempt_id"] == attempt["attempt_id"]


def test_nonterminal_discovery_and_stop_resume(repo):
    cid = snapshot(repo, 2)
    assert [c["cycle_id"] for c in repo.list_nonterminal_cycles()] == [cid]
    repo.confirm_and_start(cid, "one", 120)
    assert repo.evaluate_cycle_destination(cid, "one", stopped=True) == "STOPPED"
    assert len(repo.get_pending_transactions(cid)) == 2
    repo.resume_cycle(cid, "two", 120)
    assert repo.get_cycle(cid)["status"] == "RUNNING"


def test_stale_pending_only_recovers_stopped(repo):
    cid = snapshot(repo, 1); repo.confirm_and_start(cid, "one", 120)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=500)).isoformat(timespec="seconds")
    repo._conn.execute("UPDATE manual_adjust_cycles SET lease_heartbeat_at=? WHERE cycle_id=?", (stale, cid))
    assert repo.recover_stale_cycle(cid, 120) == "STOPPED"
    assert len(repo.get_pending_transactions(cid)) == 1


def _unknown_in_review(repo):
    cid = snapshot(repo, 1); repo.confirm_and_start(cid, "one", 120)
    tx = repo.get_pending_transactions(cid)[0]; attempt = repo.claim_pending(tx.transaction_id, "one")
    from core.manual_adjust_models import AttemptResult
    repo.finish_attempt(attempt["attempt_id"], AttemptResult.UNKNOWN,
                        click_crossed=None, submission_phase="CLICK_UNCERTAIN")
    assert repo.evaluate_cycle_destination(cid, "one") == "REVIEW_REQUIRED"
    return cid, tx, attempt


@pytest.mark.parametrize("outcome,target", [("SUCCESS", "SUCCESS"),
                                               ("NOT_SUBMITTED", "FAILED_NOT_SUBMITTED")])
def test_reconciliation_is_review_only_and_write_once(repo, outcome, target):
    cid, tx, attempt = _unknown_in_review(repo)
    with pytest.raises(ValueError):
        repo.reconcile_unknown(tx.transaction_id, "wrong-attempt", outcome,
                               reconciled_by="op", note="checked", evidence="ledger")
    repo.reconcile_unknown(tx.transaction_id, attempt["attempt_id"], outcome,
                           reconciled_by="op", note="checked", evidence="ledger")
    assert repo.get_transaction(tx.transaction_id).status.value == target
    with pytest.raises(ValueError):
        repo.reconcile_unknown(tx.transaction_id, attempt["attempt_id"], outcome,
                               reconciled_by="op", note="again", evidence="ledger")


def test_unknown_is_not_automatically_retried(repo):
    cid, tx, _ = _unknown_in_review(repo)
    assert repo.get_pending_transactions(cid) == []
    assert repo.get_transaction(tx.transaction_id).status.value == "UNKNOWN"
    with pytest.raises(ValueError, match="STOPPED"):
        repo.resume_cycle(cid, "two", 120)


def test_remote_success_persistence_failure_hard_stops(repo, monkeypatch):
    cid = snapshot(repo, 2)
    panel = Panel([ManualSubmitResult(ManualSubmitOutcome.SUCCESS, True, "SUCCESS_OBSERVED")])
    ctl = ManualAdjustController(repo, panel, config()); ctl.start(cid, confirmed=True)
    monkeypatch.setattr(repo, "finish_attempt", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert ctl.step().state == "HARD_STOPPED"
    assert ctl.step().state == "HARD_STOPPED"
    assert len(panel.calls) == 1


def test_phase_durability_failure_hard_stops_before_click(repo, monkeypatch):
    cid = snapshot(repo, 2)
    class BoundaryPanel(Panel):
        def __init__(self): super().__init__([]); self.clicks = 0
        def submit_adjustment(self, user, amount, remark, hook):
            self.calls.append((user, amount, remark)); hook("FORM_STARTED")
            hook("SUBMIT_CLICK_BOUNDARY"); self.clicks += 1
    panel = BoundaryPanel(); ctl = ManualAdjustController(repo, panel, config()); ctl.start(cid, confirmed=True)
    original = repo.record_attempt_phase
    def fail_boundary(attempt, executor, phase):
        if phase == "SUBMIT_CLICK_BOUNDARY": raise OSError("disk")
        return original(attempt, executor, phase)
    monkeypatch.setattr(repo, "record_attempt_phase", fail_boundary)
    assert ctl.step().state == "HARD_STOPPED"
    assert panel.clicks == 0 and len(panel.calls) == 1


class FakeLocator:
    def __init__(self, page, selector): self.page, self.selector = page, selector
    @property
    def first(self): return self
    def wait_for(self, **kwargs):
        if self.page.field_failure == self.selector: raise PWTimeout("field")
    def click(self): pass
    def fill(self, value): self.page.fills[self.selector] = value
    def count(self): return 0
    def inner_text(self, **kwargs): return self.page.alert


class FakePage:
    def __init__(self, *, field_failure=None, click_failure=False, post_failure=False,
                 alert="Deposit telah disubmit"):
        self.field_failure, self.click_failure, self.post_failure = field_failure, click_failure, post_failure
        self.alert, self.fills, self.clicks = alert, {}, 0
    def wait_for_selector(self, selector, **kwargs):
        if self.field_failure == selector or (self.post_failure and selector == "ok"):
            raise PWTimeout("timeout")
    def locator(self, selector): return FakeLocator(self, selector)
    def click(self, selector):
        self.clicks += 1
        if self.click_failure: raise RuntimeError("click crashed")


def panel_with(page):
    selectors = {"panel": {"username": "user", "amount": "amount", "remark": "remark",
                           "submit": "submit", "success_alert": "ok"},
                 "success_text": "Deposit telah disubmit", "timeouts": {}}
    service = PanelService({}, selectors); service._page = page; service._attached = True
    service.is_alive = lambda: True
    return service


def test_panel_exact_fields_and_success():
    page = FakePage(); phases = []
    result = panel_with(page).submit_adjustment("CaseUser", 123456, "MANUAL ADJUST", phases.append)
    assert page.fills == {"user": "CaseUser", "amount": "123456", "remark": "MANUAL ADJUST"}
    assert result.outcome is ManualSubmitOutcome.SUCCESS and result.click_crossed is True
    assert phases.index("SUBMIT_CLICK_BOUNDARY") < phases.index("CLICK_RETURNED")


def test_panel_phase_failure_prevents_click():
    page = FakePage()
    def hook(phase):
        if phase == "SUBMIT_CLICK_BOUNDARY": raise OSError("db")
    result = panel_with(page).submit_adjustment("u", 1, "r", hook)
    assert result.outcome is ManualSubmitOutcome.FAILED_NOT_SUBMITTED
    assert result.click_crossed is False and page.clicks == 0


@pytest.mark.parametrize("page,outcome,click", [
    (FakePage(field_failure="user"), ManualSubmitOutcome.FAILED_NOT_SUBMITTED, False),
    (FakePage(click_failure=True), ManualSubmitOutcome.UNKNOWN, None),
    (FakePage(post_failure=True), ManualSubmitOutcome.UNKNOWN, True),
    (FakePage(alert="unexpected"), ManualSubmitOutcome.UNKNOWN, True),
])
def test_panel_conservative_failure_contract(page, outcome, click):
    result = panel_with(page).submit_adjustment("u", 1, "r")
    assert result.outcome is outcome and result.click_crossed is click
