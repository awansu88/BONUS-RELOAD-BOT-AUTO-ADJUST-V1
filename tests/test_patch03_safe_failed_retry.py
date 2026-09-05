"""PATCH-03: bounded retry only for durably proven pre-submit failures."""

from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

from core.database import DatabaseService
from core.memory_cache import MemoryCache
from core.panel_service import AutoSubmitOutcome, AutoSubmitResult
from core.queue_manager import QueueManager
from core.validator import Validator
sys.path.insert(0, str(Path(__file__).parent))
from test_patch00_auto_baseline import RULES, Sheet, row, run_worker, worker_dashboard


DAY = "2025-08-01"


def failed_once(db, tx="tx", user="alice", amount=100_000, bonus=10_000):
    claim = db.reserve_auto_transaction(tx, user, DAY, amount, bonus, "MASTER", f"{DAY} 10:00")
    db.mark_auto_failed_not_submitted(tx, "before remote call")
    return claim


def manager(db, rows):
    return QueueManager(Sheet(rows), MemoryCache(), Validator(RULES), db)


def test_failed_not_submitted_attempt_one_is_known_and_retryable(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    assert db.has_known_auto_tx("tx")
    assert db.is_auto_retry_eligible("tx")


@pytest.mark.parametrize("status", ["UNKNOWN", "SUCCESS", "PENDING", "SUBMITTING", "CANCELLED"])
@pytest.mark.parametrize("crossed", [0, 1])
def test_non_failed_transaction_states_are_never_retryable(tmp_path, status, crossed):
    db = DatabaseService(str(tmp_path / f"{status}-{crossed}"))
    failed_once(db)
    db._conn.execute("UPDATE auto_adjust_transactions SET status=? WHERE tx_id='tx'", (status,))
    db._conn.execute("UPDATE auto_adjust_attempts SET click_crossed=? WHERE tx_id='tx'", (crossed,))
    assert not db.is_auto_retry_eligible("tx")


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE auto_adjust_transactions SET attempt_count=2 WHERE tx_id='tx'",
        "UPDATE auto_adjust_attempts SET click_crossed=1 WHERE tx_id='tx'",
        "UPDATE auto_adjust_attempts SET submit_clicked_at='2025-08-01T10:01:00' WHERE tx_id='tx'",
        "UPDATE auto_adjust_attempts SET result='UNKNOWN' WHERE tx_id='tx'",
    ],
)
def test_corrupt_or_exhausted_failed_state_is_not_retryable(tmp_path, mutation):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    db._conn.execute(mutation)
    assert not db.is_auto_retry_eligible("tx")


@pytest.mark.parametrize("legacy_result", ["FAILED", "SUCCESS", "LIMIT", "INVALID", "MANUAL BONUS"])
def test_any_legacy_outcome_permanently_blocks_retry(tmp_path, legacy_result):
    db = DatabaseService(str(tmp_path / "db"))
    if legacy_result == "SUCCESS":
        # Construct the legacy row first because SUCCESS finalization is not
        # part of this corruption/legacy compatibility test.
        db.insert("tx", "alice", 100_000, 10_000, legacy_result, "MASTER", DAY)
        assert db.reserve_auto_transaction("tx", "alice", DAY, 100_000, 10_000) is None
    else:
        failed_once(db)
        db.insert("tx", "alice", 100_000, 0, legacy_result, "MASTER", DAY)
    assert not db.is_auto_retry_eligible("tx")
    q = manager(db, [row("tx", "alice", 100_000)])
    q.refill()
    assert q.preview_items() == [] and q.next_ready() is None


def test_queue_distinguishes_new_retry_and_nonretryable_known(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db, "retry")
    failed_once(db, "unknown", "bob")
    db._conn.execute("UPDATE auto_adjust_transactions SET status='UNKNOWN' WHERE tx_id='unknown'")
    db._conn.execute("UPDATE auto_adjust_attempts SET result='UNKNOWN' WHERE tx_id='unknown'")
    q = manager(db, [row("new", "carol", 50_000), row("retry", "alice", 100_000),
                     row("unknown", "bob", 50_000)])
    q.refill()
    items = q.preview_items()
    assert [(item.tx_id, item.retry_attempt) for item in items] == [("new", False), ("retry", True)]


