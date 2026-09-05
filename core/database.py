"""
SQLite persistence layer.

Google Sheets is now READ-ONLY (transaction feed).
SQLite is the WRITE-ONLY processing database.

Every decided outcome — SUCCESS, FAILED, INVALID, LIMIT, MANUAL BONUS —
gets one row keyed by tx_id, so the same transaction is never processed
twice. Daily-bonus totals for the validator are computed here too — by
the ORIGINAL TRANSACTION DATE from Google Sheets (`timestamp_date`
column), NEVER by the adjustment execution time (`processed_at`).
See `daily_bonus_for_transaction_date()` for the business-rule primitive
introduced by BUG-015.
"""

from __future__ import annotations

import csv
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .timestamp_utils import parse_transaction_date


SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS processed_transactions (
    tx_id          TEXT PRIMARY KEY,
    username       TEXT NOT NULL,
    amount         INTEGER,
    bonus          INTEGER,
    result         TEXT NOT NULL,
    processed_at   TEXT NOT NULL,
    sheet_name     TEXT,
    timestamp      TEXT,
    timestamp_date TEXT              -- ISO YYYY-MM-DD parsed from `timestamp`
);
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_processed_at    ON processed_transactions(processed_at);
CREATE INDEX IF NOT EXISTS idx_username_date   ON processed_transactions(username, processed_at);
CREATE INDEX IF NOT EXISTS idx_result          ON processed_transactions(result);
CREATE INDEX IF NOT EXISTS idx_username_txdate ON processed_transactions(username, timestamp_date);
"""

AUTO_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_adjust_transactions (
    tx_id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL,
    business_date TEXT NOT NULL, deposit_amount INTEGER NOT NULL,
    reserved_bonus INTEGER NOT NULL, status TEXT NOT NULL,
    current_attempt_id TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
    sheet_name TEXT, source_timestamp TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS auto_adjust_attempts (
    attempt_id TEXT PRIMARY KEY, tx_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
    claimed_at TEXT NOT NULL, submit_started_at TEXT, submit_clicked_at TEXT,
    finished_at TEXT, result TEXT NOT NULL, click_crossed INTEGER NOT NULL DEFAULT 0,
    submission_phase TEXT, error_detail TEXT, evidence_detail TEXT,
    reconciled_outcome TEXT, reconciled_at TEXT, reconciled_by TEXT,
    reconciliation_note TEXT, UNIQUE(tx_id, attempt_no),
    FOREIGN KEY(tx_id) REFERENCES auto_adjust_transactions(tx_id)
);
CREATE INDEX IF NOT EXISTS idx_auto_tx_username_date
    ON auto_adjust_transactions(username_key, business_date);
CREATE INDEX IF NOT EXISTS idx_auto_tx_status ON auto_adjust_transactions(status);
CREATE INDEX IF NOT EXISTS idx_auto_tx_unresolved
    ON auto_adjust_transactions(username_key, business_date, status, reserved_bonus);
CREATE INDEX IF NOT EXISTS idx_auto_attempt_tx ON auto_adjust_attempts(tx_id);
"""


