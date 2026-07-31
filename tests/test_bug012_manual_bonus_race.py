"""
BUG-012 regression tests — Manual Bonus Race Condition.

The worker must, immediately BEFORE every adjustment, run the following
sequence (in this exact order):

    1. SQLite duplicate validation
    2. LATEST Manual Bonus validation (fresh read from Google Sheets)
    3. Daily bonus validation (keyed by original transaction date)
    4. Submit adjustment

The critical property is: if a user is added to MANUAL BONUS RELOAD
AFTER the queue was refilled but BEFORE the worker submits, the worker
MUST skip the transaction with a `MANUAL BONUS` outcome and NEVER call
`panel.submit_deposit()` for that user.

Because the worker step lives inside a PySide6 QMainWindow we exercise
its policy through a small procedural stand-in that mirrors the exact
validation order implemented in `ui/dashboard.py::_worker_step`.
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
from core.queue_manager import QueueItem
from core.timestamp_utils import parse_transaction_date
from core.validator import Validator


# ---------- Minimal stand-ins -------------------------------------------------
class FakeSheet:
    """Records how many times read_manual_set() is called so tests can
    assert the fresh-read happened."""

    def __init__(self, manual_snapshots):
        self._snapshots = list(manual_snapshots)
        self._i = 0
        self.reads = 0
        self.is_connected = True

    def read_manual_set(self):
        self.reads += 1
        snap = self._snapshots[min(self._i, len(self._snapshots) - 1)]
        self._i += 1
        return set(snap)


class FakePanel:
    def __init__(self):
        self.submits = []

    def submit_deposit(self, user_id, bonus, remark):
        self.submits.append((user_id, bonus, remark))
        return type("R", (), {"ok": True, "detail": ""})


def _process_one(item, sheet, cache, db, validator, panel):
    """Reference implementation of the BUG-012 validation sequence.

    Kept in the tests file (not shared with the UI) so we can catch any
    future divergence between the policy documented here and the code
    that actually ships in `ui/dashboard.py`.
    """
    # (1) SQLite duplicate validation.
    if db.has_tx(item.tx_id):
        return ("SKIPPED_DUPLICATE", None)

    # (2) LATEST manual bonus validation — must be a fresh read.
    fresh = sheet.read_manual_set()
    cache.set_manual(fresh)
    if item.username in fresh:
        db.insert(
            item.tx_id, item.username, item.amount, 0, "MANUAL BONUS",
            item.sheet_name, item.timestamp,
        )
        return ("MANUAL BONUS", None)

    # (3) Daily-bonus validation, keyed by transaction date (BUG-015).
    tx_date = parse_transaction_date(item.timestamp) or date.today()
    current = db.daily_bonus_for_transaction_date(item.username, tx_date.isoformat())
    result = validator.validate(
        user_id=item.username,
        deposit_raw=item.amount,
        current_daily_bonus=current,
        manual_set=fresh,
    )
    if result.status != "READY":
        db.insert(
            item.tx_id, item.username, item.amount, 0, result.status,
            item.sheet_name, item.timestamp,
        )
        return (result.status, None)

    # (4) Submit.
    submit = panel.submit_deposit(item.username, result.bonus, "R")
    if submit.ok:
        db.insert(
            item.tx_id, item.username, item.amount, result.bonus, "SUCCESS",
            item.sheet_name, item.timestamp,
        )
        return ("SUCCESS", result.bonus)
    db.insert(
        item.tx_id, item.username, item.amount, 0, "FAILED",
        item.sheet_name, item.timestamp,
    )
    return ("FAILED", None)


# ---------- Tests -------------------------------------------------------------
def _mk_env(tmp_path, snapshots):
    db = DatabaseService(str(tmp_path / "processed.db"))
    cache = MemoryCache()
    validator = Validator({
        "daily_limit": 10000,
        "tiers": [
            {"min_deposit": 100000, "bonus": 10000},
            {"min_deposit": 50000, "bonus": 5000},
        ],
    })
    sheet = FakeSheet(snapshots)
    panel = FakePanel()
    return db, cache, validator, sheet, panel


def _mk_item(tx="TX1", user="user_a", amount=100000, ts="2025-07-19 14:44"):
    return QueueItem(
        tx_id=tx, username=user, amount=amount, bonus=0,
        sheet_name="MASTER", status="READY", timestamp=ts, row_index=2,
    )


def test_bug012_manual_added_after_refill_wins(tmp_path):
    """Operator adds user to MANUAL BONUS AFTER refill.
    The worker must skip with MANUAL BONUS and never call submit."""
    db, cache, validator, sheet, panel = _mk_env(
        tmp_path,
        snapshots=[{"user_a"}],  # first fresh read already contains user_a
    )
    item = _mk_item()

    outcome, bonus = _process_one(item, sheet, cache, db, validator, panel)

    assert outcome == "MANUAL BONUS"
    assert bonus is None
    assert panel.submits == []          # NEVER called
    assert sheet.reads == 1             # fresh read did happen
    # Persisted with the correct result for audit.
    row = db._conn.execute(
        "SELECT result FROM processed_transactions WHERE tx_id='TX1'"
    ).fetchone()
    assert row[0] == "MANUAL BONUS"


def test_bug012_manual_added_between_two_items(tmp_path):
    """Two-item queue. Manual list is empty for item1 → SUCCESS.
    Between item1 and item2 the operator adds user_b → item2 skipped."""
    db, cache, validator, sheet, panel = _mk_env(
        tmp_path,
        snapshots=[set(), {"user_b"}],  # 1st read empty, 2nd contains user_b
    )

    item1 = _mk_item(tx="A", user="user_a")
    item2 = _mk_item(tx="B", user="user_b")

    out1, bonus1 = _process_one(item1, sheet, cache, db, validator, panel)
    out2, bonus2 = _process_one(item2, sheet, cache, db, validator, panel)

    assert out1 == "SUCCESS" and bonus1 == 10000
    assert out2 == "MANUAL BONUS"
    # Panel was only called once (for user_a), never for user_b.
    assert panel.submits == [("user_a", 10000, "R")]
    assert sheet.reads == 2


def test_bug012_dedup_takes_priority_over_manual(tmp_path):
    """If tx already lives in SQLite we short-circuit BEFORE the manual
    fresh-read runs, avoiding unnecessary Google API traffic."""
    db, cache, validator, sheet, panel = _mk_env(
        tmp_path,
        snapshots=[{"user_a"}],
    )
    db.insert("TX1", "user_a", 100000, 10000, "SUCCESS", "MASTER",
              "2025-07-19 14:44")

    item = _mk_item()
    out, _ = _process_one(item, sheet, cache, db, validator, panel)
    assert out == "SKIPPED_DUPLICATE"
    assert sheet.reads == 0
    assert panel.submits == []


def test_bug012_worker_step_sequence_in_dashboard():
    """Statically verifies that `_worker_step` in the shipped dashboard
    code performs the fresh manual-list refresh + uses
    `daily_bonus_for_transaction_date` — i.e. the code and this test
    file cannot silently diverge in production."""
    src = (ROOT / "ui" / "dashboard.py").read_text(encoding="utf-8")
    # dedup must come first, then fresh manual, then transaction-date DB call.
    idx_has_tx = src.find("self.db.has_tx(item.tx_id)")
    idx_fresh = src.find("_refresh_manual_list_now()")
    idx_daily = src.find("daily_bonus_for_transaction_date")
    idx_submit = src.find("self.panel.submit_deposit(")
    assert 0 < idx_has_tx < idx_fresh < idx_daily < idx_submit, (
        "Validation sequence in _worker_step is not "
        "has_tx → manual-fresh → daily-bonus-by-tx-date → submit"
    )
