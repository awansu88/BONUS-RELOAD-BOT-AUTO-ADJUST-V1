"""
Utility helpers shared by the core engine.

Currently exposes:

* parse_transaction_date(ts)  ->  date | None
      Robust parser for Google Sheets `TIME STAMP` column values.
      Handles the common formats emitted by Sheets and by hand-editing.
      Returns `None` when the string is unparseable — callers must NOT
      guess a date in that case.

Format matrix accepted (whitespace tolerant, case-insensitive month):
    2025-07-19                          ISO date
    2025-07-19 14:44                    ISO date + time (min or sec)
    2025-07-19 14:44:00
    2025/07/19 14:44                    ISO with slashes
    19/07/2025 14:44                    day-first with slashes (Indonesian)
    19-07-2025 14:44                    day-first with dashes
    19 Jul 2025 14:44                   day + short month + year
    19 Jul 14:44                        day + short month (assumes current year)
    Jul 19 2025 14:44                   month + day + year
    Jul 19 14:44                        month + day (assumes current year)
    07/19/2025 14:44                    US-style (only used when day > 12)
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _current_year() -> int:
    return datetime.now().year


def parse_transaction_date(timestamp: object) -> Optional[date]:
    """Best-effort parser. Returns a `date` or `None`.

    We deliberately return `None` (never an exception) so callers can
    decide how to fall back — usually to `processed_at` — with a warning.
    """
    if timestamp is None:
        return None

    # Native types first.
    if isinstance(timestamp, datetime):
        return timestamp.date()
    if isinstance(timestamp, date):
        return timestamp

    s = str(timestamp).strip()
    if not s:
        return None

    # Try Python's own well-known formats first (fast path).
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    # 19 Jul 14:44   /   19 Jul 2025 14:44
    m = re.match(
        r"^(\d{1,2})\s+([A-Za-z]{3,9})\s*(\d{2,4})?(?:[\s,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$",
        s,
    )
    if m:
        day = int(m.group(1))
        mon = _MONTHS.get(m.group(2).lower())
        year_s = m.group(3)
        year = int(year_s) if year_s else _current_year()
        if year < 100:
            year += 2000
        if mon and 1 <= day <= 31:
            try:
                return date(year, mon, day)
            except ValueError:
                return None

    # Jul 19 14:44   /   Jul 19 2025 14:44
    m = re.match(
        r"^([A-Za-z]{3,9})\s+(\d{1,2})\s*(\d{2,4})?(?:[\s,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$",
        s,
    )
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        day = int(m.group(2))
        year_s = m.group(3)
        year = int(year_s) if year_s else _current_year()
        if year < 100:
            year += 2000
        if mon and 1 <= day <= 31:
            try:
                return date(year, mon, day)
            except ValueError:
                return None

    # 19/07/2025 [time]   or   07/19/2025 [time]   or   19-07-2025 [time]
    m = re.match(
        r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$",
        s,
    )
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        # Heuristic:
        #   * If first number > 12 it MUST be the day (day-first).
        #   * Else if the second number > 12 it MUST be the day (US-style).
        #   * Otherwise assume day-first (Indonesian / EU convention which
        #     is what this bot's operators use).
        if a > 12:
            day, mon = a, b
        elif b > 12:
            day, mon = b, a
        else:
            day, mon = a, b
        try:
            return date(y, mon, day)
        except ValueError:
            return None

    return None


def to_iso_date(d: Optional[date]) -> Optional[str]:
    """Convenience: `2025-07-19` (matches SQLite `date()` output)."""
    return d.isoformat() if d else None
