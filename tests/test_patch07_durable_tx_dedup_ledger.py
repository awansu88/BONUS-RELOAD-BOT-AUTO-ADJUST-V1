"""PATCH-07 regression coverage for the permanent SQLite TX identity ledger."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from core import database as database_module
from core.database import DatabaseService
from tests.test_patch00_auto_baseline import (
    queue as worker_queue,
    row as worker_row,
    run_worker,
    worker_dashboard,
)

DAY = "2026-09-05"
STAMP = "2026-09-05 12:00:00"


def ledger_rows(db: DatabaseService):
    return db._conn.execute(
        "SELECT tx_id FROM tx_dedup_ledger ORDER BY tx_id"
    ).fetchall()


def reserve(db: DatabaseService, tx_id: str = "tx"):
    return db.reserve_auto_transaction(
        tx_id, "Alice", DAY, 100_000, 10_000, "MASTER", STAMP
    )


def failed_once(db: DatabaseService, tx_id: str = "tx"):
    claim = reserve(db, tx_id)
    assert claim is not None
    db.mark_auto_failed_not_submitted(tx_id, "pre-click")
    return claim


def test_fresh_schema_primary_key_empty_id_and_same_database(tmp_path):
    path = tmp_path / "processed.db"
    db = DatabaseService(str(path))
    info = db._conn.execute("PRAGMA table_info(tx_dedup_ledger)").fetchall()
    assert [(row[1], row[5]) for row in info] == [("tx_id", 1), ("recorded_at", 0)]
    db.insert("", "Alice", 1, 0, "INVALID", "MASTER", STAMP)
    assert ledger_rows(db) == []
    assert path.exists()
    assert {item.name for item in tmp_path.iterdir()} == {
        "processed.db", "processed.db-wal", "processed.db-shm"
    }
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO tx_dedup_ledger VALUES ('one','now'),('one','later')"
        )


@pytest.mark.parametrize("result", ["SUCCESS", "FAILED", "INVALID", "LIMIT", "MANUAL BONUS"])
def test_processed_history_backfills_idempotently_without_changing_rows(tmp_path, result):
    path = tmp_path / "legacy.db"
    db = DatabaseService(str(path))
    db.insert("legacy", "Alice", 100_000, 123, result, "MASTER", STAMP)
    before = db._conn.execute(
        "SELECT username,amount,bonus,result,timestamp FROM processed_transactions"
    ).fetchall()
    db._conn.execute("DROP TABLE tx_dedup_ledger")
    db.close()

    reopened = DatabaseService(str(path))
    assert ledger_rows(reopened) == [("legacy",)]
    assert reopened._conn.execute(
        "SELECT username,amount,bonus,result,timestamp FROM processed_transactions"
    ).fetchall() == before
    reopened.close()
    again = DatabaseService(str(path))
    assert ledger_rows(again) == [("legacy",)]


@pytest.mark.parametrize(
    "state", ["PENDING", "SUBMITTING", "UNKNOWN", "FAILED_NOT_SUBMITTED", "SUCCESS"]
)
def test_all_auto_journal_statuses_backfill_with_expected_overlap(tmp_path, state):
    path = tmp_path / f"{state}.db"
    db = DatabaseService(str(path))
    claim = reserve(db)
    if state == "SUBMITTING":
        db.mark_auto_submitting("tx", claim["attempt_id"])
    elif state == "UNKNOWN":
        db.mark_auto_submitting("tx", claim["attempt_id"])
        db.mark_auto_unknown("tx")
    elif state == "FAILED_NOT_SUBMITTED":
        db.mark_auto_failed_not_submitted("tx")
    elif state == "SUCCESS":
        db.mark_auto_submitting("tx", claim["attempt_id"])
        db.record_auto_attempt_phase("tx", claim["attempt_id"], "CLICK_RETURNED")
        db.finalize_auto_success("tx", "SUCCESS")
    db._conn.execute("DROP TABLE tx_dedup_ledger")
    db.close()

    reopened = DatabaseService(str(path))
    assert ledger_rows(reopened) == [("tx",)]
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM tx_dedup_ledger WHERE tx_id='tx'"
    ).fetchone() == (1,)
    if state in {"UNKNOWN", "FAILED_NOT_SUBMITTED", "SUCCESS"}:
        assert reopened.get_auto_transaction("tx")["status"] == state


@pytest.mark.parametrize("result", ["INVALID", "LIMIT", "MANUAL BONUS", "SUCCESS", "FAILED"])
def test_retention_purges_audit_but_never_identity_or_dedup(tmp_path, result):
    db = DatabaseService(str(tmp_path / "db"))
    db.insert("old", "Alice", 100_000, 0, result, "MASTER", STAMP)
    db._conn.execute(
        "UPDATE processed_transactions SET processed_at='2000-01-01T00:00:00' WHERE tx_id='old'"
    )
    assert db.count_older_than(30) == 1
    assert db.clear_older_than(30) == 1
    assert db.count_older_than(30) == 0
    assert ledger_rows(db) == [("old",)]
    assert db.has_tx("old")
    assert db.filter_new_tx_ids(["old", "new"]) == {"new"}
    assert db.filter_new_auto_tx_ids(["old", "new"]) == {"new"}
    assert not db.is_auto_retry_eligible("old")


def test_vacuum_analyze_optimize_and_backup_preserve_ledger(tmp_path):
    path = tmp_path / "db"
    db = DatabaseService(str(path))
    db.insert("known", "Alice", 1, 0, "INVALID", "MASTER", STAMP)
    db.vacuum()
    db._conn.execute("ANALYZE")
    db.optimize()
    assert ledger_rows(db) == [("known",)]
    backup = tmp_path / "backup.db"
    db.backup(str(backup))
    copied = sqlite3.connect(backup)
    assert copied.execute("SELECT tx_id FROM tx_dedup_ledger").fetchall() == [("known",)]
    copied.close()


@pytest.mark.parametrize("result", ["INVALID", "LIMIT", "MANUAL BONUS"])
def test_terminal_insert_atomically_claims_identity_and_audit(tmp_path, result):
    db = DatabaseService(str(tmp_path / "db"))
    db.insert("terminal", "Alice", 100_000, 0, result, "MASTER", STAMP)
    assert ledger_rows(db) == [("terminal",)]
    assert db._conn.execute(
        "SELECT tx_id,result FROM processed_transactions"
    ).fetchall() == [("terminal", result)]


def test_bulk_insert_is_atomic_and_does_not_recreate_purged_history(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db._conn.executescript(
        "CREATE TRIGGER reject_boom BEFORE INSERT ON processed_transactions "
        "WHEN NEW.tx_id='boom' BEGIN SELECT RAISE(ABORT,'boom'); END;"
    )
    rows = [
        ("good", "Alice", 1, 0, "INVALID", "MASTER", STAMP),
        ("boom", "Alice", 1, 0, "LIMIT", "MASTER", STAMP),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        db.bulk_insert(rows)
    assert ledger_rows(db) == []
    assert db.total_count() == 0
    db._conn.execute("DROP TRIGGER reject_boom")
    assert db.bulk_insert([rows[0], rows[0]]) == 1
    db._conn.execute("DELETE FROM processed_transactions WHERE tx_id='good'")
    assert db.bulk_insert([rows[0]]) == 0
    assert db.total_count() == 0 and db.has_tx("good")


def test_first_auto_reservation_is_one_atomic_unique_claim(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    first = reserve(db)
    assert first is not None
    assert reserve(db) is None
    assert ledger_rows(db) == [("tx",)]
    assert db._conn.execute("SELECT COUNT(*) FROM auto_adjust_transactions").fetchone() == (1,)
    assert db._conn.execute("SELECT COUNT(*) FROM auto_adjust_attempts").fetchone() == (1,)


def test_ledger_or_journal_write_failure_rolls_back_everything(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db._conn.executescript(
        "CREATE TRIGGER reject_claim BEFORE INSERT ON tx_dedup_ledger "
        "WHEN NEW.tx_id='claim-fail' BEGIN SELECT RAISE(ABORT,'claim'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError):
        reserve(db, "claim-fail")
    assert db._conn.execute("SELECT COUNT(*) FROM auto_adjust_transactions").fetchone() == (0,)
    db._conn.execute("DROP TRIGGER reject_claim")
    db._conn.executescript(
        "CREATE TRIGGER reject_journal BEFORE INSERT ON auto_adjust_transactions "
        "WHEN NEW.tx_id='journal-fail' BEGIN SELECT RAISE(ABORT,'journal'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError):
        reserve(db, "journal-fail")
    assert db._conn.execute(
        "SELECT COUNT(*) FROM tx_dedup_ledger WHERE tx_id='journal-fail'"
    ).fetchone() == (0,)
    assert db._conn.execute("SELECT COUNT(*) FROM auto_adjust_attempts").fetchone() == (0,)


def test_two_connections_cannot_both_claim_attempt_one(tmp_path):
    path = tmp_path / "shared.db"
    left, right = DatabaseService(str(path)), DatabaseService(str(path))
    barrier = threading.Barrier(2)
    results = []

    def attempt(db):
        barrier.wait()
        results.append(reserve(db))

    threads = [threading.Thread(target=attempt, args=(db,)) for db in (left, right)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(result is not None for result in results) == 1
    assert left._conn.execute("SELECT COUNT(*) FROM tx_dedup_ledger").fetchone() == (1,)
    assert left._conn.execute("SELECT COUNT(*) FROM auto_adjust_transactions").fetchone() == (1,)
    assert left._conn.execute("SELECT COUNT(*) FROM auto_adjust_attempts").fetchone() == (1,)


def test_safe_retry_uses_journal_proof_not_a_second_ledger_claim(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    assert db.filter_new_auto_tx_ids(["tx"]) == set()
    assert db.is_auto_retry_eligible("tx")
    retry = db.reserve_auto_retry_transaction("tx", "Alice", DAY, 100_000, 10_000, source_timestamp=STAMP)
    assert retry is not None
    assert ledger_rows(db) == [("tx",)]
    db.mark_auto_failed_not_submitted("tx")
    assert not db.is_auto_retry_eligible("tx")
    assert db.reserve_auto_retry_transaction("tx", "Alice", DAY, 100_000, 10_000, source_timestamp=STAMP) is None
    assert len(db.get_auto_attempts("tx")) == 2


@pytest.mark.parametrize("result", ["LIMIT", "INVALID", "MANUAL BONUS"])
def test_retry_terminal_decision_cannot_resurrect_after_processed_retention(
    tmp_path, result
):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    attempt_one = db.get_auto_attempts("tx")[0]
    assert db.is_auto_retry_eligible("tx")

    db.insert("tx", "Alice", 100_000, 0, result, "MASTER", STAMP)

    transaction = db.get_auto_transaction("tx")
    assert transaction["status"] == result
    assert transaction["attempt_count"] == 1
    assert transaction["resolved_at"] is not None
    assert db.get_auto_attempts("tx") == [attempt_one]
    assert ledger_rows(db) == [("tx",)]
    assert not db.is_auto_retry_eligible("tx")

    db._conn.execute(
        "UPDATE processed_transactions SET processed_at='2000-01-01T00:00:00' "
        "WHERE tx_id='tx'"
    )
    assert db.clear_older_than(30) == 1
    assert db._conn.execute(
        "SELECT 1 FROM processed_transactions WHERE tx_id='tx'"
    ).fetchone() is None
    assert db.has_tx("tx") and not db.is_auto_retry_eligible("tx")
    assert db.reserve_auto_retry_transaction(
        "tx", "Alice", DAY, 100_000, 10_000, source_timestamp=STAMP
    ) is None
    assert db.get_auto_attempts("tx") == [attempt_one]


@pytest.mark.parametrize(
    "result", ["SUCCESS", "FAILED", "UNKNOWN", "FAILED_NOT_SUBMITTED", "READY", "OTHER"]
)
def test_ledger_known_retry_rejects_non_validator_terminal_results(tmp_path, result):
    db = DatabaseService(str(tmp_path / result.replace(" ", "_")))
    failed_once(db)
    db.insert("tx", "Alice", 100_000, 0, result, "MASTER", STAMP)
    assert db._conn.execute(
        "SELECT 1 FROM processed_transactions WHERE tx_id='tx'"
    ).fetchone() is None
    assert db.get_auto_transaction("tx")["status"] == "FAILED_NOT_SUBMITTED"
    assert db.is_auto_retry_eligible("tx")


@pytest.mark.parametrize(
    "username,amount,timestamp",
    [
        ("Mallory", 100_000, STAMP),
        ("Alice", 50_000, STAMP),
        ("Alice", 100_000, "2026-09-06 12:00:00"),
    ],
)
def test_retry_terminal_closure_requires_original_source_identity(
    tmp_path, username, amount, timestamp
):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    db.insert("tx", username, amount, 0, "LIMIT", "MASTER", timestamp)
    assert db._conn.execute(
        "SELECT 1 FROM processed_transactions WHERE tx_id='tx'"
    ).fetchone() is None
    assert db.get_auto_transaction("tx")["status"] == "FAILED_NOT_SUBMITTED"
    assert db.is_auto_retry_eligible("tx")


def test_bulk_insert_closes_retry_atomically_without_rewriting_attempt(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    attempt_one = db.get_auto_attempts("tx")[0]
    assert db.bulk_insert(
        [("tx", "Alice", 100_000, 0, "INVALID", "MASTER", STAMP)]
    ) == 1
    assert db.get_auto_transaction("tx")["status"] == "INVALID"
    assert db.get_auto_attempts("tx") == [attempt_one]
    assert not db.is_auto_retry_eligible("tx")


def test_retry_terminal_closure_failure_rolls_back_audit_and_journal(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    failed_once(db)
    db._conn.executescript(
        "CREATE TRIGGER reject_retry_close BEFORE UPDATE ON auto_adjust_transactions "
        "WHEN NEW.status='LIMIT' BEGIN SELECT RAISE(ABORT,'close'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("tx", "Alice", 100_000, 0, "LIMIT", "MASTER", STAMP)
    assert db._conn.execute(
        "SELECT 1 FROM processed_transactions WHERE tx_id='tx'"
    ).fetchone() is None
    assert db.get_auto_transaction("tx")["status"] == "FAILED_NOT_SUBMITTED"
    assert db.is_auto_retry_eligible("tx")


def test_worker_manual_terminal_write_failure_halts_before_following_tx(
    tmp_path, monkeypatch
):
    db, manager = worker_queue(
        tmp_path,
        [worker_row("manual", "alice", 100_000), worker_row("next", "bob", 50_000)],
    )
    manager.refill()
    fake = None

    def refresh():
        fake.cache.set_manual({"alice"})

    fake, submissions, finalised = worker_dashboard(db, manager, refresh=refresh)
    monkeypatch.setattr(db, "insert", lambda *args, **kwargs: (_ for _ in ()).throw(
        sqlite3.OperationalError("ledger write failed")
    ))
    run_worker(fake)
    assert submissions == [] and fake.stop_requested
    assert finalised == ["Worker halted: durable TX terminal persistence failure"]
    assert not db.has_tx("manual") and not db.has_tx("next")
    assert manager.next_ready().tx_id == "manual"


def test_worker_limit_terminal_write_failure_halts_before_following_tx(
    tmp_path, monkeypatch
):
    db, manager = worker_queue(
        tmp_path,
        [worker_row("limit", "alice", 100_000), worker_row("next", "bob", 50_000)],
    )
    manager.refill()
    db.insert(
        "quota", "alice", 100_000, 10_000, "SUCCESS", "MASTER",
        "2025-08-01 09:00:00",
    )
    fake, submissions, finalised = worker_dashboard(db, manager)
    monkeypatch.setattr(db, "insert", lambda *args, **kwargs: (_ for _ in ()).throw(
        sqlite3.OperationalError("ledger write failed")
    ))
    run_worker(fake)
    assert submissions == [] and fake.stop_requested
    assert finalised == ["Worker halted: durable TX terminal persistence failure"]
    assert not db.has_tx("limit") and not db.has_tx("next")
    assert manager.next_ready().tx_id == "limit"


def test_worker_ledger_select_failure_halts_before_reservation_or_next_tx(
    tmp_path, monkeypatch
):
    db, manager = worker_queue(
        tmp_path,
        [worker_row("first", "alice", 100_000), worker_row("next", "bob", 50_000)],
    )
    manager.refill()
    fake, submissions, finalised = worker_dashboard(db, manager)
    monkeypatch.setattr(db, "has_known_auto_tx", lambda *_: (_ for _ in ()).throw(
        sqlite3.OperationalError("ledger select failed")
    ))
    run_worker(fake)
    assert submissions == [] and fake.stop_requested
    assert finalised == ["Worker halted: AUTO dedup database failure"]
    assert db.get_auto_transaction("first") is None
    assert db.get_auto_transaction("next") is None
    assert manager.next_ready().tx_id == "first"


def test_unknown_and_success_remain_known_without_processed_history(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    unknown = reserve(db, "unknown")
    db.mark_auto_submitting("unknown", unknown["attempt_id"])
    db.mark_auto_unknown("unknown")
    unknown_bonus = db.get_auto_transaction("unknown")["reserved_bonus"]
    assert not db.is_auto_retry_eligible("unknown")
    success = db.reserve_auto_transaction(
        "success", "Bob", DAY, 100_000, 10_000, "MASTER", STAMP
    )
    db.mark_auto_submitting("success", success["attempt_id"])
    db.record_auto_attempt_phase("success", success["attempt_id"], "CLICK_RETURNED")
    db.finalize_auto_success("success", "SUCCESS")
    db._conn.execute("DELETE FROM processed_transactions WHERE tx_id='success'")
    assert db.has_tx("unknown") and db.has_tx("success")
    assert db.get_auto_transaction("unknown")["reserved_bonus"] == unknown_bonus
    assert reserve(db, "success") is None
    assert len(db.get_auto_attempts("success")) == 1


def test_ledger_select_failure_propagates_instead_of_treating_ids_as_new(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db._conn.execute("DROP TABLE tx_dedup_ledger")
    with pytest.raises(sqlite3.DatabaseError):
        db.has_tx("x")
    with pytest.raises(sqlite3.DatabaseError):
        db.filter_new_tx_ids(["x"])
    with pytest.raises(sqlite3.DatabaseError):
        db.filter_new_auto_tx_ids(["x"])


def test_migration_failure_aborts_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(database_module, "TX_DEDUP_LEDGER_SCHEMA", "CREATE TABLE broken (")
    with pytest.raises(sqlite3.DatabaseError):
        DatabaseService(str(tmp_path / "broken.db"))
