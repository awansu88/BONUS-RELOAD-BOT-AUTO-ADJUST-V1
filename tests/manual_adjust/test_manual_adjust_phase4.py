from datetime import datetime, timedelta, timezone

import pytest

from core.manual_adjust_controller import ManualAdjustController
from core.manual_adjust_models import ClassifiedSourceRow, SourceClassification
from core.manual_adjust_repository import ManualAdjustRepository
from core.panel_service import ManualSubmitOutcome, ManualSubmitResult


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
