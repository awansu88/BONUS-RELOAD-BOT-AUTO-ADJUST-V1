from __future__ import annotations

import sqlite3

import pytest

from core.manual_adjust_loader import ManualAdjustLoader, classify_rows, snapshot_fingerprint
from core.manual_adjust_models import (AttemptResult, RawManualAdjustRow,
    SQLITE_INTEGER_MAX, SourceClassification, normalize_username, parse_true_amount)
from core.manual_adjust_queue import ManualAdjustQueue
from core.manual_adjust_repository import ManualAdjustRepository


@pytest.mark.parametrize("raw,expected", [(1,1),(49999,49999),(50000,50000),(100000,100000),
    (1040243,1040243),("1,040,243",1040243),("1 040 243",1040243),(" 1,040,243 ",1040243)])
def test_strict_parser_valid(raw, expected):
    assert parse_true_amount(raw) == expected


@pytest.mark.parametrize("raw", [None,"","   ","abc","10.5","1,000.00","-100","0","+100",
    "NaN","Infinity","1e3",float("nan"),float("inf"),SQLITE_INTEGER_MAX + 1])
def test_strict_parser_invalid(raw):
    with pytest.raises(ValueError): parse_true_amount(raw)


def test_username_normalization_is_exact_strip_lower_not_casefold():
    assert {normalize_username(v)[1] for v in ("imat","IMAT"," Imat ","imat ")} == {"imat"}
    # German sharp-s demonstrates lower(), not casefold().
    assert normalize_username("ẞ")[1] == "ß"
    assert normalize_username("ẞ")[1] != "ẞ".casefold()


def rr(row, user, amount, tx=""):
    return RawManualAdjustRow(row, user, str(amount), tx)


def test_first_occurrence_and_invalid_first_always_own_key():
    rows = classify_rows([rr(2,"imat","bad"),rr(20,"IMAT",500000),rr(40," imat ",250000)])
    assert [r.classification for r in rows] == [SourceClassification.INVALID,
        SourceClassification.DUPLICATE,SourceClassification.DUPLICATE]
    assert rows[1].winner_source_row == rows[2].winner_source_row == 2


def test_over_one_hundred_duplicates_first_wins():
    rows = classify_rows([rr(2,"User",1)] + [rr(i," user ",i) for i in range(3,130)])
    assert sum(r.classification is SourceClassification.READY for r in rows) == 1
    assert all(r.winner_source_row == 2 for r in rows[1:])


@pytest.fixture
def repo(tmp_path):
    value = ManualAdjustRepository(tmp_path / "db.sqlite")
    value.initialize_schema()
    yield value
    value.close()


def snapshot(repo, rows=None):
    raw = rows or [rr(2,"u2",20),rr(3,"u1",10),rr(4,"U2",99),rr(5,"bad","x")]
    return repo.create_snapshot("sid","MASTER",snapshot_fingerprint("sid","MASTER",raw),classify_rows(raw))


def test_snapshot_persists_all_rows_summary_and_finite_ordered_queue(repo):
    cid = snapshot(repo)
    summary = repo.get_cycle_summary(cid)
    assert (summary.source_rows,summary.unique_users,summary.ready,summary.duplicates,
            summary.invalid,summary.total_adjustment_amount) == (4,3,2,1,1,30)
    assert len(repo.get_source_rows(cid)) == 4
    q = ManualAdjustQueue(repo,cid)
    assert [q.next_pending().username, q.next_pending().username] == ["u2","u1"]
    assert q.next_pending() is None and q.is_empty()


def test_same_fingerprint_and_username_allowed_across_cycles(repo):
    a, b = snapshot(repo,[rr(2,"u",1)]), snapshot(repo,[rr(2,"u",1)])
    assert a != b
    assert repo.get_cycle(a)["snapshot_fingerprint"] == repo.get_cycle(b)["snapshot_fingerprint"]


def test_database_unique_barrier_foreign_keys_and_atomic_rollback(repo):
    cid = snapshot(repo,[rr(2,"u",1)])
    source = repo.get_source_rows(cid)[0]
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute("INSERT INTO manual_adjust_transactions(cycle_id,source_row_id,username,username_key,adjust_amount,status) VALUES(?,?,?,?,?,'PENDING')",
            (cid,source["source_row_id"],"u","u",2))
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute("INSERT INTO manual_adjust_attempts(attempt_id,transaction_id,attempt_no,executor_id,claimed_at,result,submission_phase,created_at) VALUES('x',999,1,'e','n','IN_PROGRESS','X','n')")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_snapshot("sid","MASTER","fp",classify_rows([rr(2,"a",1),rr(2,"b",2)]))
    assert repo._conn.execute("SELECT COUNT(*) FROM manual_adjust_cycles WHERE snapshot_fingerprint='fp'").fetchone()[0] == 0


