"""Finite, persisted queue for a single Manual Adjust cycle."""

from __future__ import annotations

from collections import deque

from .manual_adjust_repository import ManualAdjustRepository


class ManualAdjustQueue:
    def __init__(self, repository: ManualAdjustRepository, cycle_id: str):
        self.cycle_id = cycle_id
        self._items = deque(repository.get_pending_transactions(cycle_id))

    def next_pending(self):
        return self._items.popleft() if self._items else None

    def is_empty(self) -> bool:
        return not self._items

    def __len__(self) -> int:
        return len(self._items)
