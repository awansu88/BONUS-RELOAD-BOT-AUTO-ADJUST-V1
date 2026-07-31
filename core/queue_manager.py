"""
Adaptive polling queue — SQLite-backed dedup.

- `refill()` reads MASTER, pre-filters rows already in SQLite by TX_ID,
  validates the rest (with per-user cumulative bonus simulation keyed by
  the ORIGINAL TRANSACTION DATE from Google Sheets — see BUG-015),
  inserts every non-READY outcome (INVALID / LIMIT / MANUAL BONUS) into
  SQLite in one batch, and keeps only READY items in the worker queue.
- Preview is cleared and rebuilt on every refill (no appending).
- QueueItem carries only the fields the worker needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from .database import DatabaseService
from .memory_cache import MemoryCache
from .sheet_service import MasterRow, SheetService
from .timestamp_utils import parse_transaction_date
from .validator import ValidationResult, Validator


@dataclass(slots=True)
class QueueItem:
    """Minimal payload — the only shape the worker needs."""
    tx_id: str
    username: str
    amount: int
    bonus: int
    sheet_name: str
    status: str = "READY"      # READY / LIMIT / INVALID / MANUAL BONUS (preview)
    timestamp: str = ""
    row_index: int = 0
    processed: bool = False


@dataclass
class QueueStats:
    total: int = 0
    ready: int = 0
    limit: int = 0
    invalid: int = 0
    manual: int = 0
    skipped: int = 0      # written to SQLite at refill time
    processed: int = 0    # worker successes
    failed: int = 0
    already_in_db: int = 0


class QueueManager:
    def __init__(
        self,
        sheet: SheetService,
        cache: MemoryCache,
        validator: Validator,
        db: DatabaseService,
        batch_size: int = 100,
    ) -> None:
        self.sheet = sheet
        self.cache = cache
        self.validator = validator
        self.db = db
        self.batch_size = int(batch_size)

        self._ready: List[QueueItem] = []
        self._last_preview: List[QueueItem] = []
        self._stats = QueueStats()

    # ------------------------------------------------------------------
    def refill(self) -> QueueStats:
        """Read next batch, dedup via SQLite, validate, commit skips, keep
        only READY in the worker queue. Preview is fully rebuilt each call
        — nothing carries over.

        BUG-015: the per-user daily-bonus counter is keyed by the ORIGINAL
        TRANSACTION DATE parsed from the Google Sheets `TIME STAMP` cell,
        never by adjustment execution time.
        """
        rows = self.sheet.read_master_rows()

        # Pre-filter: keep only tx_ids we haven't processed yet.
        already = set()
        tx_ids = [r.tx_id for r in rows]
        if tx_ids:
            # `filter_new_tx_ids` returns the NEW ones; invert to get 'already'.
            new_set = self.db.filter_new_tx_ids(tx_ids)
            already = {t for t in tx_ids if t not in new_set}
        pending = [r for r in rows if r.tx_id not in already]
        batch = pending[: self.batch_size]

        manual_set = self.cache.manual_set()

        # Per-(user, transaction_date) cumulative simulation.
        # BUG-015: bonuses granted for transactions dated Jul 19 must
        # accumulate against Jul 19's daily cap, even when the bot runs
        # after midnight.
        simulated: Dict[Tuple[str, str], int] = {}
        db_cache: Dict[Tuple[str, str], int] = {}
        today_iso = date.today().isoformat()

        def db_daily_bonus(uid: str, ts_iso: str) -> int:
            key = (uid, ts_iso)
            if key not in db_cache:
                db_cache[key] = self.db.daily_bonus_for_transaction_date(uid, ts_iso)
            return db_cache[key]

        def current(uid: str, ts_iso: str) -> int:
            uid = str(uid).strip()
            base = db_daily_bonus(uid, ts_iso)
            return base + simulated.get((uid, ts_iso), 0)

        # Fresh preview — clear anything from the previous refresh.
        preview: List[QueueItem] = []
        ready: List[QueueItem] = []
        skip_rows: List[tuple] = []
        stats = QueueStats(total=len(batch), already_in_db=len(already))

        for r in batch:
            tx_date = parse_transaction_date(r.timestamp)
            # Fallback: use today if the sheet cell is unparseable. We keep
            # today so we don't accidentally grant an unlimited bonus for a
            # user with a corrupt timestamp cell — but the operator will
            # see the log warning through the caller.
            ts_iso = (tx_date or date.today()).isoformat()

            result = self.validator.validate(
                user_id=r.user_id,
                deposit_raw=r.true_amount,
                current_daily_bonus=current(r.user_id, ts_iso),
                manual_set=manual_set,
            )
            item = QueueItem(
                tx_id=r.tx_id,
                username=r.user_id,
                amount=int(r.true_amount),
                bonus=int(result.bonus),
                sheet_name=r.sheet_name,
                status=result.status,
                timestamp=r.timestamp,
                row_index=r.row_index,
            )
            preview.append(item)

            if result.status == "READY":
                stats.ready += 1
                ready.append(item)
                uid = str(r.user_id).strip()
                simulated[(uid, ts_iso)] = (
                    simulated.get((uid, ts_iso), 0) + int(result.bonus)
                )
                # Also keep the in-RAM cache in sync for the "today's bonus"
                # UI KPI when the tx date is today.
                if ts_iso == today_iso:
                    self.cache.add_bonus(uid, int(result.bonus))
            else:
                if result.status == "LIMIT":
                    stats.limit += 1
                elif result.status == "INVALID":
                    stats.invalid += 1
                elif result.status == "MANUAL BONUS":
                    stats.manual += 1
                # (tx_id, username, amount, bonus, result, sheet_name, timestamp)
                skip_rows.append((
                    r.tx_id, r.user_id, int(r.true_amount), 0,
                    result.status, r.sheet_name, r.timestamp,
                ))

        # Persist all non-READY outcomes in ONE SQLite call.
        if skip_rows:
            self.db.bulk_insert(skip_rows)
            stats.skipped = len(skip_rows)

        # Commit only after DB write succeeds.
        self._ready = ready
        self._last_preview = preview
        self._stats = stats
        return stats

    # ------------------------------------------------------------------
    def preview_items(self) -> List[QueueItem]:
        return list(self._last_preview)

    def items(self) -> List[QueueItem]:
        return self.preview_items()

    def next_ready(self) -> Optional[QueueItem]:
        for it in self._ready:
            if not it.processed:
                return it
        return None

    def is_empty(self) -> bool:
        return not any(not it.processed for it in self._ready)

    def ready_count(self) -> int:
        return sum(1 for it in self._ready if not it.processed)

    def stats(self) -> QueueStats:
        return self._stats

    def mark_processed(self, item: QueueItem, success: bool) -> None:
        item.processed = True
        if success:
            self._stats.processed += 1
        else:
            self._stats.failed += 1
