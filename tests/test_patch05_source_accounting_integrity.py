"""PATCH-05 source/accounting integrity acceptance coverage."""

import sqlite3
from types import SimpleNamespace

import pytest

from core.database import DatabaseService
from core.memory_cache import MemoryCache
from core.queue_manager import QueueItem, QueueManager
from core.sheet_service import MasterRow, SheetService
from core.source_integrity import (
    AccountingIntegrityError,
    SourceIntegrityError,
    canonical_username_key,
)
from core.validator import Validator
from tests.test_patch00_auto_baseline import run_worker, worker_dashboard


RULES = {"daily_limit": 10_000, "tiers": [
    {"min_deposit": 100_000, "bonus": 10_000},
    {"min_deposit": 50_000, "bonus": 5_000},
]}
DAY = "2025-08-01"


class Feed:
    def __init__(self, rows): self.rows = rows
    def read_master_rows(self): return list(self.rows)


def row(tx, user="Alice", amount=50_000, timestamp=f"{DAY} 10:00", index=2):
    return MasterRow(index, tx, user, amount, "MASTER", timestamp)


def queue(db, rows, manual=()):
    cache = MemoryCache()
    cache.set_manual(set(manual))
    return QueueManager(Feed(rows), cache, Validator(RULES), db)


def test_canonical_key_is_lower_strip_only_and_preserves_internal_whitespace():
    assert canonical_username_key(" Alice ") == "alice"
    assert canonical_username_key("ALICE") == canonical_username_key("alice")
    assert canonical_username_key("user  name") == "user  name"


def test_raw_username_is_preserved_in_queue_and_journal(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [row("tx", "PlayerABC")])
    q.refill()
    item = q.next_ready()
    assert item.username == "PlayerABC"
    db.reserve_auto_transaction("tx", item.username, DAY, item.amount, item.bonus,
                                source_timestamp=item.timestamp)
    tx = db.get_auto_transaction("tx")
    assert (tx["username"], tx["username_key"]) == ("PlayerABC", "playerabc")


def test_panel_receives_original_trimmed_source_username(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [row("tx", "PlayerABC")])
    q.refill()
    dashboard, submissions, _ = worker_dashboard(db, q)
    run_worker(dashboard)
    assert submissions[0]["user_id"] == "PlayerABC"


def test_fresh_worker_manual_check_is_canonical_and_makes_no_panel_call(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [row("tx", "Alice")])
    q.refill()
    dashboard, submissions, _ = worker_dashboard(
        db, q, refresh=lambda: dashboard.cache.set_manual({"ALICE"})
    )
    run_worker(dashboard)
    assert submissions == []
    assert db._conn.execute(
        "SELECT result FROM processed_transactions WHERE tx_id='tx'"
    ).fetchone() == ("MANUAL BONUS",)


@pytest.mark.parametrize("manual,source", [("Alice", "alice"), ("ALICE", "Alice")])
def test_manual_bonus_is_canonical(manual, source, tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [row("tx", source)], [manual])
    q.refill()
    assert q.preview_items()[0].status == "MANUAL BONUS"
    assert q.next_ready() is None


