"""
In-memory cache for daily bonuses and manual-reload users.

- Loaded from Google Sheets when session starts / reconnects / date changes.
- Manual list refreshed every N seconds by the dashboard.
- Daily bonus map updated locally after each successful adjustment
  (avoids re-reading the MASTER sheet).
"""

from __future__ import annotations

from datetime import date
from threading import RLock
from typing import Dict, Set


class MemoryCache:
    def __init__(self) -> None:
        self._lock = RLock()
        self._daily_bonus: Dict[str, int] = {}
        self._manual: Set[str] = set()
        self._loaded_date: date | None = None

    # ---------- daily bonus ----------
    def set_daily_bonus(self, mapping: Dict[str, int]) -> None:
        with self._lock:
            self._daily_bonus = {str(k).strip(): int(v or 0) for k, v in mapping.items() if k}
            self._loaded_date = date.today()

    def get_daily_bonus(self, user_id: str) -> int:
        with self._lock:
            return int(self._daily_bonus.get(str(user_id).strip(), 0))

    def add_bonus(self, user_id: str, amount: int) -> None:
        with self._lock:
            uid = str(user_id).strip()
            self._daily_bonus[uid] = self._daily_bonus.get(uid, 0) + int(amount)

    def loaded_date(self) -> date | None:
        with self._lock:
            return self._loaded_date

    def is_stale(self) -> bool:
        with self._lock:
            return self._loaded_date is None or self._loaded_date != date.today()

    # ---------- manual list ----------
    def set_manual(self, users: Set[str]) -> None:
        with self._lock:
            self._manual = {str(u).strip() for u in users if str(u).strip()}

    def manual_set(self) -> Set[str]:
        with self._lock:
            return set(self._manual)

    def in_manual(self, user_id: str) -> bool:
        with self._lock:
            return str(user_id).strip() in self._manual
