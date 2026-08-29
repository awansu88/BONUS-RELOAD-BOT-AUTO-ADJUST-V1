"""Manual Adjust domain types and deliberately strict input parsing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


SQLITE_INTEGER_MAX = 9_223_372_036_854_775_807


class CycleStatus(str, Enum):
    LOADING = "LOADING"
    PREVIEW = "PREVIEW"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILURE_REVIEW = "FAILURE_REVIEW"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class TransactionStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    SUCCESS = "SUCCESS"
    FAILED_NOT_SUBMITTED = "FAILED_NOT_SUBMITTED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class AttemptResult(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED_NOT_SUBMITTED = "FAILED_NOT_SUBMITTED"
    UNKNOWN = "UNKNOWN"


class SourceClassification(str, Enum):
    READY = "READY"
    DUPLICATE = "DUPLICATE"
    INVALID = "INVALID"


def normalize_username(raw_username: object) -> tuple[str, str]:
    """Return the locked display value and duplicate key: strip, then lower."""
    username = "" if raw_username is None else str(raw_username).strip()
    return username, username.lower()


def parse_true_amount(value: object) -> int:
    """Parse a positive base-10 integer without ever converting through float.

    Commas and ordinary ASCII spaces are accepted as display separators.  No
    sign, decimal, exponent, special float, boolean, or out-of-range value is
    accepted.
    """
    if value is None or isinstance(value, (bool, float)):
        raise ValueError("amount must be a positive integer")
    text = str(value).strip()
    if not text:
        raise ValueError("amount is blank")
    compact = text.replace(",", "").replace(" ", "")
    if not compact or not compact.isascii() or not compact.isdigit():
        raise ValueError("amount must contain decimal digits only")
    amount = int(compact, 10)
    if amount <= 0:
        raise ValueError("amount must be greater than zero")
    if amount > SQLITE_INTEGER_MAX:
        raise ValueError("amount exceeds SQLite INTEGER maximum")
    return amount


@dataclass(frozen=True, slots=True)
class RawManualAdjustRow:
    source_row: int
    username_raw: str
    amount_raw: str
    source_tx_id: str = ""


@dataclass(frozen=True, slots=True)
class ClassifiedSourceRow:
    source_row: int
    username_raw: str
    amount_raw: str
    source_tx_id: str
    username: str
    username_key: str
    parsed_amount: Optional[int]
    classification: SourceClassification
    reason: Optional[str] = None
    winner_source_row: Optional[int] = None


@dataclass(frozen=True, slots=True)
class ManualAdjustTransaction:
    transaction_id: int
    cycle_id: str
    source_row_id: int
    source_row: int
    username: str
    username_key: str
    adjust_amount: int
    status: TransactionStatus
    attempt_count: int
    current_attempt_id: Optional[str]


@dataclass(frozen=True, slots=True)
class CycleSummary:
    source_rows: int
    unique_users: int
    ready: int
    duplicates: int
    invalid: int
    total_adjustment_amount: int