def test_committed_and_reserved_accounting_is_canonical(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db.insert("old", "ALICE", 50_000, 5_000, "SUCCESS", "MASTER", DAY)
    claim = db.reserve_auto_transaction("new", "alice", DAY, 100_000, 10_000,
                                        source_timestamp=f"{DAY} 11:00")
    assert claim["reserved_bonus"] == 5_000
    assert db.daily_bonus_exposure_for_transaction_date(" Alice ", DAY) == 10_000


@pytest.mark.parametrize("status,reserves", [
    ("PENDING", True), ("SUBMITTING", True), ("UNKNOWN", True),
    ("FAILED_NOT_SUBMITTED", False),
])
def test_unresolved_status_accounting_is_canonical(status, reserves, tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db.reserve_auto_transaction("tx", "Alice", DAY, 50_000, 5_000)
    db._conn.execute("UPDATE auto_adjust_transactions SET status=?", (status,))
    assert db.daily_bonus_exposure_for_transaction_date("ALICE", DAY) == (5_000 if reserves else 0)


def test_processed_and_journal_migrations_are_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE processed_transactions(tx_id TEXT PRIMARY KEY,username TEXT NOT NULL,
        amount INTEGER,bonus INTEGER,result TEXT NOT NULL,processed_at TEXT NOT NULL,
        sheet_name TEXT,timestamp TEXT);
    """)
    conn.execute("INSERT INTO processed_transactions VALUES(?,?,?,?,?,?,?,?)",
                 ("p", " ALICE ", 50000, 5000, "SUCCESS", DAY, "MASTER", DAY))
    conn.commit(); conn.close()
    db = DatabaseService(str(path))
    db.reserve_auto_transaction("j", "Bob", DAY, 50_000, 5_000)
    db._conn.execute("UPDATE auto_adjust_transactions SET username_key='Bob',status='UNKNOWN'")
    db.close()
    reopened = DatabaseService(str(path))
    assert reopened._conn.execute("SELECT username_key,bonus FROM processed_transactions").fetchone() == ("alice", 5000)
    assert reopened._conn.execute("SELECT username_key,status,reserved_bonus FROM auto_adjust_transactions").fetchone() == ("bob", "UNKNOWN", 5000)
    assert reopened.total_count() == 1 and len(reopened.get_auto_attempts("j")) == 1


def test_case_only_retry_matches_but_real_change_and_attempt_three_do_not(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db.reserve_auto_transaction("tx", "Alice", DAY, 100_000, 10_000,
                                source_timestamp=f"{DAY} 10:00")
    db.mark_auto_failed_not_submitted("tx", "safe")
    second = db.reserve_auto_retry_transaction("tx", "alice", DAY, 100_000, 10_000,
                                               source_timestamp=f"{DAY} 10:00")
    assert second and len(db.get_auto_attempts("tx")) == 2
    assert db.reserve_auto_retry_transaction("tx", "Alice2", DAY, 100_000, 10_000) is None


def test_header_contract_exact_normalized_and_fixed_position():
    cfg = {"columns": {"user_id": 2, "sheet_data": 4, "time_stamp": 5,
                       "true_amount": 6, "tx_id": 9},
           "required_headers": {"user_id": "USER ID", "sheet_data": "SHEET DATA",
                                "time_stamp": "TIME STAMP", "true_amount": "TRUE AMOUNT",
                                "tx_id": "TX_ID"},
           "sheet_names": {"master": "MASTER", "manual_bonus_reload": "MANUAL"}}
    service = SheetService("unused", cfg)
    headers = ["", " user   id ", "", "sheet data", "TIME STAMP", "true amount", "", "", "tx_id"]
    service._master = SimpleNamespace(row_values=lambda _: headers)
    assert service._validate_headers() == []
    headers[5] = "BALANCE"
    assert "expected 'TRUE AMOUNT'" in service._validate_headers()[0]
    service.cols["tx_id"] = 6
    assert any("duplicates" in error for error in service._validate_headers())


@pytest.mark.parametrize("timestamp", ["", "garbage"])
def test_invalid_candidate_timestamp_fails_atomically(timestamp, tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [row("skip", amount=1), row("bad", timestamp=timestamp, index=3)])
    with pytest.raises(SourceIntegrityError, match="TIME STAMP"):
        q.refill()
    assert db.total_count() == 0 and db.get_auto_transaction("bad") is None
    assert q.preview_items() == [] and q.next_ready() is None


def test_worker_invalid_timestamp_hard_stops_before_panel_or_reservation(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [])
    q._ready = [QueueItem("bad", "Alice", 50_000, 5_000, "MASTER",
                          timestamp="garbage")]
    dashboard, submissions, finalised = worker_dashboard(db, q)
    run_worker(dashboard)
    assert submissions == [] and db.get_auto_transaction("bad") is None
    assert dashboard.stop_requested and "source integrity" in finalised[-1]


@pytest.mark.parametrize("method", ["new", "retry"])
def test_atomic_business_date_rejects_garbage_without_attempt(method, tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    if method == "new":
        call = lambda: db.reserve_auto_transaction("tx", "alice", "garbage", 50_000, 5_000)
    else:
        call = lambda: db.reserve_auto_retry_transaction("tx", "alice", "garbage", 50_000, 5_000)
    with pytest.raises(SourceIntegrityError): call()
    assert db.get_auto_attempts("tx") == []


def test_atomic_source_date_mismatch_rolls_back(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    with pytest.raises(SourceIntegrityError):
        db.reserve_auto_transaction("tx", "alice", DAY, 50_000, 5_000,
                                    source_timestamp="2025-08-02 10:00")
    assert db.get_auto_transaction("tx") is None


def test_unknown_date_success_blocks_only_same_canonical_user(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db.insert("old", "Alice", 50_000, 5_000, "SUCCESS", "MASTER", "garbage")
    with pytest.raises(AccountingIntegrityError):
        db.daily_bonus_exposure_for_transaction_date("alice", DAY)
    with pytest.raises(AccountingIntegrityError):
        db.reserve_auto_transaction("new", "ALICE", DAY, 50_000, 5_000)
    assert db.reserve_auto_transaction("other", "bob", DAY, 50_000, 5_000)


def test_unknown_date_non_success_does_not_block(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db.insert("old", "Alice", 50_000, 0, "INVALID", "MASTER", "garbage")
    assert db.daily_bonus_exposure_for_transaction_date("alice", DAY) == 0


def test_identical_duplicates_collapse_and_conflicts_name_rows(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [row("same", index=2), row("same", index=3)])
    assert q.refill().ready == 1 and len(q.preview_items()) == 1
    bad = queue(db, [row("conflict", "Alice", index=7), row("conflict", "Bob", index=9)])
    with pytest.raises(SourceIntegrityError, match="rows 7 and 9"):
        bad.refill()
    assert db.get_auto_transaction("conflict") is None


def test_batch_case_variants_share_quota_and_dates_remain_separate(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [row("a", "Alice"), row("b", "alice", index=3),
                   row("c", "ALICE", index=4)])
    q.refill()
    assert [(x.bonus, x.status) for x in q.preview_items()] == [
        (5000, "READY"), (5000, "READY"), (0, "LIMIT")]
    q2 = queue(db, [row("d", "alice", timestamp="2025-08-02", index=5)])
    assert q2.refill().ready == 1