class DatabaseService:
    """Small SQLite wrapper. Single connection, WAL mode."""

    def __init__(self, path: str = "processed.db") -> None:
        self.path = Path(path)
        # isolation_level=None -> autocommit; we still use executemany batches.
        self._conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        # Split schema application into three phases so pre-v1.1 databases
        # (which don't have the `timestamp_date` column yet) don't fail on
        # index creation:
        #   1. CREATE TABLE IF NOT EXISTS   (fresh installs)
        #   2. ALTER TABLE ... ADD COLUMN   (legacy migration)
        #   3. CREATE INDEX IF NOT EXISTS   (safe after step 2)
        self._conn.executescript(SCHEMA_TABLE)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        self._migrate_timestamp_date_column()
        self._conn.executescript(SCHEMA_INDEXES)
        self._conn.executescript(AUTO_JOURNAL_SCHEMA)
        self.recover_auto_journal()

    # ---------------------------------------------------------------- migrations
    def _migrate_timestamp_date_column(self) -> None:
        """Add + backfill `timestamp_date` for databases created before v1.1.

        Safe on both fresh (SCHEMA already includes the column) and legacy
        databases (ALTER TABLE runs; backfill parses each existing
        `timestamp` value).
        """
        cols = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(processed_transactions)"
            ).fetchall()
        }
        if "timestamp_date" not in cols:
            try:
                self._conn.execute(
                    "ALTER TABLE processed_transactions ADD COLUMN timestamp_date TEXT"
                )
            except sqlite3.DatabaseError:
                # Race / already-added by another process — safe to ignore.
                pass

        # Backfill any NULL timestamp_date values from the `timestamp` column.
        rows = self._conn.execute(
            "SELECT tx_id, timestamp FROM processed_transactions "
            "WHERE timestamp_date IS NULL"
        ).fetchall()
        if not rows:
            return
        updates: List[Tuple[str, str]] = []
        for tx_id, ts in rows:
            d = parse_transaction_date(ts)
            if d is not None:
                updates.append((d.isoformat(), tx_id))
        if updates:
            self._conn.executemany(
                "UPDATE processed_transactions SET timestamp_date = ? WHERE tx_id = ?",
                updates,
            )

    # ---------------------------------------------------------------- dedup
    def has_tx(self, tx_id: str) -> bool:
        if not tx_id:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM processed_transactions WHERE tx_id=? LIMIT 1", (tx_id,)
        ).fetchone()
        return row is not None

    def filter_new_tx_ids(self, tx_ids: Iterable[str]) -> Set[str]:
        """Return the subset of tx_ids that are NOT yet in the DB."""
        ids = [t for t in tx_ids if t]
        if not ids:
            return set()
        # SQLite has a variable limit (~999 by default); chunk to be safe.
        already: Set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT tx_id FROM processed_transactions WHERE tx_id IN ({placeholders})",
                chunk,
            ).fetchall()
            already.update(r[0] for r in rows)
        return set(ids) - already

    def has_known_auto_tx(self, tx_id: str) -> bool:
        """AUTO-only dedup across legacy outcomes and the safety journal."""
        if not tx_id:
            return False
        return self._conn.execute(
            "SELECT 1 FROM processed_transactions WHERE tx_id=? UNION ALL "
            "SELECT 1 FROM auto_adjust_transactions WHERE tx_id=? LIMIT 1",
            (str(tx_id), str(tx_id)),
        ).fetchone() is not None

    def filter_new_auto_tx_ids(self, tx_ids: Iterable[str]) -> Set[str]:
        return {str(tx) for tx in tx_ids if tx and not self.has_known_auto_tx(str(tx))}

    def get_auto_transaction(self, tx_id: str):
        row = self._conn.execute(
            "SELECT * FROM auto_adjust_transactions WHERE tx_id=?", (str(tx_id),)
        ).fetchone()
        if row is None:
            return None
        columns = [d[0] for d in self._conn.execute(
            "SELECT * FROM auto_adjust_transactions LIMIT 0"
        ).description]
        return dict(zip(columns, row))

    def get_auto_attempts(self, tx_id: str) -> List[Dict]:
        cur = self._conn.execute(
            "SELECT * FROM auto_adjust_attempts WHERE tx_id=? ORDER BY attempt_no",
            (str(tx_id),),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def daily_bonus_exposure_for_transaction_date(
        self, username: str, transaction_date_iso: str
    ) -> int:
        if not username or not transaction_date_iso:
            return 0
        key = str(username).strip()
        committed = self.daily_bonus_for_transaction_date(key, transaction_date_iso)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(reserved_bonus),0) FROM auto_adjust_transactions "
            "WHERE username_key=? AND business_date=? "
            "AND status IN ('PENDING','SUBMITTING','UNKNOWN')",
            (key, str(transaction_date_iso)),
        ).fetchone()
        return committed + int(row[0] or 0)

    def reserve_auto_transaction(
        self, tx_id: str, username: str, business_date: str, deposit_amount: int,
        requested_bonus: int, sheet_name: str = "", source_timestamp: str = "",
        daily_limit: int = 10_000,
    ) -> Optional[Dict]:
        """Atomically reserve the remaining quota and claim attempt number one."""
        now = datetime.now().isoformat(timespec="seconds")
        key = str(username).strip()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            known = self._conn.execute(
                "SELECT 1 FROM processed_transactions WHERE tx_id=? UNION ALL "
                "SELECT 1 FROM auto_adjust_transactions WHERE tx_id=? LIMIT 1",
                (str(tx_id), str(tx_id)),
            ).fetchone()
            if known:
                self._conn.execute("ROLLBACK")
                return None
            success = self._conn.execute(
                "SELECT COALESCE(SUM(bonus),0) FROM processed_transactions "
                "WHERE username=? AND timestamp_date=? AND result='SUCCESS'",
                (key, str(business_date)),
            ).fetchone()[0]
            reserved = self._conn.execute(
                "SELECT COALESCE(SUM(reserved_bonus),0) FROM auto_adjust_transactions "
                "WHERE username_key=? AND business_date=? "
                "AND status IN ('PENDING','SUBMITTING','UNKNOWN')",
                (key, str(business_date)),
            ).fetchone()[0]
            bonus = min(int(requested_bonus), max(0, int(daily_limit)-int(success or 0)-int(reserved or 0)))
            if bonus <= 0:
                self._conn.execute("ROLLBACK")
                return None
            attempt_id = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO auto_adjust_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(tx_id), str(username), key, str(business_date), int(deposit_amount),
                 bonus, "PENDING", attempt_id, 1, str(sheet_name or ""),
                 str(source_timestamp or ""), now, now, None),
            )
            self._conn.execute(
                "INSERT INTO auto_adjust_attempts "
                "(attempt_id,tx_id,attempt_no,claimed_at,result,click_crossed,submission_phase) "
                "VALUES (?,?,?,?, 'IN_PROGRESS',0,'RESERVED')",
                (attempt_id, str(tx_id), 1, now),
            )
            self._conn.execute("COMMIT")
            return {"tx_id": str(tx_id), "attempt_id": attempt_id, "reserved_bonus": bonus}
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def mark_auto_submitting(self, tx_id: str, attempt_id: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE auto_adjust_transactions SET status='SUBMITTING',updated_at=? "
                "WHERE tx_id=? AND current_attempt_id=? AND status='PENDING'",
                (now, str(tx_id), str(attempt_id)),
            )
            if cur.rowcount != 1:
                raise sqlite3.IntegrityError("AUTO transaction is not claimable")
            self._conn.execute(
                "UPDATE auto_adjust_attempts SET submit_started_at=?,submission_phase='REMOTE_CALL_STARTED' "
                "WHERE attempt_id=? AND result='IN_PROGRESS'", (now, str(attempt_id))
            )
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def record_auto_attempt_phase(
        self, tx_id: str, attempt_id: str, phase: str, evidence: str = ""
    ) -> None:
        """Persist one phase for the owned in-progress AUTO attempt.

        ``CLICK_RETURNED`` is the sole operation which records positive click
        crossing and its timestamp.  A zero value therefore means "not
        positively proven", not necessarily "the remote click did not happen".
        """
        now = datetime.now().isoformat(timespec="seconds")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            tx = self._conn.execute(
                "SELECT current_attempt_id FROM auto_adjust_transactions "
                "WHERE tx_id=? AND status='SUBMITTING'", (str(tx_id),)
            ).fetchone()
            if tx is None or tx[0] != str(attempt_id):
                raise sqlite3.IntegrityError("AUTO attempt ownership mismatch")
            if str(phase) == "CLICK_RETURNED":
                cur = self._conn.execute(
                    "UPDATE auto_adjust_attempts SET submission_phase=?,evidence_detail=?,"
                    "click_crossed=1,submit_clicked_at=? WHERE attempt_id=? AND tx_id=? "
                    "AND result='IN_PROGRESS'", (str(phase), str(evidence), now,
                                                  str(attempt_id), str(tx_id)))
            else:
                cur = self._conn.execute(
                    "UPDATE auto_adjust_attempts SET submission_phase=?,evidence_detail=? "
                    "WHERE attempt_id=? AND tx_id=? AND result='IN_PROGRESS'",
                    (str(phase), str(evidence), str(attempt_id), str(tx_id)))
            if cur.rowcount != 1:
                raise sqlite3.IntegrityError("AUTO attempt is not in progress")
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def finalize_auto_success(self, tx_id: str, classified_outcome: str) -> None:
        """Atomically create the legacy audit row and resolve journal + attempt."""
        if str(classified_outcome) != "SUCCESS":
            raise sqlite3.IntegrityError("classified AUTO outcome is not SUCCESS")
        now = datetime.now().isoformat(timespec="seconds")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            tx = self._conn.execute(
                "SELECT username,deposit_amount,reserved_bonus,sheet_name,source_timestamp,"
                "current_attempt_id,business_date FROM auto_adjust_transactions "
                "WHERE tx_id=? AND status='SUBMITTING'", (str(tx_id),)
            ).fetchone()
            if tx is None:
                raise sqlite3.IntegrityError("AUTO transaction is not SUBMITTING")
            proof = self._conn.execute(
                "SELECT 1 FROM auto_adjust_attempts WHERE attempt_id=? AND tx_id=? "
                "AND result='IN_PROGRESS' AND click_crossed=1 AND submit_clicked_at IS NOT NULL",
                (tx[5], str(tx_id)),
            ).fetchone()
            if proof is None:
                raise sqlite3.IntegrityError("AUTO SUCCESS lacks durable click evidence")
            self._conn.execute(
                "INSERT INTO processed_transactions (tx_id,username,amount,bonus,result,"
                "processed_at,sheet_name,timestamp,timestamp_date) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(tx_id), tx[0], tx[1], tx[2], "SUCCESS", now, tx[3], tx[4], tx[6]),
            )
            self._conn.execute(
                "UPDATE auto_adjust_attempts SET result='SUCCESS',finished_at=?,submission_phase='FINISHED' "
                "WHERE attempt_id=?", (now, tx[5])
            )
            self._conn.execute(
                "UPDATE auto_adjust_transactions SET status='SUCCESS',updated_at=?,resolved_at=? WHERE tx_id=?",
                (now, now, str(tx_id)),
            )
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def mark_auto_unknown(self, tx_id: str, detail: str = "", phase: str = "AMBIGUOUS_RESPONSE",
                          evidence: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            tx = self._conn.execute(
                "SELECT current_attempt_id FROM auto_adjust_transactions "
                "WHERE tx_id=? AND status='SUBMITTING'", (str(tx_id),)
            ).fetchone()
            if tx is None:
                raise sqlite3.IntegrityError("AUTO transaction is not SUBMITTING")
            cur = self._conn.execute(
                "UPDATE auto_adjust_attempts SET result='UNKNOWN',finished_at=?,error_detail=?,"
                "evidence_detail=?,submission_phase=? WHERE attempt_id=? AND result='IN_PROGRESS'",
                (now, str(detail), str(evidence), str(phase), tx[0])
            )
            if cur.rowcount != 1:
                raise sqlite3.IntegrityError("AUTO attempt is not in progress")
            self._conn.execute(
                "UPDATE auto_adjust_transactions SET status='UNKNOWN',updated_at=?,resolved_at=NULL "
                "WHERE tx_id=?",
                (now, str(tx_id)),
            )
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def mark_auto_failed_not_submitted(self, tx_id: str, detail: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            tx = self._conn.execute(
                "SELECT current_attempt_id FROM auto_adjust_transactions WHERE tx_id=? AND status='PENDING'",
                (str(tx_id),),
            ).fetchone()
            if tx is None:
                self._conn.execute("COMMIT")
                return
            self._conn.execute(
                "UPDATE auto_adjust_attempts SET result='FAILED_NOT_SUBMITTED',finished_at=?,"
                "error_detail=?,submission_phase='FINISHED' WHERE attempt_id=?",
                (now, str(detail), tx[0]),
            )
            self._conn.execute(
                "UPDATE auto_adjust_transactions SET status='FAILED_NOT_SUBMITTED',updated_at=?,"
                "resolved_at=? WHERE tx_id=?", (now, now, str(tx_id)),
            )
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def finalize_auto_failed_not_submitted(
        self, tx_id: str, attempt_id: str, classified_outcome: str,
        detail: str = "", phase: str = "FAILED_PRE_CLICK", evidence: str = "",
    ) -> None:
        """Resolve SUBMITTING only with explicit, guarded pre-click proof."""
        if str(classified_outcome) != "FAILED_NOT_SUBMITTED":
            raise sqlite3.IntegrityError("classification does not prove pre-click failure")
        now = datetime.now().isoformat(timespec="seconds")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            tx = self._conn.execute(
                "SELECT current_attempt_id FROM auto_adjust_transactions "
                "WHERE tx_id=? AND status='SUBMITTING'", (str(tx_id),)
            ).fetchone()
            if tx is None or tx[0] != str(attempt_id):
                raise sqlite3.IntegrityError("AUTO attempt ownership mismatch")
            cur = self._conn.execute(
                "UPDATE auto_adjust_attempts SET result='FAILED_NOT_SUBMITTED',finished_at=?,"
                "error_detail=?,evidence_detail=?,submission_phase=? WHERE attempt_id=? "
                "AND tx_id=? AND result='IN_PROGRESS' AND click_crossed<>1 "
                "AND submit_clicked_at IS NULL",
                (now, str(detail), str(evidence), str(phase), str(attempt_id), str(tx_id)))
            if cur.rowcount != 1:
                raise sqlite3.IntegrityError("AUTO attempt has possible click evidence")
            self._conn.execute(
                "UPDATE auto_adjust_transactions SET status='FAILED_NOT_SUBMITTED',updated_at=?,"
                "resolved_at=? WHERE tx_id=?", (now, now, str(tx_id)))
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def recover_auto_journal(self) -> Dict[str, int]:
        """Resolve process-local crash states in one idempotent transaction."""
        now = datetime.now().isoformat(timespec="seconds")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            pending = self._conn.execute(
                "SELECT tx_id,current_attempt_id FROM auto_adjust_transactions WHERE status='PENDING'"
            ).fetchall()
            submitting = self._conn.execute(
                "SELECT t.tx_id,t.current_attempt_id,a.submission_phase,a.click_crossed,"
                "a.submit_clicked_at FROM auto_adjust_transactions t JOIN auto_adjust_attempts a "
                "ON a.attempt_id=t.current_attempt_id WHERE t.status='SUBMITTING'"
            ).fetchall()
            for tx_id, attempt_id in pending:
                self._conn.execute(
                    "UPDATE auto_adjust_attempts SET result='FAILED_NOT_SUBMITTED',finished_at=?,"
                    "error_detail='startup recovery before remote call',submission_phase='FINISHED' "
                    "WHERE attempt_id=? AND result='IN_PROGRESS'", (now, attempt_id))
                self._conn.execute(
                    "UPDATE auto_adjust_transactions SET status='FAILED_NOT_SUBMITTED',updated_at=?,"
                    "resolved_at=? WHERE tx_id=?", (now, now, tx_id))
            pre_click = {"REMOTE_CALL_STARTED", "FORM_STARTED", "USERNAME_FILLED",
                         "AMOUNT_FILLED", "REMARK_FILLED", "READY_TO_CLICK"}
            recovered_pre = 0
            recovered_unknown = 0
            for tx_id, attempt_id, phase, crossed, clicked_at in submitting:
                safe = phase in pre_click and int(crossed or 0) == 0 and clicked_at is None
                if safe:
                    recovered_pre += 1
                    self._conn.execute(
                        "UPDATE auto_adjust_attempts SET result='FAILED_NOT_SUBMITTED',finished_at=?,"
                        "error_detail='startup recovery in proven pre-click phase' "
                        "WHERE attempt_id=? AND result='IN_PROGRESS'", (now, attempt_id))
                    self._conn.execute(
                        "UPDATE auto_adjust_transactions SET status='FAILED_NOT_SUBMITTED',updated_at=?,"
                        "resolved_at=? WHERE tx_id=?", (now, now, tx_id))
                else:
                    recovered_unknown += 1
                    self._conn.execute(
                        "UPDATE auto_adjust_attempts SET result='UNKNOWN',finished_at=?,"
                        "error_detail='startup recovery at ambiguous click boundary' "
                        "WHERE attempt_id=? AND result='IN_PROGRESS'", (now, attempt_id))
                    self._conn.execute(
                        "UPDATE auto_adjust_transactions SET status='UNKNOWN',updated_at=?,resolved_at=NULL "
                        "WHERE tx_id=?", (now, tx_id))
            self._conn.execute("COMMIT")
            return {"failed_not_submitted": len(pending) + recovered_pre,
                    "unknown": recovered_unknown}
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    # ---------------------------------------------------------------- write
    def insert(
        self,
        tx_id: str,
        username: str,
        amount: int,
        bonus: int,
        result: str,
        sheet_name: str,
        timestamp: str,
    ) -> None:
        ts_date = parse_transaction_date(timestamp)
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_transactions "
            "(tx_id, username, amount, bonus, result, processed_at, "
            " sheet_name, timestamp, timestamp_date) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(tx_id),
                str(username),
                int(amount or 0),
                int(bonus or 0),
                str(result),
                datetime.now().isoformat(timespec="seconds"),
                str(sheet_name or ""),
                str(timestamp or ""),
                ts_date.isoformat() if ts_date else None,
            ),
        )

    def bulk_insert(self, rows: List[Tuple]) -> int:
        """Rows: (tx_id, username, amount, bonus, result, sheet_name, timestamp)."""
        if not rows:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        payload = []
        for r in rows:
            ts_raw = str(r[6] or "")
            ts_date = parse_transaction_date(ts_raw)
            payload.append(
                (
                    str(r[0]),
                    str(r[1]),
                    int(r[2] or 0),
                    int(r[3] or 0),
                    str(r[4]),
                    now,
                    str(r[5] or ""),
                    ts_raw,
                    ts_date.isoformat() if ts_date else None,
                )
            )
        self._conn.executemany(
            "INSERT OR IGNORE INTO processed_transactions "
            "(tx_id, username, amount, bonus, result, processed_at, "
            " sheet_name, timestamp, timestamp_date) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            payload,
        )
        return len(payload)

    # ---------------------------------------------------------------- reads
    def daily_bonus_map(self) -> Dict[str, int]:
        """{username: SUM(bonus)} for today's SUCCESS rows only.

        NOTE: This helper is retained for the Dashboard KPI ("Today's
        Bonus Paid" display) and for warming a cache — it is based on
        `processed_at` (adjustment execution time) which is the correct
        semantics for the "processed today" KPI.

        Do NOT use this helper for BUSINESS RULE decisions. For the daily
        bonus rule, see `daily_bonus_for_transaction_date()` below which
        keys by the **original transaction date** from Google Sheets
        (BUG-015).
        """
        rows = self._conn.execute(
            """
            SELECT username, COALESCE(SUM(bonus), 0)
            FROM processed_transactions
            WHERE date(processed_at) = date('now', 'localtime')
              AND result = 'SUCCESS'
            GROUP BY username
            """
        ).fetchall()
        return {str(r[0]): int(r[1] or 0) for r in rows if r[0]}

    def daily_bonus_for_transaction_date(
        self, username: str, transaction_date_iso: str
    ) -> int:
        """SUM(bonus) for `username` whose ORIGINAL transaction date on the
        source Google Sheet equals `transaction_date_iso` (ISO `YYYY-MM-DD`).

        Business-rule primitive for BUG-015. `processed_at` is *never*
        consulted here — only the persisted `timestamp` column which is
        the verbatim TIME STAMP cell copied from the sheet.
        """
        if not username or not transaction_date_iso:
            return 0
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(bonus), 0)
            FROM processed_transactions
            WHERE username = ?
              AND result = 'SUCCESS'
              AND timestamp_date = ?
            """,
            (str(username).strip(), str(transaction_date_iso)),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def processed_today_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM processed_transactions "
            "WHERE date(processed_at) = date('now', 'localtime')"
        ).fetchone()
        return int(row[0] if row else 0)

    def total_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM processed_transactions"
        ).fetchone()
        return int(row[0] if row else 0)

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def last_vacuum(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM _meta WHERE key='last_vacuum'"
        ).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- maint
    def vacuum(self) -> str:
        self._conn.execute("PRAGMA wal_checkpoint(FULL)")
        self._conn.execute("VACUUM")
        ts = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES('last_vacuum', ?)",
            (ts,),
        )
        return ts

    def clear_older_than(self, days: int = 30) -> int:
        cur = self._conn.execute(
            "DELETE FROM processed_transactions "
            "WHERE date(processed_at) < date('now', 'localtime', ?)",
            (f"-{int(days)} days",),
        )
        return int(cur.rowcount or 0)

    def backup(self, dest: str) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(FULL)")
        except sqlite3.DatabaseError:
            pass
        shutil.copy2(self.path, dest)

    def export_csv(self, dest: str) -> int:
        rows = self._conn.execute(
            "SELECT tx_id, username, amount, bonus, result, processed_at, "
            "sheet_name, timestamp, timestamp_date FROM processed_transactions "
            "ORDER BY processed_at"
        ).fetchall()
        with open(dest, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "tx_id", "username", "amount", "bonus", "result",
                    "processed_at", "sheet_name", "timestamp", "timestamp_date",
                ]
            )
            w.writerows(rows)
        return len(rows)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- v1.2 helpers
    # Additive safe helpers used by the Maintenance Center + Health
    # Watchdog (Production Hardening v1.2). They wrap the SAME connection
    # already used by the engine — no new connection is opened.
    def checkpoint_wal(self, mode: str = "PASSIVE") -> None:
        """Run PRAGMA wal_checkpoint(<mode>). Never raises."""
        try:
            self._conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchall()
        except Exception:
            pass

    def integrity_check(self) -> str:
        """Return the raw result of PRAGMA integrity_check (typically 'ok')."""
        rows = self._conn.execute("PRAGMA integrity_check").fetchall()
        return "; ".join(str(r[0]) for r in rows)

    def optimize(self) -> None:
        """PRAGMA optimize + update statistics. Never raises."""
        try:
            self._conn.execute("PRAGMA optimize").fetchall()
        except Exception:
            pass

    def count_older_than(self, days: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM processed_transactions "
            "WHERE date(processed_at) < date('now', 'localtime', ?)",
            (f"-{int(days)} days",),
        ).fetchone()
        return int(row[0] if row else 0)

    def is_open(self) -> bool:
        try:
            self._conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False
