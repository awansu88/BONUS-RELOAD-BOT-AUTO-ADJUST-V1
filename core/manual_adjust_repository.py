"""Dedicated SQLite persistence for Full Manual Adjust.

This module intentionally contains no AUTO table names or business services.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .manual_adjust_models import (
    AttemptResult, ClassifiedSourceRow, CycleStatus, CycleSummary,
    ManualAdjustTransaction, SourceClassification, TransactionStatus,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_adjust_cycles (
 cycle_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('LOADING','PREVIEW','RUNNING','STOPPED','FAILURE_REVIEW','REVIEW_REQUIRED','COMPLETED','CANCELLED')),
 spreadsheet_id TEXT NOT NULL, sheet_name TEXT NOT NULL, snapshot_fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL, loaded_at TEXT, confirmed_at TEXT, started_at TEXT, stopped_at TEXT, completed_at TEXT,
 executor_id TEXT, lease_heartbeat_at TEXT,
 total_source_rows INTEGER NOT NULL DEFAULT 0 CHECK(total_source_rows>=0), total_unique_users INTEGER NOT NULL DEFAULT 0 CHECK(total_unique_users>=0),
 ready_count INTEGER NOT NULL DEFAULT 0 CHECK(ready_count>=0), duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK(duplicate_count>=0),
 invalid_count INTEGER NOT NULL DEFAULT 0 CHECK(invalid_count>=0), total_amount INTEGER NOT NULL DEFAULT 0 CHECK(total_amount>=0),
 success_count INTEGER NOT NULL DEFAULT 0 CHECK(success_count>=0), failed_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_count>=0),
 unknown_count INTEGER NOT NULL DEFAULT 0 CHECK(unknown_count>=0),
 CHECK(completed_at IS NULL OR status IN ('COMPLETED','CANCELLED'))
);
CREATE TABLE IF NOT EXISTS manual_adjust_source_rows (
 source_row_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL REFERENCES manual_adjust_cycles(cycle_id),
 source_row INTEGER NOT NULL CHECK(source_row>=2), source_tx_id TEXT, username_raw TEXT, amount_raw TEXT,
 username TEXT, username_key TEXT, parsed_amount INTEGER,
 classification TEXT NOT NULL CHECK(classification IN ('READY','DUPLICATE','INVALID')), reason TEXT,
 winner_source_row_id INTEGER REFERENCES manual_adjust_source_rows(source_row_id), UNIQUE(cycle_id,source_row)
);
CREATE TABLE IF NOT EXISTS manual_adjust_transactions (
 transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL REFERENCES manual_adjust_cycles(cycle_id),
 source_row_id INTEGER NOT NULL UNIQUE REFERENCES manual_adjust_source_rows(source_row_id), username TEXT NOT NULL,
 username_key TEXT NOT NULL, adjust_amount INTEGER NOT NULL CHECK(adjust_amount>0),
 status TEXT NOT NULL CHECK(status IN ('PENDING','SUBMITTING','SUCCESS','FAILED_NOT_SUBMITTED','UNKNOWN','CANCELLED')),
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
 current_attempt_id TEXT REFERENCES manual_adjust_attempts(attempt_id), processed_at TEXT, UNIQUE(cycle_id,username_key)
);
CREATE TABLE IF NOT EXISTS manual_adjust_attempts (
 attempt_id TEXT PRIMARY KEY, transaction_id INTEGER NOT NULL REFERENCES manual_adjust_transactions(transaction_id),
 attempt_no INTEGER NOT NULL CHECK(attempt_no>0), executor_id TEXT NOT NULL, claimed_at TEXT NOT NULL,
 submit_started_at TEXT, submit_clicked_at TEXT, finished_at TEXT,
 result TEXT NOT NULL CHECK(result IN ('IN_PROGRESS','SUCCESS','FAILED_NOT_SUBMITTED','UNKNOWN')),
 click_crossed INTEGER CHECK(click_crossed IN (0,1)), submission_phase TEXT NOT NULL, error_detail TEXT, evidence_detail TEXT,
 reconciled_outcome TEXT CHECK(reconciled_outcome IN ('SUCCESS','NOT_SUBMITTED')), reconciled_at TEXT, reconciled_by TEXT,
 reconciliation_note TEXT, reconciliation_evidence TEXT, created_at TEXT NOT NULL, UNIQUE(transaction_id,attempt_no),
 CHECK((reconciled_outcome IS NULL AND reconciled_at IS NULL AND reconciled_by IS NULL AND reconciliation_note IS NULL AND reconciliation_evidence IS NULL)
    OR (result='UNKNOWN' AND reconciled_outcome IS NOT NULL AND reconciled_at IS NOT NULL AND reconciled_by IS NOT NULL
        AND reconciliation_note IS NOT NULL AND reconciliation_evidence IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_manual_cycle_status ON manual_adjust_transactions(cycle_id,status,source_row_id);
CREATE INDEX IF NOT EXISTS idx_manual_source_class ON manual_adjust_source_rows(cycle_id,classification,source_row);
CREATE INDEX IF NOT EXISTS idx_manual_attempt_transaction ON manual_adjust_attempts(transaction_id,attempt_no);
CREATE TRIGGER IF NOT EXISTS manual_attempt_no_delete BEFORE DELETE ON manual_adjust_attempts BEGIN SELECT RAISE(ABORT,'attempt history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS manual_attempt_identity_immutable BEFORE UPDATE ON manual_adjust_attempts
WHEN NEW.attempt_id<>OLD.attempt_id OR NEW.transaction_id<>OLD.transaction_id OR NEW.attempt_no<>OLD.attempt_no
 OR NEW.executor_id<>OLD.executor_id OR NEW.claimed_at<>OLD.claimed_at OR NEW.created_at<>OLD.created_at
BEGIN SELECT RAISE(ABORT,'attempt identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS manual_attempt_finished_immutable BEFORE UPDATE ON manual_adjust_attempts
WHEN OLD.result<>'IN_PROGRESS' AND (NEW.result<>OLD.result OR NEW.submit_started_at IS NOT OLD.submit_started_at OR NEW.submit_clicked_at IS NOT OLD.submit_clicked_at
 OR NEW.finished_at IS NOT OLD.finished_at OR NEW.click_crossed IS NOT OLD.click_crossed OR NEW.submission_phase<>OLD.submission_phase
 OR NEW.error_detail IS NOT OLD.error_detail OR NEW.evidence_detail IS NOT OLD.evidence_detail)
BEGIN SELECT RAISE(ABORT,'finished attempt evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS manual_attempt_reconciliation_write_once BEFORE UPDATE ON manual_adjust_attempts
WHEN OLD.reconciled_outcome IS NOT NULL AND (NEW.reconciled_outcome IS NOT OLD.reconciled_outcome OR NEW.reconciled_at IS NOT OLD.reconciled_at
 OR NEW.reconciled_by IS NOT OLD.reconciled_by OR NEW.reconciliation_note IS NOT OLD.reconciliation_note OR NEW.reconciliation_evidence IS NOT OLD.reconciliation_evidence)
BEGIN SELECT RAISE(ABORT,'reconciliation is write-once'); END;
"""


class ManualAdjustRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")

    def initialize_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY,value TEXT)")
        self._conn.execute("INSERT OR REPLACE INTO _meta(key,value) VALUES('manual_adjust_schema_version','1')")

    def close(self) -> None:
        self._conn.close()

    def create_snapshot(self, spreadsheet_id: str, sheet_name: str, fingerprint: str,
                        rows: Iterable[ClassifiedSourceRow], cycle_id: Optional[str] = None) -> str:
        materialized = list(rows)
        cycle_id = cycle_id or str(uuid.uuid4())
        unique_users = len({r.username_key for r in materialized if r.username_key})
        ready = [r for r in materialized if r.classification is SourceClassification.READY]
        duplicates = sum(r.classification is SourceClassification.DUPLICATE for r in materialized)
        invalid = sum(r.classification is SourceClassification.INVALID for r in materialized)
        now = _now()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("""INSERT INTO manual_adjust_cycles
              (cycle_id,status,spreadsheet_id,sheet_name,snapshot_fingerprint,created_at,loaded_at,total_source_rows,total_unique_users,ready_count,duplicate_count,invalid_count,total_amount)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (cycle_id, CycleStatus.PREVIEW.value, spreadsheet_id, sheet_name, fingerprint, now, now,
               len(materialized), unique_users, len(ready), duplicates, invalid, sum(r.parsed_amount or 0 for r in ready)))
            ids: dict[int, int] = {}
            for r in materialized:
                winner_id = ids.get(r.winner_source_row) if r.winner_source_row is not None else None
                cur = self._conn.execute("""INSERT INTO manual_adjust_source_rows
                  (cycle_id,source_row,source_tx_id,username_raw,amount_raw,username,username_key,parsed_amount,classification,reason,winner_source_row_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (cycle_id,r.source_row,r.source_tx_id,r.username_raw,r.amount_raw,r.username or None,r.username_key or None,
                   r.parsed_amount,r.classification.value,r.reason,winner_id))
                ids[r.source_row] = int(cur.lastrowid)
                if r.classification is SourceClassification.READY:
                    self._conn.execute("""INSERT INTO manual_adjust_transactions
                      (cycle_id,source_row_id,username,username_key,adjust_amount,status) VALUES(?,?,?,?,?,'PENDING')""",
                      (cycle_id,cur.lastrowid,r.username,r.username_key,r.parsed_amount))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return cycle_id

    def get_cycle(self, cycle_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM manual_adjust_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        return dict(row) if row else None

    def get_cycle_summary(self, cycle_id: str) -> CycleSummary:
        row = self._conn.execute("SELECT total_source_rows,total_unique_users,ready_count,duplicate_count,invalid_count,total_amount FROM manual_adjust_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if not row:
            raise KeyError(cycle_id)
        return CycleSummary(*map(int, row))

    def get_source_rows(self, cycle_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM manual_adjust_source_rows WHERE cycle_id=? ORDER BY source_row", (cycle_id,))]

    @staticmethod
    def _transaction(row: sqlite3.Row) -> ManualAdjustTransaction:
        return ManualAdjustTransaction(transaction_id=row["transaction_id"], cycle_id=row["cycle_id"],
            source_row_id=row["source_row_id"], source_row=row["source_row"], username=row["username"],
            username_key=row["username_key"], adjust_amount=row["adjust_amount"], status=TransactionStatus(row["status"]),
            attempt_count=row["attempt_count"], current_attempt_id=row["current_attempt_id"])

    def get_pending_transactions(self, cycle_id: str) -> list[ManualAdjustTransaction]:
        rows = self._conn.execute("""SELECT t.*,s.source_row FROM manual_adjust_transactions t JOIN manual_adjust_source_rows s USING(source_row_id)
          WHERE t.cycle_id=? AND t.status='PENDING' ORDER BY s.source_row""", (cycle_id,)).fetchall()
        return [self._transaction(r) for r in rows]

    def get_transaction(self, transaction_id: int) -> Optional[ManualAdjustTransaction]:
        row = self._conn.execute("""SELECT t.*,s.source_row FROM manual_adjust_transactions t JOIN manual_adjust_source_rows s USING(source_row_id)
          WHERE transaction_id=?""", (transaction_id,)).fetchone()
        return self._transaction(row) if row else None

    def get_attempt_history(self, transaction_id: int) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM manual_adjust_attempts WHERE transaction_id=? ORDER BY attempt_no", (transaction_id,))]

    def claim_pending(self, transaction_id: int, executor_id: str) -> dict:
        """Atomically claim one internally consistent PENDING transaction.

        Lease acquisition/heartbeat scheduling belongs to the future
        controller, but once ownership is represented on a cycle this boundary
        requires an exact RUNNING-cycle owner match.  It never manufactures or
        repairs ownership.
        """
        if not executor_id:
            raise ValueError("executor_id is required")
        now, attempt_id = _now(), str(uuid.uuid4())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("""SELECT t.status,t.attempt_count,t.current_attempt_id,
              c.status cycle_status,c.executor_id
              FROM manual_adjust_transactions t JOIN manual_adjust_cycles c USING(cycle_id)
              WHERE t.transaction_id=?""", (transaction_id,)).fetchone()
            if not row or row["status"] != TransactionStatus.PENDING.value:
                raise ValueError("transaction is not PENDING")
            if row["cycle_status"] != CycleStatus.RUNNING.value:
                raise ValueError("cycle is not RUNNING")
            if row["executor_id"] != executor_id:
                raise ValueError("executor does not own cycle")
            history = self._conn.execute("""SELECT attempt_id,attempt_no,result
              FROM manual_adjust_attempts WHERE transaction_id=? ORDER BY attempt_no""",
              (transaction_id,)).fetchall()
            expected = list(range(1, len(history) + 1))
            if [int(a["attempt_no"]) for a in history] != expected:
                raise ValueError("attempt numbers are not contiguous")
            if int(row["attempt_count"]) != len(history):
                raise ValueError("attempt_count does not match history")
            if any(a["result"] == AttemptResult.IN_PROGRESS.value for a in history):
                raise ValueError("conflicting IN_PROGRESS attempt exists")
            if history:
                if row["current_attempt_id"] != history[-1]["attempt_id"]:
                    raise ValueError("current_attempt_id does not identify latest attempt")
            elif row["current_attempt_id"] is not None:
                raise ValueError("current_attempt_id exists without attempt history")
            attempt_no = int(row["attempt_count"]) + 1
            self._conn.execute("""INSERT INTO manual_adjust_attempts
              (attempt_id,transaction_id,attempt_no,executor_id,claimed_at,result,submission_phase,created_at)
              VALUES(?,?,?,?,?,'IN_PROGRESS','CLAIMED',?)""", (attempt_id,transaction_id,attempt_no,executor_id,now,now))
            self._conn.execute("UPDATE manual_adjust_transactions SET status='SUBMITTING',attempt_count=?,current_attempt_id=? WHERE transaction_id=?", (attempt_no,attempt_id,transaction_id))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.get_attempt_history(transaction_id)[-1]

    def finish_attempt(self, attempt_id: str, result: AttemptResult, *, click_crossed: bool,
                       submission_phase: str, error_detail: Optional[str] = None,
                       evidence_detail: Optional[str] = None) -> None:
        if result is AttemptResult.IN_PROGRESS:
            raise ValueError("finish result must be terminal")
        now = _now()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT transaction_id,result FROM manual_adjust_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if not row or row["result"] != AttemptResult.IN_PROGRESS.value:
                raise ValueError("attempt is not IN_PROGRESS")
            self._conn.execute("UPDATE manual_adjust_attempts SET finished_at=?,result=?,click_crossed=?,submission_phase=?,error_detail=?,evidence_detail=? WHERE attempt_id=?",
                (now,result.value,int(click_crossed),submission_phase,error_detail,evidence_detail,attempt_id))
            changed = self._conn.execute("UPDATE manual_adjust_transactions SET status=?,processed_at=? WHERE transaction_id=? AND current_attempt_id=? AND status='SUBMITTING'",
                (result.value,now,row["transaction_id"],attempt_id))
            if changed.rowcount != 1:
                raise ValueError("matching SUBMITTING transaction transition failed")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def reconcile_unknown(self, transaction_id: int, attempt_id: str, outcome: str, *, reconciled_by: str,
                          note: str, evidence: str) -> None:
        if outcome not in {"SUCCESS", "NOT_SUBMITTED"} or not all((reconciled_by, note, evidence)):
            raise ValueError("complete valid reconciliation fields are required")
        target = "SUCCESS" if outcome == "SUCCESS" else "FAILED_NOT_SUBMITTED"
        now = _now()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("""SELECT a.result,a.reconciled_outcome,t.status,t.current_attempt_id
              FROM manual_adjust_attempts a JOIN manual_adjust_transactions t ON t.transaction_id=a.transaction_id
              WHERE a.attempt_id=? AND a.transaction_id=?""", (attempt_id,transaction_id)).fetchone()
            if not row or row["result"] != "UNKNOWN" or row["status"] != "UNKNOWN" or row["current_attempt_id"] != attempt_id or row["reconciled_outcome"] is not None:
                raise ValueError("exact current UNKNOWN attempt is required")
            self._conn.execute("""UPDATE manual_adjust_attempts SET reconciled_outcome=?,reconciled_at=?,reconciled_by=?,reconciliation_note=?,reconciliation_evidence=? WHERE attempt_id=?""",
                (outcome,now,reconciled_by,note,evidence,attempt_id))
            self._conn.execute("UPDATE manual_adjust_transactions SET status=?,processed_at=? WHERE transaction_id=?", (target,now,transaction_id))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def validate_cycle_integrity(self, cycle_id: str) -> list[str]:
        """Report persistence contradictions without attempting recovery."""
        errors: list[str] = []
        transactions = self._conn.execute(
            "SELECT * FROM manual_adjust_transactions WHERE cycle_id=?",
            (cycle_id,),
        ).fetchall()
        for tx in transactions:
            txid = int(tx["transaction_id"])
            prefix = f"transaction {txid}: "
            history = self._conn.execute(
                "SELECT * FROM manual_adjust_attempts WHERE transaction_id=? ORDER BY attempt_no",
                (txid,),
            ).fetchall()
            numbers = [int(a["attempt_no"]) for a in history]
            if numbers != list(range(1, len(history) + 1)):
                errors.append(prefix + "attempt numbers are not contiguous from 1")
            if int(tx["attempt_count"]) != len(history):
                errors.append(prefix + "attempt count mismatch")

            active = [a for a in history if a["result"] == AttemptResult.IN_PROGRESS.value]
            if len(active) > 1:
                errors.append(prefix + "multiple IN_PROGRESS attempts")

            current = None
            if tx["current_attempt_id"] is not None:
                current = next((a for a in history if a["attempt_id"] == tx["current_attempt_id"]), None)
                if current is None:
                    errors.append(prefix + "current_attempt_id does not belong to transaction")
                elif history and current["attempt_id"] != history[-1]["attempt_id"]:
                    errors.append(prefix + "current_attempt_id is not latest attempt")
            elif history:
                errors.append(prefix + "attempt history exists without current_attempt_id")

            status = tx["status"]
            if status == TransactionStatus.SUBMITTING.value:
                if current is None or current["result"] != AttemptResult.IN_PROGRESS.value:
                    errors.append(prefix + "SUBMITTING requires current IN_PROGRESS attempt")
                if len(active) != 1:
                    errors.append(prefix + "SUBMITTING requires exactly one IN_PROGRESS attempt")
            elif active:
                errors.append(prefix + f"{status} transaction has IN_PROGRESS attempt")

            if status == TransactionStatus.UNKNOWN.value:
                if current is None or current["result"] != AttemptResult.UNKNOWN.value:
                    errors.append(prefix + "UNKNOWN requires current UNKNOWN attempt")
                elif current["reconciled_outcome"] is not None:
                    errors.append(prefix + "UNKNOWN transaction has already reconciled current attempt")

            if status == TransactionStatus.PENDING.value and current is not None:
                retryable = (current["result"] == AttemptResult.FAILED_NOT_SUBMITTED.value or
                    (current["result"] == AttemptResult.UNKNOWN.value and
                     current["reconciled_outcome"] == "NOT_SUBMITTED"))
                if not retryable:
                    errors.append(prefix + "PENDING does not follow a retryable current attempt")
            if status == TransactionStatus.CANCELLED.value and history:
                errors.append(prefix + "CANCELLED transaction has attempt history")

            for attempt in history:
                fields = (attempt["reconciled_outcome"], attempt["reconciled_at"],
                          attempt["reconciled_by"], attempt["reconciliation_note"],
                          attempt["reconciliation_evidence"])
                populated = sum(value is not None for value in fields)
                if populated not in (0, 5):
                    errors.append(prefix + f"attempt {attempt['attempt_no']} has partial reconciliation")
                if populated and attempt["result"] != AttemptResult.UNKNOWN.value:
                    errors.append(prefix + f"attempt {attempt['attempt_no']} reconciliation is not on UNKNOWN")

            if current is not None and status in (TransactionStatus.SUCCESS.value,
                                                  TransactionStatus.FAILED_NOT_SUBMITTED.value):
                direct = current["result"] == status
                reconciled = (current["result"] == AttemptResult.UNKNOWN.value and
                    ((status == TransactionStatus.SUCCESS.value and current["reconciled_outcome"] == "SUCCESS") or
                     (status == TransactionStatus.FAILED_NOT_SUBMITTED.value and current["reconciled_outcome"] == "NOT_SUBMITTED")))
                if not (direct or reconciled):
                    errors.append(prefix + f"{status} does not match current attempt outcome")
            elif current is None and status in (TransactionStatus.SUCCESS.value,
                                                TransactionStatus.FAILED_NOT_SUBMITTED.value):
                errors.append(prefix + f"{status} requires a current attempt")
        return errors
