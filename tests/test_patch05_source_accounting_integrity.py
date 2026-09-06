"""PATCH-05 source/accounting integrity acceptance coverage."""

import sqlite3
from datetime import date
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
from tests.test_patch00_auto_baseline import dashboard_method, run_worker, worker_dashboard


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
           "required_headers": {"user_id": "USER ID", "sheet_data": "KEY_ID",
                                "time_stamp": "TIME STAMP", "true_amount": "TRUE AMOUNT",
                                "tx_id": "TX_ID"},
           "sheet_names": {"master": "MASTER", "manual_bonus_reload": "MANUAL"}}
    service = SheetService("unused", cfg)
    headers = ["", " user   id ", "", "key_id", "TIME STAMP", "true amount", "", "", "tx_id"]
    service._master = SimpleNamespace(row_values=lambda _: headers)
    assert service._validate_headers() == []
    headers[5] = "BALANCE"
    assert "expected 'TRUE AMOUNT'" in service._validate_headers()[0]
    service.cols["tx_id"] = 6
    assert any("duplicates" in error for error in service._validate_headers())


@pytest.mark.parametrize("column_d", ["SHEET DATA", "WRONG", ""])
def test_production_key_id_contract_rejects_stale_wrong_or_blank_column_d(column_d):
    service = SheetService("unused", sheet_config())
    headers = ["", "USER ID", "", column_d, "TIME STAMP",
               "TRUE AMOUNT", "", "", "TX_ID"]
    service._master = FakeWorksheet("MASTER", headers)
    errors = service._validate_headers()
    assert errors and "D expected 'KEY_ID'" in errors[0]


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