def test_retry_claim_atomically_creates_new_attempt_and_keeps_first_immutable(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    first = failed_once(db)
    before = dict(db.get_auto_attempts("tx")[0])
    second = db.reserve_auto_retry_transaction("tx", "alice", DAY, 100_000, 10_000)
    tx = db.get_auto_transaction("tx")
    attempts = db.get_auto_attempts("tx")
    assert second and second["attempt_id"] != first["attempt_id"]
    assert tx["attempt_count"] == 2 and tx["current_attempt_id"] == second["attempt_id"]
    assert tx["status"] == "PENDING" and tx["resolved_at"] is None
    assert attempts[0] == before
    assert (attempts[1]["attempt_no"], attempts[1]["result"], attempts[1]["submission_phase"]) == (2, "IN_PROGRESS", "RESERVED")
    assert db.reserve_auto_retry_transaction("tx", "alice", DAY, 100_000, 10_000) is None


@pytest.mark.parametrize("field,value", [("username", "Alice2"), ("amount", 50_000), ("day", "2025-08-02")])
def test_retry_source_mismatch_has_no_attempt_two_or_remote_call(tmp_path, field, value):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    args = {"username": "alice", "business_date": DAY, "deposit_amount": 100_000}
    keys = {"username": "username", "amount": "deposit_amount", "day": "business_date"}
    args[keys[field]] = value
    assert db.reserve_auto_retry_transaction("tx", requested_bonus=10_000, **args) is None
    assert len(db.get_auto_attempts("tx")) == 1


def test_retry_recomputes_and_clamps_new_quota(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    db.insert("other", "alice", 50_000, 5_000, "SUCCESS", "MASTER", DAY)
    claim = db.reserve_auto_retry_transaction("tx", "alice", DAY, 100_000, 10_000)
    assert claim["reserved_bonus"] == 5_000
    assert db.daily_bonus_exposure_for_transaction_date("alice", DAY) == 10_000


def test_full_current_exposure_blocks_retry_claim(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    db.insert("other", "alice", 100_000, 10_000, "SUCCESS", "MASTER", DAY)
    assert db.reserve_auto_retry_transaction("tx", "alice", DAY, 100_000, 10_000) is None
    assert len(db.get_auto_attempts("tx")) == 1


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (AutoSubmitOutcome.SUCCESS, "SUCCESS"),
        (AutoSubmitOutcome.FAILED_NOT_SUBMITTED, "FAILED_NOT_SUBMITTED"),
        (AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT, "UNKNOWN"),
    ],
)
def test_later_refill_runs_exactly_one_second_attempt_and_never_third(tmp_path, outcome, expected):
    db = DatabaseService(str(tmp_path / "db"))
    q1 = manager(db, [row("tx", "alice", 100_000)])
    q1.refill()
    first_calls = []
    panel1 = SimpleNamespace(
        is_alive=lambda: True,
        submit_deposit_classified=lambda **kwargs: first_calls.append(kwargs) or AutoSubmitResult(
            AutoSubmitOutcome.FAILED_NOT_SUBMITTED, False, "FAILED_PRE_CLICK", "safe"
        ),
    )
    worker1, _, _ = worker_dashboard(db, q1, panel=panel1)
    run_worker(worker1)
    run_worker(worker1)
    assert len(first_calls) == 1 and len(db.get_auto_attempts("tx")) == 1

    q2 = manager(db, [row("tx", "alice", 100_000)])
    q2.refill()
    assert q2.next_ready().retry_attempt
    second_calls = []

    def submit(phase_hook=None, **kwargs):
        second_calls.append(kwargs)
        if outcome is not AutoSubmitOutcome.FAILED_NOT_SUBMITTED:
            phase_hook("CLICK_RETURNED" if outcome is AutoSubmitOutcome.SUCCESS else "SUBMIT_CLICK_BOUNDARY")
        return AutoSubmitResult(outcome, outcome is AutoSubmitOutcome.SUCCESS,
                                "FINISHED" if outcome is AutoSubmitOutcome.SUCCESS else "FAILED_PRE_CLICK", "result")

    worker2, _, _ = worker_dashboard(db, q2, panel=SimpleNamespace(is_alive=lambda: True, submit_deposit_classified=submit))
    run_worker(worker2)
    assert len(second_calls) == 1
    assert db.get_auto_transaction("tx")["status"] == expected
    assert [a["result"] for a in db.get_auto_attempts("tx")] == ["FAILED_NOT_SUBMITTED", expected]
    q3 = manager(db, [row("tx", "alice", 100_000)])
    q3.refill()
    assert q3.next_ready() is None
    if expected == "SUCCESS":
        assert db._conn.execute("SELECT COUNT(*) FROM processed_transactions WHERE tx_id='tx'").fetchone()[0] == 1
    elif expected == "UNKNOWN":
        assert db.daily_bonus_exposure_for_transaction_date("alice", DAY) == 10_000
    else:
        assert db.daily_bonus_exposure_for_transaction_date("alice", DAY) == 0


def test_attempt_two_recovery_is_idempotent_and_never_retryable(tmp_path):
    path = tmp_path / "db"
    db = DatabaseService(str(path))
    failed_once(db)
    db.reserve_auto_retry_transaction("tx", "alice", DAY, 100_000, 10_000)
    db.close()
    reopened = DatabaseService(str(path))
    assert reopened.get_auto_transaction("tx")["status"] == "FAILED_NOT_SUBMITTED"
    assert reopened.get_auto_transaction("tx")["attempt_count"] == 2
    assert not reopened.is_auto_retry_eligible("tx")
    assert reopened.recover_auto_journal() == {"failed_not_submitted": 0, "unknown": 0}


def test_retry_claim_database_failure_halts_before_panel(tmp_path, monkeypatch):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    q = manager(db, [row("tx", "alice", 100_000)])
    q.refill()
    monkeypatch.setattr(db, "reserve_auto_retry_transaction", lambda **_: (_ for _ in ()).throw(OSError("disk")))
    worker, calls, stopped = worker_dashboard(db, q)
    run_worker(worker)
    assert calls == [] and stopped == ["Worker halted: AUTO reservation database failure"]


def test_manual_bonus_added_before_retry_blocks_remote_and_future_retry(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    q = manager(db, [row("tx", "alice", 100_000)])
    q.refill()
    worker, calls, _ = worker_dashboard(db, q, refresh=lambda: worker.cache.set_manual({"alice"}))
    run_worker(worker)
    assert calls == [] and db.has_tx("tx") and not db.is_auto_retry_eligible("tx")


def test_retry_fresh_limit_blocks_remote_and_future_retry(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    q = manager(db, [row("tx", "alice", 100_000)])
    q.refill()
    first_before = dict(db.get_auto_attempts("tx")[0])
    # This award lands after preview, so only the shipped worker's fresh
    # transaction-date validation can observe it.
    db.insert("new-award", "alice", 100_000, 10_000, "SUCCESS", "MASTER", DAY)
    worker, calls, _ = worker_dashboard(db, q)
    run_worker(worker)
    assert calls == []
    assert db.get_auto_attempts("tx") == [first_before]
    assert db._conn.execute(
        "SELECT result FROM processed_transactions WHERE tx_id='tx'"
    ).fetchone() == ("LIMIT",)
    assert not db.is_auto_retry_eligible("tx")


def test_retry_fresh_invalid_blocks_remote_and_future_retry(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    q = manager(db, [row("tx", "alice", 100_000)])
    q.refill()
    first_before = dict(db.get_auto_attempts("tx")[0])
    # The worker validates its current MASTER-derived QueueItem again.  An
    # amount below the frozen 50k tier is deterministically INVALID.
    q.next_ready().amount = 49_999
    worker, calls, _ = worker_dashboard(db, q)
    run_worker(worker)
    assert calls == []
    assert db.get_auto_attempts("tx") == [first_before]
    assert db._conn.execute(
        "SELECT result FROM processed_transactions WHERE tx_id='tx'"
    ).fetchone() == ("INVALID",)
    assert not db.is_auto_retry_eligible("tx")


def test_worker_retry_source_mismatch_never_reaches_panel(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db, amount=100_000)
    first_before = dict(db.get_auto_attempts("tx")[0])
    q = manager(db, [row("tx", "alice", 50_000)])
    q.refill()
    assert q.next_ready().retry_attempt
    worker, calls, _ = worker_dashboard(db, q)
    run_worker(worker)
    assert calls == [] and db.get_auto_attempts("tx") == [first_before]
    tx = db.get_auto_transaction("tx")
    assert tx["status"] == "FAILED_NOT_SUBMITTED" and tx["attempt_count"] == 1


def test_worker_retry_full_quota_blocks_panel(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    q = manager(db, [row("tx", "alice", 100_000)])
    q.refill()
    # Simulate a concurrent award after fresh validation but before the atomic
    # retry claim, forcing the authoritative reservation guard to reject it.
    original = db.reserve_auto_retry_transaction

    def concurrent_award_then_claim(**kwargs):
        db.insert("concurrent", "alice", 100_000, 10_000, "SUCCESS", "MASTER", DAY)
        return original(**kwargs)

    db.reserve_auto_retry_transaction = concurrent_award_then_claim
    worker, calls, _ = worker_dashboard(db, q)
    run_worker(worker)
    assert calls == [] and len(db.get_auto_attempts("tx")) == 1
    assert db.get_auto_transaction("tx")["status"] == "FAILED_NOT_SUBMITTED"


def _crash_attempt_two(path, phase):
    db = DatabaseService(str(path))
    failed_once(db)
    claim = db.reserve_auto_retry_transaction("tx", "alice", DAY, 100_000, 10_000)
    db.mark_auto_submitting("tx", claim["attempt_id"])
    db.record_auto_attempt_phase("tx", claim["attempt_id"], phase)
    db.close()
    return DatabaseService(str(path))


def test_attempt_two_preclick_submitting_crash_recovers_failed_not_submitted(tmp_path):
    db = _crash_attempt_two(tmp_path / "db", "FORM_STARTED")
    tx = db.get_auto_transaction("tx")
    attempts = db.get_auto_attempts("tx")
    assert tx["status"] == "FAILED_NOT_SUBMITTED" and tx["resolved_at"] is not None
    assert tx["attempt_count"] == 2 and attempts[1]["result"] == "FAILED_NOT_SUBMITTED"
    assert not db.is_auto_retry_eligible("tx")
    assert db.recover_auto_journal() == {"failed_not_submitted": 0, "unknown": 0}


def test_attempt_two_click_boundary_crash_recovers_unknown(tmp_path):
    db = _crash_attempt_two(tmp_path / "db", "SUBMIT_CLICK_BOUNDARY")
    tx = db.get_auto_transaction("tx")
    attempts = db.get_auto_attempts("tx")
    assert tx["status"] == "UNKNOWN" and tx["resolved_at"] is None
    assert tx["attempt_count"] == 2 and attempts[1]["result"] == "UNKNOWN"
    assert db.daily_bonus_exposure_for_transaction_date("alice", DAY) == 10_000
    assert not db.is_auto_retry_eligible("tx")


def test_attempt_two_postclick_crash_recovers_unknown(tmp_path):
    db = _crash_attempt_two(tmp_path / "db", "CLICK_RETURNED")
    tx = db.get_auto_transaction("tx")
    attempt = db.get_auto_attempts("tx")[1]
    assert tx["status"] == "UNKNOWN" and tx["resolved_at"] is None
    assert tx["attempt_count"] == 2 and attempt["result"] == "UNKNOWN"
    assert attempt["click_crossed"] == 1 and attempt["submit_clicked_at"] is not None
    assert db.daily_bonus_exposure_for_transaction_date("alice", DAY) == 10_000
    assert not db.is_auto_retry_eligible("tx")


def test_retry_mark_submitting_failure_halts_before_panel(tmp_path, monkeypatch):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db, "TX-A")
    q = manager(db, [row("TX-A", "alice", 100_000), row("TX-B", "bob", 50_000)])
    q.refill()
    monkeypatch.setattr(
        db, "mark_auto_submitting", lambda *_: (_ for _ in ()).throw(OSError("disk"))
    )
    worker, calls, stopped = worker_dashboard(db, q)
    run_worker(worker)
    tx = db.get_auto_transaction("TX-A")
    assert calls == [] and stopped == ["Worker halted: AUTO journal database failure"]
    assert tx["status"] == "FAILED_NOT_SUBMITTED" and tx["attempt_count"] == 2
    assert len(db.get_auto_attempts("TX-A")) == 2 and not db.is_auto_retry_eligible("TX-A")
    tx_b = next(item for item in q.preview_items() if item.tx_id == "TX-B")
    assert not tx_b.processed


def test_retry_outcome_accounting_failure_hard_stops_before_next_tx(tmp_path, monkeypatch):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db, "TX-A")
    q = manager(db, [row("TX-A", "alice", 100_000), row("TX-B", "bob", 50_000)])
    q.refill()
    monkeypatch.setattr(
        db, "finalize_auto_success", lambda *_: (_ for _ in ()).throw(OSError("disk"))
    )
    worker, calls, stopped = worker_dashboard(db, q)
    run_worker(worker)
    tx = db.get_auto_transaction("TX-A")
    assert len(calls) == 1 and stopped == ["Worker halted: AUTO accounting state lost"]
    assert tx["status"] == "SUBMITTING" and tx["attempt_count"] == 2
    assert db.get_auto_attempts("TX-A")[1]["click_crossed"] == 1
    assert not any(item.processed for item in q.preview_items() if item.tx_id == "TX-B")


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (AutoSubmitOutcome.SUCCESS, "SUCCESS"),
        (AutoSubmitOutcome.FAILED_NOT_SUBMITTED, "FAILED_NOT_SUBMITTED"),
        (AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT, "UNKNOWN"),
    ],
)
def test_stop_during_retry_finalises_boundary_without_consuming_tx_b(tmp_path, outcome, expected):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db, "TX-A")
    q = manager(db, [row("TX-A", "alice", 100_000), row("TX-B", "bob", 50_000)])
    q.refill()
    calls = []
    worker = None

    def submit(phase_hook=None, **kwargs):
        calls.append(kwargs)
        worker.stop_requested = True
        if outcome is AutoSubmitOutcome.SUCCESS:
            phase_hook("CLICK_RETURNED")
        elif outcome is AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT:
            phase_hook("SUBMIT_CLICK_BOUNDARY")
        return AutoSubmitResult(
            outcome,
            outcome is AutoSubmitOutcome.SUCCESS,
            "FINISHED" if outcome is AutoSubmitOutcome.SUCCESS else "FAILED_PRE_CLICK",
            "classified",
        )

    panel = SimpleNamespace(is_alive=lambda: True, submit_deposit_classified=submit)
    worker, _, stopped = worker_dashboard(db, q, panel=panel)
    run_worker(worker)
    assert len(calls) == 1 and db.get_auto_transaction("TX-A")["status"] == expected
    assert stopped == ["Worker stopped"]
    tx_b = next(item for item in q.preview_items() if item.tx_id == "TX-B")
    assert not tx_b.processed
