"""PATCH-01 journal tests. All panel and database files are local fakes."""

from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

from core.database import DatabaseService
from core.memory_cache import MemoryCache
from core.queue_manager import QueueManager
from core.validator import Validator
sys.path.insert(0, str(Path(__file__).parent))
from test_patch00_auto_baseline import RULES, Sheet, row, run_worker, worker_dashboard


def reserve(db, tx="tx", user="alice", day="2025-08-01", bonus=10_000):
    return db.reserve_auto_transaction(
        tx, user, day, 100_000, bonus, "MASTER", f"{day} 10:00"
    )


def test_additive_schema_and_idempotent_legacy_migration(tmp_path):
    path = tmp_path / "processed.db"
    db = DatabaseService(str(path))
    db.insert("legacy", "alice", 50_000, 0, "FAILED", "MASTER", "2025-08-01")
    expected = {r[0] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"processed_transactions", "auto_adjust_transactions", "auto_adjust_attempts"} <= expected
    db.close()
    reopened = DatabaseService(str(path))
    assert reopened._conn.execute(
        "SELECT result FROM processed_transactions WHERE tx_id='legacy'"
    ).fetchone() == ("FAILED",)
    reopened.close()


@pytest.mark.parametrize("bonus", [5_000, 10_000])
def test_atomic_reservation_creates_pending_attempt_and_deduplicates(tmp_path, bonus):
    db = DatabaseService(str(tmp_path / "db"))
    claim = reserve(db, bonus=bonus)
    assert db.get_auto_transaction("tx")["reserved_bonus"] == bonus
    assert db.get_auto_transaction("tx")["status"] == "PENDING"
    attempt = db.get_auto_attempts("tx")[0]
    assert attempt["result"] == "IN_PROGRESS" and attempt["click_crossed"] == 0
    assert reserve(db, bonus=bonus) is None
    assert db.has_known_auto_tx("tx")


