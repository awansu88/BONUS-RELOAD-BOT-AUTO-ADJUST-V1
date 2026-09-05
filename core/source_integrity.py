"""Fail-closed primitives shared by AUTO source and accounting paths."""

from __future__ import annotations

from datetime import date

from .timestamp_utils import parse_transaction_date


class SourceIntegrityError(RuntimeError):
    """The source cannot safely prove a financial transaction identity."""


class AccountingIntegrityError(RuntimeError):
    """Durable accounting history cannot safely prove available quota."""


def canonical_username_key(value) -> str:
    """Return the sole AUTO accounting identity (never the panel identity)."""
    return str(value or "").strip().lower()


def normalize_header(value) -> str:
    """Normalize only for exact contractual MASTER header comparison."""
    return " ".join(str(value or "").strip().split()).lower()


def require_iso_business_date(value) -> str:
    """Require an already-canonical ISO calendar date."""
    raw = str(value or "")
    try:
        parsed = date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise SourceIntegrityError(f"invalid business_date {raw!r}; expected YYYY-MM-DD")
    if parsed.isoformat() != raw:
        raise SourceIntegrityError(f"invalid business_date {raw!r}; expected YYYY-MM-DD")
    return raw


def require_source_date(source_timestamp, business_date: str, *, tx_id: str = "") -> str:
    """Prove a source timestamp agrees with its supplied accounting date."""
    business_date = require_iso_business_date(business_date)
    parsed = parse_transaction_date(source_timestamp)
    if parsed is None:
        raise SourceIntegrityError(
            f"TX_ID {tx_id!r}: invalid TIME STAMP {source_timestamp!r}"
        )
    if parsed.isoformat() != business_date:
        raise SourceIntegrityError(
            f"TX_ID {tx_id!r}: TIME STAMP {source_timestamp!r} resolves to "
            f"{parsed.isoformat()}, not business_date {business_date}"
        )
    return business_date
