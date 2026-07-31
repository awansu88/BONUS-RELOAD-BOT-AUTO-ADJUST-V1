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