def test_success_and_reservation_exposure_is_clamped_and_never_double_counted(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db.insert("old", "alice", 50_000, 5_000, "SUCCESS", "MASTER", "2025-08-01")
    claim = reserve(db)
    assert claim["reserved_bonus"] == 5_000
    assert db.daily_bonus_exposure_for_transaction_date("alice", "2025-08-01") == 10_000
    db.mark_auto_submitting("tx", claim["attempt_id"])
    db.finalize_auto_success("tx")
    assert db.get_auto_transaction("tx")["status"] == "SUCCESS"
    assert db.get_auto_attempts("tx")[0]["result"] == "SUCCESS"
    assert db.daily_bonus_exposure_for_transaction_date("alice", "2025-08-01") == 10_000
    assert reserve(db, tx="blocked") is None


def test_unknown_reserves_by_username_and_business_date_and_queue_uses_it(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    claim = reserve(db, bonus=5_000)
    db.mark_auto_submitting("tx", claim["attempt_id"])
    db.mark_auto_unknown("tx", "binary failure")
    manager = QueueManager(
        Sheet([row("new", "alice", 100_000), row("tomorrow", "alice", 100_000, "2025-08-02")]),
        MemoryCache(), Validator(RULES), db,
    )
    manager.refill()
    assert [(x.status, x.bonus) for x in manager.preview_items()] == [
        ("READY", 5_000), ("READY", 10_000)
    ]


def test_unknown_full_reservation_makes_next_same_date_limit(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    claim = reserve(db)
    db.mark_auto_submitting("tx", claim["attempt_id"])
    db.mark_auto_unknown("tx")
    manager = QueueManager(
        Sheet([row("new", "alice", 50_000)]), MemoryCache(), Validator(RULES), db
    )
    manager.refill()
    assert [(x.status, x.bonus) for x in manager.preview_items()] == [("LIMIT", 0)]


def test_failed_not_submitted_releases_quota_but_remains_known(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    claim = reserve(db)
    db.mark_auto_failed_not_submitted("tx", "local failure")
    assert db.daily_bonus_exposure_for_transaction_date("alice", "2025-08-01") == 0
    assert db.has_known_auto_tx("tx") and reserve(db) is None


def test_close_reopen_recovers_pending_and_submitting_idempotently(tmp_path):
    path = tmp_path / "db"
    db = DatabaseService(str(path))
    reserve(db, tx="pending", bonus=5_000)
    claim = reserve(db, tx="submitting", user="bob", bonus=10_000)
    db.mark_auto_submitting("submitting", claim["attempt_id"])
    db.close()
    reopened = DatabaseService(str(path))
    assert reopened.get_auto_transaction("pending")["status"] == "FAILED_NOT_SUBMITTED"
    assert reopened.get_auto_transaction("submitting")["status"] == "UNKNOWN"
    assert reopened.daily_bonus_exposure_for_transaction_date("alice", "2025-08-01") == 0
    assert reopened.daily_bonus_exposure_for_transaction_date("bob", "2025-08-01") == 10_000
    assert reopened.recover_auto_journal() == {"failed_not_submitted": 0, "unknown": 0}


@pytest.mark.parametrize("behavior", ["false", "raise"])
def test_worker_binary_non_success_is_unknown_without_legacy_failed(tmp_path, behavior):
    db = DatabaseService(str(tmp_path / "db"))
    manager = QueueManager(Sheet([row("ambiguous", "alice", 50_000)]), MemoryCache(), Validator(RULES), db)
    manager.refill()
    calls = []
    def submit(**values):
        calls.append(values)
        if behavior == "raise":
            raise RuntimeError("lost response")
        return SimpleNamespace(ok=False, detail="not confirmed")
    panel = SimpleNamespace(is_alive=lambda: True, submit_deposit=submit)
    fake, _, _ = worker_dashboard(db, manager, panel=panel)
    run_worker(fake)
    assert len(calls) == 1
    assert db.get_auto_transaction("ambiguous")["status"] == "UNKNOWN"
    assert db.daily_bonus_exposure_for_transaction_date("alice", "2025-08-01") == 5_000
    assert db._conn.execute(
        "SELECT 1 FROM processed_transactions WHERE tx_id='ambiguous'"
    ).fetchone() is None
    run_worker(fake)
    assert len(calls) == 1


def test_worker_reservation_failure_never_calls_panel_and_halts(tmp_path, monkeypatch):
    db = DatabaseService(str(tmp_path / "db"))
    manager = QueueManager(Sheet([row("tx", "alice", 50_000)]), MemoryCache(), Validator(RULES), db)
    manager.refill()
    monkeypatch.setattr(db, "reserve_auto_transaction", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    fake, submits, finalised = worker_dashboard(db, manager)
    run_worker(fake)
    assert submits == [] and finalised == ["Worker halted: AUTO reservation database failure"]


def test_worker_submitting_transition_failure_never_calls_panel_and_halts(tmp_path, monkeypatch):
    db = DatabaseService(str(tmp_path / "db"))
    manager = QueueManager(Sheet([row("tx", "alice", 50_000)]), MemoryCache(), Validator(RULES), db)
    manager.refill()
    monkeypatch.setattr(db, "mark_auto_submitting", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    fake, submits, finalised = worker_dashboard(db, manager)
    run_worker(fake)
    assert submits == [] and finalised == ["Worker halted: AUTO journal database failure"]
    assert db.get_auto_transaction("tx")["status"] == "FAILED_NOT_SUBMITTED"


def test_worker_success_finalization_failure_halts_with_submitting_anchor(tmp_path, monkeypatch):
    db = DatabaseService(str(tmp_path / "db"))
    manager = QueueManager(
        Sheet([row("first", "alice", 50_000), row("second", "bob", 50_000)]),
        MemoryCache(), Validator(RULES), db,
    )
    manager.refill()
    original = db.finalize_auto_success
    monkeypatch.setattr(db, "finalize_auto_success", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    fake, submits, finalised = worker_dashboard(db, manager)
    run_worker(fake)
    assert len(submits) == 1 and db.get_auto_transaction("first")["status"] == "SUBMITTING"
    assert finalised == ["Worker halted: AUTO accounting state lost"]
    monkeypatch.setattr(db, "finalize_auto_success", original)
    db.close()
    reopened = DatabaseService(str(tmp_path / "db"))
    assert reopened.get_auto_transaction("first")["status"] == "UNKNOWN"
