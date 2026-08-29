"""One-read loader for immutable Full Manual Adjust snapshots."""

from __future__ import annotations

import hashlib
import json

from .manual_adjust_models import (ClassifiedSourceRow, RawManualAdjustRow,
    SourceClassification, normalize_username, parse_true_amount)
from .manual_adjust_repository import ManualAdjustRepository


def snapshot_fingerprint(spreadsheet_id: str, sheet_name: str,
                         rows: list[RawManualAdjustRow]) -> str:
    payload = {"spreadsheet_id": spreadsheet_id, "sheet_name": sheet_name,
               "rows": [[r.source_row, r.username_raw, r.amount_raw, r.source_tx_id] for r in rows]}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def classify_rows(rows: list[RawManualAdjustRow]) -> list[ClassifiedSourceRow]:
    seen: dict[str, int] = {}
    out: list[ClassifiedSourceRow] = []
    for raw in rows:
        username, key = normalize_username(raw.username_raw)
        if not username:
            out.append(ClassifiedSourceRow(raw.source_row,raw.username_raw,raw.amount_raw,raw.source_tx_id,
                username,key,None,SourceClassification.INVALID,"blank username"))
            continue
        if key in seen:
            out.append(ClassifiedSourceRow(raw.source_row,raw.username_raw,raw.amount_raw,raw.source_tx_id,
                username,key,None,SourceClassification.DUPLICATE,"duplicate username",seen[key]))
            continue
        seen[key] = raw.source_row  # ownership precedes amount validation
        try:
            amount = parse_true_amount(raw.amount_raw)
        except ValueError as exc:
            out.append(ClassifiedSourceRow(raw.source_row,raw.username_raw,raw.amount_raw,raw.source_tx_id,
                username,key,None,SourceClassification.INVALID,str(exc)))
        else:
            out.append(ClassifiedSourceRow(raw.source_row,raw.username_raw,raw.amount_raw,raw.source_tx_id,
                username,key,amount,SourceClassification.READY))
    return out


class ManualAdjustLoader:
    def __init__(self, sheet_service, repository: ManualAdjustRepository):
        self.sheet_service = sheet_service
        self.repository = repository

    def load(self) -> str:
        # This is deliberately the loader's sole SheetService call.
        rows = self.sheet_service.read_manual_adjust_snapshot()
        spreadsheet_id = self.sheet_service.spreadsheet_id
        sheet_name = self.sheet_service.master_name
        fingerprint = snapshot_fingerprint(spreadsheet_id, sheet_name, rows)
        return self.repository.create_snapshot(spreadsheet_id, sheet_name, fingerprint, classify_rows(rows))
