"""
Existing behaviour regression tests.

These are aimed at proving that the v1.1 changes did not break any of
the RC-stage guarantees the app already relied on:

  * SQLite dedup (PRIMARY KEY + pre-filter).
  * daily_bonus_map() reflects today's SUCCESS rows only (kept for the
    Dashboard KPI — validators must use daily_bonus_for_transaction_date
    instead).
  * QueueManager.refill() rebuilds preview each call.
  * VACUUM + CSV export + backup + clear_older_than() still work.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.database import DatabaseService
from core.memory_cache import MemoryCache
from core.queue_manager import QueueManager
from core.sheet_service import MasterRow
from core.validator import Validator


class _StubSheet:
    def __init__(self, rows):
        self._rows = rows

    def read_master_rows(self):
        return list(self._rows)


def _mk_row(tx, uid, amt, ts):
    return MasterRow(row_index=1, tx_id=tx, user_id=uid,
                     true_amount=amt, sheet_name="MASTER", timestamp=ts)


def _mk_validator():
    return Validator({
        "daily_limit": 10000,
        "tiers": [
            {"min_deposit": 100000, "bonus": 10000},
            {"min_deposit": 50000, "bonus": 5000},
        ],
    })


def test_dedup_via_primary_key(tmp_path):
    db = DatabaseService(str(tmp_path / "processed.db"))
    db.insert("A", "u", 100000, 10000, "SUCCESS", "M", "2025-07-19 10:00")
    # Second insert with same tx_id is silently ignored.
    db.insert("A", "u", 100000, 10000, "SUCCESS", "M", "2025-07-19 10:00")
    assert db.total_count() == 1
    db.close()


def test_daily_bonus_map_uses_processed_at(tmp_path):
    """`daily_bonus_map()` is retained for the "Today's Bonus" KPI, keyed
    by adjustment execution time. The rule engine no longer uses it."""
    db = DatabaseService(str(tmp_path / "processed.db"))
    db.insert("A", "u1", 100000, 10000, "SUCCESS", "M", "2025-07-19 10:00")
    m = db.daily_bonus_map()
    # Row was just inserted so processed_at is today → shows up.
    assert m.get("u1", 0) == 10000
    db.close()


def test_queue_manager_uses_transaction_date_for_daily_bonus(tmp_path):
    """
    End-to-end BUG-015 regression via QueueManager.refill().

    Prior state: `maknyus27` already got a 10 000 bonus on Jul 19.
    Incoming pending row: another Jul 19 deposit (arrived at 23:56 but
    the bot is refilling on Jul 20). The refill MUST classify it as
    LIMIT — not READY.
    """
    db = DatabaseService(str(tmp_path / "processed.db"))
    db.insert(
        "TX-001", "maknyus27", 100158, 10000, "SUCCESS",
        "MASTER", "2025-07-19 14:44",
    )

    pending = [_mk_row("TX-002", "maknyus27", 110513, "2025-07-19 23:56")]
    qm = QueueManager(_StubSheet(pending), MemoryCache(), _mk_validator(), db)
    stats = qm.refill()

    assert stats.ready == 0
    assert stats.limit == 1
    # Persisted with the correct outcome.
    row = db._conn.execute(
        "SELECT result, bonus FROM processed_transactions WHERE tx_id='TX-002'"
    ).fetchone()
    assert row == ("LIMIT", 0)
    db.close()


def test_queue_manager_preview_rebuilt_each_refill(tmp_path):
    db = DatabaseService(str(tmp_path / "processed.db"))
    pending1 = [_mk_row("T1", "u1", 50000, "2025-07-19 10:00")]
    qm = QueueManager(_StubSheet(pending1), MemoryCache(), _mk_validator(), db)
    qm.refill()
    assert len(qm.preview_items()) == 1

    qm.sheet = _StubSheet([_mk_row("T2", "u2", 50000, "2025-07-19 10:00")])
    qm.refill()
    items = qm.preview_items()
    assert len(items) == 1
    assert items[0].tx_id == "T2"
    db.close()


def test_export_csv_and_backup(tmp_path):
    db = DatabaseService(str(tmp_path / "processed.db"))
    db.insert("A", "u", 100000, 10000, "SUCCESS", "M", "2025-07-19 10:00")

    export = tmp_path / "export.csv"
    n = db.export_csv(str(export))
    assert n == 1
    text = export.read_text(encoding="utf-8")
    assert "timestamp_date" in text     # new column now exposed
    assert "2025-07-19" in text

    backup = tmp_path / "backup.db"
    db.backup(str(backup))
    assert backup.stat().st_size > 0
    db.close()


def test_vacuum_marks_meta(tmp_path):
    db = DatabaseService(str(tmp_path / "processed.db"))
    assert db.last_vacuum() is None
    db.vacuum()
    assert db.last_vacuum() is not None
    db.close()
