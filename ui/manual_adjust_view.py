"""Read-only Phase 3 preview widget for Full Manual Adjust."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)


class ManualAdjustView(QWidget):
    load_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        title_row = QHBoxLayout()
        title = QLabel("FULL MANUAL ADJUST")
        title.setStyleSheet("color:#F5B301;font-size:22px;font-weight:800;letter-spacing:4px")
        self.load_button = QPushButton("LOAD MANUAL DATA")
        self.load_button.setProperty("cls", "primary")
        self.load_button.setEnabled(False)
        self.load_button.clicked.connect(self.load_requested)
        title_row.addWidget(title); title_row.addStretch(1); title_row.addWidget(self.load_button)
        layout.addLayout(title_row)
        note = QLabel("TRUE AMOUNT is adjusted exactly 1:1. Preview only — no submission is available in Phase 3.")
        note.setStyleSheet("color:#C7C6BE;font-size:12px")
        layout.addWidget(note)

        self.frozen = QLabel("NO SNAPSHOT LOADED")
        self.frozen.setStyleSheet("color:#6E7180;font-weight:700;letter-spacing:2px")
        self.provenance = QLabel("Load is operator-initiated and reads MASTER exactly once.")
        self.provenance.setStyleSheet("color:#8A8C99")
        layout.addWidget(self.frozen); layout.addWidget(self.provenance)

        cards = QGridLayout()
        self.values = {}
        for i, key in enumerate(("SOURCE ROWS", "UNIQUE USERS", "READY", "DUPLICATE", "INVALID", "TOTAL ADJUSTMENT")):
            card = QFrame(); card.setObjectName("SubCard")
            box = QVBoxLayout(card)
            value = QLabel("0"); value.setStyleSheet("color:#F5B301;font-size:20px;font-weight:800")
            caption = QLabel(key); caption.setObjectName("KpiCaption")
            box.addWidget(value); box.addWidget(caption)
            cards.addWidget(card, i // 3, i % 3)
            self.values[key] = value
        layout.addLayout(cards)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["ROW", "USER ID", "TRUE AMOUNT", "STATUS", "SOURCE TX_ID", "REASON"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table, 1)

    def set_sheet_connected(self, connected: bool) -> None:
        self.load_button.setEnabled(connected)
        self.load_button.setToolTip("" if connected else "Connect the Google Sheet before loading Manual data.")

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "Manual snapshot failed", message)

    def display_preview(self, cycle: dict, summary, rows: list[dict]) -> None:
        self.values["SOURCE ROWS"].setText(f"{summary.source_rows:,}")
        self.values["UNIQUE USERS"].setText(f"{summary.unique_users:,}")
        self.values["READY"].setText(f"{summary.ready:,}")
        self.values["DUPLICATE"].setText(f"{summary.duplicates:,}")
        self.values["INVALID"].setText(f"{summary.invalid:,}")
        self.values["TOTAL ADJUSTMENT"].setText(f"{summary.total_adjustment_amount:,}")
        self.frozen.setText("SNAPSHOT FROZEN — READ ONLY")
        fp = cycle["snapshot_fingerprint"][:12]
        self.provenance.setText(
            f"Cycle {cycle['cycle_id']}  •  Loaded {cycle['loaded_at']}  •  "
            f"{cycle['sheet_name']}  •  Fingerprint {fp}…"
        )
        self.table.setRowCount(len(rows))
        colors = {"READY": "#4ADE80", "DUPLICATE": "#F5B301", "INVALID": "#EF4444"}
        source_rows_by_id = {row["source_row_id"]: row["source_row"] for row in rows}
        for index, row in enumerate(rows):
            amount = f"{row['parsed_amount']:,}" if row["parsed_amount"] is not None else str(row["amount_raw"] or "")
            winner = ""
            if row["winner_source_row_id"] is not None:
                winner_row = source_rows_by_id.get(row["winner_source_row_id"], "?")
                winner = f" (first occurrence: row {winner_row})"
            values = (row["source_row"], row["username"] or str(row["username_raw"] or ""),
                      amount, row["classification"], row["source_tx_id"] or "",
                      (row["reason"] or "") + winner)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 3:
                    item.setForeground(QColor(colors.get(row["classification"], "#ECEBE4")))
                self.table.setItem(index, column, item)
