"""
BUG-015 regression tests — Daily bonus MUST use the ORIGINAL TRANSACTION
DATE from Google Sheets, never `processed_at`.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# Make repo root importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.database import DatabaseService
from core.timestamp_utils import parse_transaction_date


# ---------------------------------------------------------------- parser tests
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2025-07-19 14:44", date(2025, 7, 19)),
        ("2025-07-19 14:44:00", date(2025, 7, 19)),
        ("2025-07-19", date(2025, 7, 19)),
        ("2025/07/19 23:56", date(2025, 7, 19)),
        ("19 Jul 2025 14:44", date(2025, 7, 19)),
        ("19 Jul 14:44", date(date.today().year, 7, 19)),
        ("Jul 19 2025 14:44", date(2025, 7, 19)),
        ("Jul 19 14:44", date(date.today().year, 7, 19)),
        ("19/07/2025 14:44", date(2025, 7, 19)),
        ("19-07-2025", date(2025, 7, 19)),
        # Ambiguous 07/19/2025 — 19 must be treated as day.
        ("07/19/2025", date(2025, 7, 19)),
        # US-style 07/09/2025 — day-first heuristic; both < 12 → day-first (7 Sep)
        ("07/09/2025", date(2025, 9, 7)),
    ],
)
def test_parse_transaction_date_common_formats(raw, expected):
    assert parse_transaction_date(raw) == expected


def test_parse_transaction_date_rejects_garbage():
    assert parse_transaction_date("") is None
    assert parse_transaction_date(None) is None
    assert parse_transaction_date("not-a-date") is None
    assert parse_transaction_date("99/99/2025") is None


# ---------------------------------------------------------------- DB tests
def test_daily_bonus_for_transaction_date_ignores_processed_at(tmp_path):
    """
    Regression for the exact scenario spelled out in the problem statement:

        maknyus27
        Tx1: 19 Jul 14:44   Deposit 100158   Bonus 10000  (SUCCESS)
        Tx2: 19 Jul 23:56   Deposit 110513
        Bot adjusts Tx2 at 20 Jul 03:27 → expected LIMIT / bonus 0.

    Even though `processed_at` for Tx2 falls on the NEXT calendar day
    (Jul 20), the daily-bonus rule must accumulate against the ORIGINAL
    transaction date (Jul 19).
    """
    db = DatabaseService(str(tmp_path / "processed.db"))

    # Tx1 — processed and successful with a Jul 19 sheet timestamp.
    db.insert(
        tx_id="TX-001",
        username="maknyus27",
        amount=100158,
        bonus=10000,
        result="SUCCESS",
        sheet_name="MASTER",
        timestamp="2025-07-19 14:44",
    )

    # Sanity: the record was persisted with the correct transaction date.
    assert db.daily_bonus_for_transaction_date("maknyus27", "2025-07-19") == 10000
    # And unrelated dates report 0.
    assert db.daily_bonus_for_transaction_date("maknyus27", "2025-07-20") == 0
    assert db.daily_bonus_for_transaction_date("someone_else", "2025-07-19") == 0

    db.close()


def test_daily_bonus_ignores_non_success_rows(tmp_path):
    db = DatabaseService(str(tmp_path / "processed.db"))
    db.insert("A", "u1", 100000, 10000, "FAILED", "MASTER", "2025-07-19 10:00")
    db.insert("B", "u1", 100000, 10000, "LIMIT", "MASTER", "2025-07-19 11:00")
    db.insert("C", "u1", 100000, 10000, "INVALID", "MASTER", "2025-07-19 12:00")
    db.insert("D", "u1", 100000, 10000, "MANUAL BONUS", "MASTER", "2025-07-19 13:00")
    assert db.daily_bonus_for_transaction_date("u1", "2025-07-19") == 0
    db.insert("E", "u1", 100000, 5000, "SUCCESS", "MASTER", "2025-07-19 14:00")
    assert db.daily_bonus_for_transaction_date("u1", "2025-07-19") == 5000
    db.close()


def test_backfill_migration_populates_timestamp_date(tmp_path):
    """Legacy databases (no timestamp_date column) get migrated + backfilled
    on next open without data loss."""
    db_path = tmp_path / "legacy.db"
    # Manually seed a legacy schema.
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE processed_transactions (
            tx_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            amount INTEGER, bonus INTEGER,
            result TEXT NOT NULL, processed_at TEXT NOT NULL,
            sheet_name TEXT, timestamp TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO processed_transactions VALUES (?,?,?,?,?,?,?,?)",
        ("X1", "u1", 100000, 10000, "SUCCESS", "2025-07-20T03:27",
         "MASTER", "2025-07-19 14:44"),
    )
    conn.commit()
    conn.close()

    db = DatabaseService(str(db_path))
    # After migration the legacy row must be queryable by transaction date.
    assert db.daily_bonus_for_transaction_date("u1", "2025-07-19") == 10000
    assert db.daily_bonus_for_transaction_date("u1", "2025-07-20") == 0
    db.close()
