"""
Bonus rule engine.

Given a deposit (TRUE AMOUNT) and the user's already-received daily bonus,
compute the actual bonus to give.

Rules (from config.json):
    daily_limit    = 10000
    tiers          = [{min_deposit: 100000, bonus: 10000},
                      {min_deposit:  50000, bonus:  5000}]

    bonus_to_give = MIN(tier_bonus, daily_limit - current_daily_bonus)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ValidationResult:
    status: str            # READY | LIMIT | INVALID | MANUAL BONUS
    bonus: int             # bonus amount to give (0 if none)
    reason: str = ""       # human-readable reason


class Validator:
    def __init__(self, bonus_rules: Dict, manual_status: str = "MANUAL BONUS") -> None:
        self.daily_limit: int = int(bonus_rules.get("daily_limit", 10000))
        self.tiers: List[Dict] = sorted(
            bonus_rules.get("tiers", []),
            key=lambda t: t["min_deposit"],
            reverse=True,
        )
        self.manual_status = manual_status

    def _tier_bonus(self, deposit: int) -> int:
        for tier in self.tiers:
            if deposit >= int(tier["min_deposit"]):
                return int(tier["bonus"])
        return 0

    def validate(
        self,
        user_id: str,
        deposit_raw,
        current_daily_bonus: int,
        manual_set: set,
    ) -> ValidationResult:
        # 1. Manual bonus reload
        if user_id and user_id in manual_set:
            return ValidationResult(self.manual_status, 0, "user in manual list")

        # 2. Parse deposit
        deposit = self._to_int(deposit_raw)
        if deposit is None or deposit <= 0:
            return ValidationResult("INVALID", 0, "invalid deposit")
        if not user_id or not str(user_id).strip():
            return ValidationResult("INVALID", 0, "empty user id")

        # 3. Tier bonus
        tier_bonus = self._tier_bonus(deposit)
        if tier_bonus <= 0:
            return ValidationResult("INVALID", 0, "deposit below minimum tier")

        # 4. Daily limit
        remaining = self.daily_limit - int(current_daily_bonus or 0)
        if remaining <= 0:
            return ValidationResult("LIMIT", 0, "daily limit reached")

        bonus_to_give = min(tier_bonus, remaining)
        if bonus_to_give <= 0:
            return ValidationResult("LIMIT", 0, "daily limit reached")

        return ValidationResult("READY", bonus_to_give)

    @staticmethod
    def _to_int(value) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip().replace(",", "").replace(".", "").replace(" ", "")
        if not s or not s.lstrip("-").isdigit():
            return None
        try:
            return int(s)
        except ValueError:
            return None
