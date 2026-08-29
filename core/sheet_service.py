"""
Google Sheets service (gspread) — READ ONLY.

Google Sheets is now only the transaction feed. This module never writes
back — no STATUS, no BONUS, no remark. The bot deduplicates using SQLite
(see core/database.py).

Responsibilities:
    * Parse spreadsheet ID out of a full URL (never hardcoded).
    * Validate connectivity, worksheet presence, and REQUIRED columns:
        B = USER ID
        D = SHEET DATA
        E = TIME STAMP
        F = TRUE AMOUNT
        I = TX_ID
    * Read the MASTER sheet in bulk (one API call) into small MasterRow
      objects.
    * Read the MANUAL BONUS RELOAD sheet (Column B) into a HashSet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .manual_adjust_models import RawManualAdjustRow

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


@dataclass(slots=True)
class MasterRow:
    """Small immutable payload — kept minimal for low RAM usage."""
    row_index: int          # 1-based; useful only for logging
    tx_id: str
    user_id: str
    true_amount: int
    sheet_name: str
    timestamp: str = ""


@dataclass
class ConnectionInfo:
    ok: bool
    error: str = ""
    title: str = ""
    spreadsheet_id: str = ""
    tabs: List[str] = field(default_factory=list)
    missing_columns: List[str] = field(default_factory=list)


class SheetService:
    _URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")

    def __init__(self, credentials_path: str, config: Dict) -> None:
        self.credentials_path = credentials_path
        self.config = config
        self.cols = config["columns"]
        self.required_headers: Dict[str, str] = config.get("required_headers", {})
        self.sheet_names = config["sheet_names"]

        self._client: Optional[gspread.Client] = None
        self._spreadsheet: Optional[gspread.Spreadsheet] = None
        self._master = None
        self._manual = None
        self._spreadsheet_id: str = ""

    # ---------------------------------------------------------------- utils
    @classmethod
    def extract_spreadsheet_id(cls, url_or_id: str) -> Optional[str]:
        if not url_or_id:
            return None
        url_or_id = url_or_id.strip()
        m = cls._URL_RE.search(url_or_id)
        if m:
            return m.group(1)
        if "/" not in url_or_id and len(url_or_id) >= 20:
            return url_or_id
        return None

    def _authorize(self) -> gspread.Client:
        creds = Credentials.from_service_account_file(
            self.credentials_path, scopes=SCOPES
        )
        return gspread.authorize(creds)

    # ---------------------------------------------------------------- connect
    def connect(self, url_or_id: str) -> ConnectionInfo:
        sid = self.extract_spreadsheet_id(url_or_id)
        if not sid:
            return ConnectionInfo(False, error="Invalid spreadsheet URL")

        try:
            self._client = self._authorize()
            self._spreadsheet = self._client.open_by_key(sid)
            self._spreadsheet_id = sid

            tabs = [ws.title for ws in self._spreadsheet.worksheets()]

            if self.sheet_names["master"] not in tabs:
                return ConnectionInfo(
                    False,
                    error=f"Missing worksheet: {self.sheet_names['master']}",
                    tabs=tabs,
                )
            if self.sheet_names["manual_bonus_reload"] not in tabs:
                return ConnectionInfo(
                    False,
                    error=f"Missing worksheet: {self.sheet_names['manual_bonus_reload']}",
                    tabs=tabs,
                )

            self._master = self._spreadsheet.worksheet(self.sheet_names["master"])
            self._manual = self._spreadsheet.worksheet(
                self.sheet_names["manual_bonus_reload"]
            )

            # --- Required column validation on connect -------------------
            missing = self._validate_headers()
            if missing:
                return ConnectionInfo(
                    False,
                    error="MASTER is missing required columns: " + ", ".join(missing),
                    title=self._spreadsheet.title,
                    spreadsheet_id=sid,
                    tabs=tabs,
                    missing_columns=missing,
                )

            return ConnectionInfo(
                ok=True,
                title=self._spreadsheet.title,
                spreadsheet_id=sid,
                tabs=tabs,
            )
        except APIError as exc:
            return ConnectionInfo(False, error=f"Google API error: {exc}")
        except FileNotFoundError:
            return ConnectionInfo(
                False, error=f"Credentials file not found: {self.credentials_path}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            return ConnectionInfo(False, error=str(exc))

    def _validate_headers(self) -> List[str]:
        """Return the list of required columns whose header is empty."""
        if not self._master or not self.required_headers:
            return []
        try:
            header_row = self._master.row_values(1)
        except APIError as exc:
            raise RuntimeError(f"Failed reading header row: {exc}")

        missing: List[str] = []
        for key, expected in self.required_headers.items():
            col_index = int(self.cols.get(key, 0))
            if col_index <= 0:
                continue
            actual = header_row[col_index - 1] if col_index - 1 < len(header_row) else ""
            if not actual.strip():
                missing.append(f"{self._col_letter(col_index)} ({expected})")
        return missing

    # ---------------------------------------------------------------- reads
    def read_master_rows(self) -> List[MasterRow]:
        """Read MASTER once and return small MasterRow objects.

        A row is skipped if its tx_id (column I) is empty — such rows cannot
        be deduplicated safely.
        """
        if not self._master:
            raise RuntimeError("Not connected")

        values = self._master.get_all_values()
        sheet_name = self._master.title
        rows: List[MasterRow] = []

        for idx, row in enumerate(values[1:], start=2):
            def cell(col_index: int) -> str:
                i = col_index - 1
                return row[i] if 0 <= i < len(row) else ""

            tx_id = cell(self.cols["tx_id"]).strip()
            if not tx_id:
                continue

            rows.append(
                MasterRow(
                    row_index=idx,
                    tx_id=tx_id,
                    user_id=cell(self.cols["user_id"]).strip(),
                    true_amount=self._safe_int(cell(self.cols["true_amount"])),
                    sheet_name=sheet_name,
                    timestamp=cell(self.cols["time_stamp"]).strip(),
                )
            )
        return rows

    def read_manual_set(self) -> Set[str]:
        """Manual bonus list — USER IDs live in Column B only."""
        if not self._manual:
            raise RuntimeError("Not connected")
        values = self._manual.get_all_values()
        out: Set[str] = set()
        for row in values[1:]:
            if len(row) >= 2:
                uid = row[1].strip()
                if uid:
                    out.add(uid)
        return out

    def read_manual_adjust_snapshot(self) -> List[RawManualAdjustRow]:
        """Bulk-read raw MASTER B/F/I values once for Manual Adjust.

        Unlike the frozen AUTO reader, this preserves blank TX_ID and performs
        no amount conversion or business filtering.
        """
        if not self._master:
            raise RuntimeError("Not connected")
        values = self._master.get_all_values()
        rows: List[RawManualAdjustRow] = []
        for idx, row in enumerate(values[1:], start=2):
            def cell(col_index: int) -> str:
                i = col_index - 1
                return row[i] if 0 <= i < len(row) else ""
            rows.append(RawManualAdjustRow(
                source_row=idx,
                username_raw=cell(self.cols["user_id"]),
                amount_raw=cell(self.cols["true_amount"]),
                source_tx_id=cell(self.cols["tx_id"]),
            ))
        return rows

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _safe_int(value) -> int:
        if value is None:
            return 0
        s = str(value).strip().replace(",", "").replace(" ", "")
        if not s:
            return 0
        try:
            return int(float(s))
        except ValueError:
            return 0

    @staticmethod
    def _col_letter(col_index: int) -> str:
        letters = ""
        n = col_index
        while n > 0:
            n, rem = divmod(n - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    @property
    def is_connected(self) -> bool:
        return self._spreadsheet is not None

    @property
    def title(self) -> str:
        return self._spreadsheet.title if self._spreadsheet else ""

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id

    @property
    def master_name(self) -> str:
        return self._master.title if self._master else ""