def test_attempt_history_completion_immutability_and_reconciliation(repo):
    cid = snapshot(repo,[rr(2,"u",1)])
    tx = repo.get_pending_transactions(cid)[0]
    attempt = repo.claim_pending(tx.transaction_id,"executor")
    with pytest.raises(ValueError): repo.claim_pending(tx.transaction_id,"executor")
    repo.finish_attempt(attempt["attempt_id"],AttemptResult.UNKNOWN,click_crossed=True,
        submission_phase="CLICKED",evidence_detail="timeout")
    with pytest.raises(ValueError):
        repo.finish_attempt(attempt["attempt_id"],AttemptResult.SUCCESS,click_crossed=True,submission_phase="DONE")
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute("UPDATE manual_adjust_attempts SET evidence_detail='changed' WHERE attempt_id=?",(attempt["attempt_id"],))
    with pytest.raises(ValueError):
        repo.reconcile_unknown(tx.transaction_id,attempt["attempt_id"],"SUCCESS",reconciled_by="",note="n",evidence="e")
    repo.reconcile_unknown(tx.transaction_id,attempt["attempt_id"],"SUCCESS",reconciled_by="op",note="checked",evidence="ledger")
    assert repo.get_transaction(tx.transaction_id).status.value == "SUCCESS"
    with pytest.raises(ValueError):
        repo.reconcile_unknown(tx.transaction_id,attempt["attempt_id"],"NOT_SUBMITTED",reconciled_by="op",note="n",evidence="e")


def test_non_unknown_and_wrong_attempt_cannot_be_reconciled(repo):
    cid = snapshot(repo,[rr(2,"u",1)])
    tx = repo.get_pending_transactions(cid)[0]
    attempt = repo.claim_pending(tx.transaction_id,"e")
    repo.finish_attempt(attempt["attempt_id"],AttemptResult.FAILED_NOT_SUBMITTED,
        click_crossed=False,submission_phase="FILL")
    with pytest.raises(ValueError):
        repo.reconcile_unknown(tx.transaction_id,attempt["attempt_id"],"SUCCESS",reconciled_by="o",note="n",evidence="e")


class FakeSheet:
    spreadsheet_id = "sheet-id"
    master_name = "MASTER"
    def __init__(self): self.calls = 0; self.rows = [rr(2,"u","1,000","")]
    def read_manual_adjust_snapshot(self): self.calls += 1; return list(self.rows)


def test_loader_reads_once_and_snapshot_is_immutable(repo):
    sheet = FakeSheet()
    cid = ManualAdjustLoader(sheet,repo).load()
    sheet.rows[0] = rr(2,"u",999999)
    assert sheet.calls == 1
    assert repo.get_cycle_summary(cid).total_adjustment_amount == 1000
    assert ManualAdjustQueue(repo,cid).next_pending().adjust_amount == 1000


@pytest.mark.parametrize("size", [100,500,1000])
def test_scale_exact_counts_and_totals(repo,size):
    rows = [rr(i+2,f"u{i}",i+1) for i in range(size)]
    cid = snapshot(repo,rows)
    s = repo.get_cycle_summary(cid)
    assert (s.source_rows,s.ready,s.duplicates,s.invalid) == (size,size,0,0)
    assert s.total_adjustment_amount == size*(size+1)//2


def test_repeat_migration_preserves_existing_auto_table(tmp_path):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE processed_transactions(tx_id TEXT PRIMARY KEY, username TEXT NOT NULL)")
    con.execute("INSERT INTO processed_transactions VALUES('T','u')")
    before = con.execute("PRAGMA table_info(processed_transactions)").fetchall(); con.commit(); con.close()
    repo = ManualAdjustRepository(path); repo.initialize_schema(); repo.initialize_schema()
    after = repo._conn.execute("PRAGMA table_info(processed_transactions)").fetchall()
    assert [tuple(r) for r in after] == before
    assert repo._conn.execute("SELECT * FROM processed_transactions").fetchall()[0][0] == "T"
    repo.close()