def test_failed_refill_does_not_partially_mutate_cache_or_queue(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    cache = MemoryCache()
    feed = Feed([row("old", "old", timestamp=DAY)])
    q = QueueManager(feed, cache, Validator(RULES), db)
    old_stats = q.refill()
    old_preview, old_ready = q.preview_items(), q.next_ready()
    before = cache.get_daily_bonus("Alice")
    today = date.today().isoformat()
    feed.rows = [row("good", "Alice", timestamp=today),
                 row("bad", "Bob", timestamp="garbage", index=3)]
    with pytest.raises(SourceIntegrityError):
        q.refill()
    assert cache.get_daily_bonus("Alice") == before
    assert q.preview_items() == old_preview and q.next_ready() is old_ready
    assert q.stats() is old_stats and db.total_count() == 0
    assert db.get_auto_transaction("good") is None


def test_accounting_integrity_refill_is_atomic_for_cache_and_queue(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db.insert("ambiguous", "Alice", 50_000, 5_000, "SUCCESS", "MASTER", "bad")
    cache = MemoryCache()
    feed = Feed([])
    q = QueueManager(feed, cache, Validator(RULES), db)
    old_stats = q.refill()
    today = date.today().isoformat()
    feed.rows = [row("good", "Bob", timestamp=today),
                 row("blocked", "alice", timestamp=today, index=3)]
    with pytest.raises(AccountingIntegrityError):
        q.refill()
    assert cache.get_daily_bonus("Bob") == 0
    assert q.preview_items() == [] and q.next_ready() is None and q.stats() is old_stats


def test_bulk_insert_failure_does_not_publish_cache_or_queue(monkeypatch, tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    cache = MemoryCache()
    feed = Feed([])
    q = QueueManager(feed, cache, Validator(RULES), db)
    old_stats = q.refill()
    today = date.today().isoformat()
    feed.rows = [row("good", "Alice", timestamp=today),
                 row("skip", "Bob", amount=1, timestamp=today, index=3)]
    monkeypatch.setattr(
        db, "bulk_insert",
        lambda _, **__: (_ for _ in ()).throw(OSError("disk")),
    )
    with pytest.raises(OSError, match="disk"):
        q.refill()
    assert cache.get_daily_bonus("Alice") == 0
    assert q.preview_items() == [] and q.next_ready() is None and q.stats() is old_stats


def sheet_config():
    return {"columns": {"user_id": 2, "sheet_data": 4, "time_stamp": 5,
                        "true_amount": 6, "tx_id": 9},
            "required_headers": {"user_id": "USER ID", "sheet_data": "KEY_ID",
                                 "time_stamp": "TIME STAMP", "true_amount": "TRUE AMOUNT",
                                 "tx_id": "TX_ID"},
            "sheet_names": {"master": "MASTER", "manual_bonus_reload": "MANUAL"}}


class FakeWorksheet:
    def __init__(self, title, headers=()): self.title, self.headers = title, list(headers)
    def row_values(self, _): return list(self.headers)
    def get_all_values(self): return [self.headers]


class FakeSpreadsheet:
    title = "SOURCE"
    def __init__(self, tabs, headers):
        self._tabs = {name: FakeWorksheet(name, headers if name == "MASTER" else [])
                      for name in tabs}
    def worksheets(self): return list(self._tabs.values())
    def worksheet(self, name): return self._tabs[name]


def connect_fake(monkeypatch, tabs=("MASTER", "MANUAL"), headers=None):
    headers = headers or ["", "USER ID", "", "KEY_ID", "TIME STAMP",
                          "TRUE AMOUNT", "", "", "TX_ID"]
    service = SheetService("unused", sheet_config())
    book = FakeSpreadsheet(tabs, headers)
    monkeypatch.setattr(service, "_authorize",
                        lambda: SimpleNamespace(open_by_key=lambda _: book))
    return service, service.connect("x" * 20)


@pytest.mark.parametrize("position,value", [(1, "WRONG"), (4, "WRONG"),
                                              (5, "BALANCE"), (8, "WRONG"),
                                              (5, "")])
def test_wrong_or_blank_required_header_matrix(position, value):
    service = SheetService("unused", sheet_config())
    headers = ["", "USER ID", "", "KEY_ID", "TIME STAMP",
               "TRUE AMOUNT", "", "", "TX_ID"]
    headers[position] = value
    service._master = FakeWorksheet("MASTER", headers)
    assert service._validate_headers()


@pytest.mark.parametrize("mutation", ["missing", "nonnumeric", "zero", "negative", "duplicate"])
def test_invalid_required_column_matrix(mutation):
    service = SheetService("unused", sheet_config())
    service._master = FakeWorksheet("MASTER", ["", "USER ID", "", "KEY_ID",
                                                     "TIME STAMP", "TRUE AMOUNT", "", "", "TX_ID"])
    if mutation == "missing": service.cols.pop("tx_id")
    elif mutation == "nonnumeric": service.cols["tx_id"] = "nine"
    elif mutation == "zero": service.cols["tx_id"] = 0
    elif mutation == "negative": service.cols["tx_id"] = -1
    else: service.cols["tx_id"] = service.cols["true_amount"]
    assert service._validate_headers()


def test_failed_header_contract_leaves_sheet_service_disconnected(monkeypatch):
    headers = ["", "WRONG", "", "KEY_ID", "TIME STAMP", "TRUE AMOUNT", "", "", "TX_ID"]
    service, info = connect_fake(monkeypatch, headers=headers)
    assert not info.ok and not info.retryable and not service.is_connected
    assert service.master_name == "" and service.spreadsheet_id == ""
    with pytest.raises(RuntimeError, match="Not connected"): service.read_master_rows()
    with pytest.raises(RuntimeError, match="Not connected"): service.read_manual_set()


@pytest.mark.parametrize("tabs", [("MANUAL",), ("MASTER",)])
def test_missing_required_tab_leaves_service_disconnected(monkeypatch, tabs):
    service, info = connect_fake(monkeypatch, tabs=tabs)
    assert not info.ok and not info.retryable and not service.is_connected
    assert service.master_name == "" and service.spreadsheet_id == ""


def failed_once(db, tx="retry", user="Alice"):
    db.reserve_auto_transaction(tx, user, DAY, 100_000, 10_000,
                                source_timestamp=f"{DAY} 10:00")
    db.mark_auto_failed_not_submitted(tx, "safe")


def test_retry_source_timestamp_mismatch_cannot_create_attempt_two(tmp_path):
    db = DatabaseService(str(tmp_path / "db")); failed_once(db)
    with pytest.raises(SourceIntegrityError):
        db.reserve_auto_retry_transaction("retry", "alice", DAY, 100_000, 10_000,
                                          source_timestamp="2025-08-02 10:00")
    assert len(db.get_auto_attempts("retry")) == 1


def test_unknown_date_success_blocks_atomic_retry_claim(tmp_path):
    db = DatabaseService(str(tmp_path / "db")); failed_once(db)
    db.insert("old", "ALICE", 50_000, 5_000, "SUCCESS", "MASTER", "bad")
    with pytest.raises(AccountingIntegrityError):
        db.reserve_auto_retry_transaction("retry", "alice", DAY, 100_000, 10_000,
                                          source_timestamp=f"{DAY} 10:00")
    assert len(db.get_auto_attempts("retry")) == 1


@pytest.mark.parametrize("second", [
    row("dup", "Alice2", index=9), row("dup", "Alice", 100_000, index=9),
    row("dup", "Alice", timestamp=f"{DAY} 11:00", index=9),
])
def test_duplicate_candidate_conflict_matrix(second, tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    with pytest.raises(SourceIntegrityError, match="rows 2 and 9"):
        queue(db, [row("dup"), second]).refill()


def test_safe_retry_conflicting_duplicate_cannot_create_attempt_two(tmp_path):
    db = DatabaseService(str(tmp_path / "db")); failed_once(db)
    q = queue(db, [row("retry", "Alice"), row("retry", "Alice2", index=3)])
    with pytest.raises(SourceIntegrityError): q.refill()
    assert db.get_auto_transaction("retry")["attempt_count"] == 1
    assert len(db.get_auto_attempts("retry")) == 1


def test_known_terminal_conflicting_duplicates_remain_ineligible(tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    db.insert("done", "Alice", 50_000, 0, "LIMIT", "MASTER", DAY)
    q = queue(db, [row("done", "Alice"), row("done", "Bob", index=3)])
    assert q.refill().ready == 0 and q.preview_items() == []


def test_unparseable_legacy_success_migration_never_guesses_date(tmp_path):
    path = tmp_path / "legacy"
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE processed_transactions(
      tx_id TEXT PRIMARY KEY,username TEXT NOT NULL,amount INTEGER,bonus INTEGER,
      result TEXT NOT NULL,processed_at TEXT NOT NULL,sheet_name TEXT,timestamp TEXT);""")
    conn.execute("INSERT INTO processed_transactions VALUES(?,?,?,?,?,?,?,?)",
                 ("old", "Alice", 50000, 5000, "SUCCESS", DAY, "MASTER", "bad"))
    conn.commit(); conn.close()
    db = DatabaseService(str(path))
    assert db._conn.execute("SELECT timestamp_date FROM processed_transactions").fetchone() == (None,)
    with pytest.raises(AccountingIntegrityError):
        db.daily_bonus_exposure_for_transaction_date("alice", DAY)


@pytest.mark.parametrize("alive", [True, False])
def test_worker_accounting_failure_preempts_panel_and_recovery(alive, tmp_path):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [row("candidate")]); q.refill()
    db.insert("old", "ALICE", 50000, 5000, "SUCCESS", "MASTER", "bad")
    panel = SimpleNamespace(is_alive=lambda: alive,
                            submit_deposit_classified=lambda **_: pytest.fail("remote submit"))
    dashboard, submissions, finalised = worker_dashboard(db, q, panel=panel)
    recoveries = []
    dashboard._handle_active_panel_loss = lambda reason: recoveries.append(reason)
    run_worker(dashboard)
    assert dashboard.stop_requested and len(finalised) == 1
    assert recoveries == [] and submissions == []
    assert db.get_auto_transaction("candidate") is None


def test_accounting_preflight_database_failure_hard_stops_before_panel(
    monkeypatch, tmp_path
):
    db = DatabaseService(str(tmp_path / "db"))
    q = queue(db, [row("candidate")])
    q.refill()
    monkeypatch.setattr(
        db,
        "assert_auto_accounting_integrity",
        lambda _: (_ for _ in ()).throw(sqlite3.DatabaseError("database unavailable")),
    )
    liveness, recoveries, submissions = [], [], []
    panel = SimpleNamespace(
        is_alive=lambda: liveness.append(True) or False,
        submit_deposit_classified=lambda **values: submissions.append(values),
    )
    dashboard, _, finalised = worker_dashboard(db, q, panel=panel)
    dashboard._handle_active_panel_loss = lambda reason: recoveries.append(reason)

    run_worker(dashboard)

    assert dashboard.stop_requested and len(finalised) == 1
    assert liveness == [] and recoveries == [] and submissions == []
    assert db.get_auto_transaction("candidate") is None
    assert db.get_auto_attempts("candidate") == []
    assert not q.next_ready().processed


def test_monitoring_accounting_integrity_error_hard_stops():
    finalised, recoveries = [], []
    host = SimpleNamespace(
        _next_refresh_ts=0, _monitoring_interval=10,
        queue=SimpleNamespace(refill=lambda: (_ for _ in ()).throw(
            AccountingIntegrityError("ambiguous"))),
        logger=SimpleNamespace(error=lambda *_: None), stop_requested=False,
        _stamp_sync=lambda: None, _log_queue_summary=lambda _: None,
        _exit_monitoring=lambda: None, _update_countdown_label=lambda: None,
        _finalise_stop=lambda note: finalised.append(note),
        _handle_active_panel_loss=lambda reason: recoveries.append(reason),
    )
    tick = dashboard_method("_tick_monitoring", {
        "time": SimpleNamespace(monotonic=lambda: 1),
        "AccountingIntegrityError": AccountingIntegrityError,
    })
    tick(host)
    assert host.stop_requested and len(finalised) == 1 and recoveries == []
    assert host._next_refresh_ts == 0
