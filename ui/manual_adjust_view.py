"""Read-only Phase 3 preview widget for Full Manual Adjust."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QComboBox, QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)


class ManualAdjustView(QWidget):
    load_requested = Signal()
    open_panel_requested = Signal()
    attach_panel_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    resume_requested = Signal()
    retry_requested = Signal()
    finalize_requested = Signal()
    reconcile_requested = Signal()
    open_cycle_requested = Signal(str)
    recover_requested = Signal()

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
        note = QLabel("TRUE AMOUNT is submitted exactly 1:1. Execution requires explicit confirmation and the Manual safety gate.")
        note.setStyleSheet("color:#C7C6BE;font-size:12px")
        layout.addWidget(note)

        self.frozen = QLabel("NO SNAPSHOT LOADED")
        self.frozen.setStyleSheet("color:#6E7180;font-weight:700;letter-spacing:2px")
        self.provenance = QLabel("Load is operator-initiated and reads MASTER exactly once.")
        self.provenance.setStyleSheet("color:#8A8C99")
        layout.addWidget(self.frozen); layout.addWidget(self.provenance)

        discovery = QHBoxLayout()
        self.cycle_selector = QComboBox()
        self.cycle_selector.setPlaceholderText("SELECT A PERSISTED CYCLE")
        self.open_cycle_button = QPushButton("OPEN SELECTED CYCLE")
        self.recover_button = QPushButton("RECOVER STALE CYCLE")
        self.open_cycle_button.clicked.connect(self._emit_open_cycle)
        self.recover_button.clicked.connect(self.recover_requested)
        self.recover_button.setEnabled(False)
        discovery.addWidget(self.cycle_selector, 1)
        discovery.addWidget(self.open_cycle_button)
        discovery.addWidget(self.recover_button)
        layout.addLayout(discovery)

        controls = QHBoxLayout()
        specs = (("OPEN PANEL", "open_panel", self.open_panel_requested),
                 ("ATTACH PANEL", "attach_panel", self.attach_panel_requested),
                 ("START MANUAL ADJUST", "start", self.start_requested),
                 ("STOP", "stop", self.stop_requested), ("RESUME", "resume", self.resume_requested),
                 ("RETRY SELECTED", "retry", self.retry_requested),
                 ("FINALIZE WITH FAILURES", "finalize", self.finalize_requested),
                 ("RECONCILE UNKNOWN", "reconcile", self.reconcile_requested))
        self.actions = {}
        self._sheet_connected = False
        self._execution_status = "PREVIEW"
        for text, key, signal in specs:
            button = QPushButton(text); button.clicked.connect(signal); button.setEnabled(False)
            controls.addWidget(button); self.actions[key] = button
        layout.addLayout(controls)
        self.execution_status = QLabel("PREVIEW")
        self.execution_status.setObjectName("StatusBadge")
        self.progress = QLabel("SUCCESS 0  •  FAILED 0  •  UNKNOWN 0  •  PENDING 0  •  TOTAL ADJUSTED SUCCESSFULLY 0")
        layout.addWidget(self.execution_status); layout.addWidget(self.progress)

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
        self._sheet_connected = connected
        self.load_button.setEnabled(connected and self._execution_status != "RUNNING")
        self.load_button.setToolTip("" if connected else "Connect the Google Sheet before loading Manual data.")

    def set_execution_state(self, status: str, summary: dict | None = None, *,
                            execution_enabled: bool = False, panel_attached: bool = False) -> None:
        summary = summary or {}
        self._execution_status = status
        self.execution_status.setText(status)
        self.progress.setText("SUCCESS {success}  •  FAILED {failed}  •  UNKNOWN {unknown}  •  PENDING {pending}  •  TOTAL ADJUSTED SUCCESSFULLY {total_adjusted_successfully:,}".format(
            success=summary.get("success", 0), failed=summary.get("failed", 0),
            unknown=summary.get("unknown", 0), pending=summary.get("pending", 0),
            total_adjusted_successfully=summary.get("total_adjusted_successfully", 0)))
        for button in self.actions.values(): button.setEnabled(False)
        panel_mutation_enabled = status != "RUNNING"
        self.actions["open_panel"].setEnabled(panel_mutation_enabled)
        self.actions["attach_panel"].setEnabled(panel_mutation_enabled)
        self.actions["start"].setEnabled(status == "PREVIEW" and execution_enabled and panel_attached and summary.get("pending", 0) > 0)
        self.actions["stop"].setEnabled(status == "RUNNING")
        self.actions["resume"].setEnabled(status == "STOPPED" and execution_enabled and panel_attached)
        self.actions["retry"].setEnabled(status == "FAILURE_REVIEW")
        self.actions["finalize"].setEnabled(status == "FAILURE_REVIEW")
        self.actions["reconcile"].setEnabled(status == "REVIEW_REQUIRED")
        self.recover_button.setEnabled(status == "RUNNING")
        navigation_enabled = status != "RUNNING"
        self.load_button.setEnabled(self._sheet_connected and navigation_enabled)
        self.cycle_selector.setEnabled(navigation_enabled)
        self.open_cycle_button.setEnabled(navigation_enabled)

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "Manual snapshot failed", message)

    def display_nonterminal_cycles(self, cycles: list[dict]) -> None:
        """List restart candidates without implicitly activating one."""
        self.cycle_selector.clear()
        self.cycle_selector.addItem("SELECT A PERSISTED CYCLE", None)
        for cycle in cycles:
            self.cycle_selector.addItem(
                f"{cycle['status']} • {cycle['created_at']} • {cycle['cycle_id']}",
                cycle["cycle_id"],
            )

    def _emit_open_cycle(self) -> None:
        cycle_id = self.cycle_selector.currentData()
        if cycle_id:
            self.open_cycle_requested.emit(str(cycle_id))

    def selected_failure_ids(self) -> list[int]:
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                selected.append(int(item.data(Qt.UserRole)))
        return selected

    def selected_unknown(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def display_failure_review(self, rows: list[dict]) -> None:
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["SELECT", "USER ID", "TRUE AMOUNT", "ATTEMPT", "PHASE", "ERROR / EVIDENCE"])
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            select = QTableWidgetItem("")
            select.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            select.setCheckState(Qt.Unchecked)
            select.setData(Qt.UserRole, int(row["transaction_id"]))
            values = (row["username"], f"{row['adjust_amount']:,}", row.get("attempt_no") or "",
                      row.get("submission_phase") or "",
                      " • ".join(x for x in (row.get("error_detail"), row.get("evidence_detail")) if x))
            self.table.setItem(index, 0, select)
            for column, value in enumerate(values, 1):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))

    def display_unknown_review(self, rows: list[dict]) -> None:
        headers = ["USER ID", "TRUE AMOUNT", "ATTEMPT", "ATTEMPT ID", "CLAIMED", "SUBMIT STARTED",
                   "CLICK BOUNDARY", "PHASE", "CLICK CROSSED", "ERROR", "EVIDENCE"]
        self.table.setColumnCount(len(headers)); self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            identity = {"transaction_id": int(row["transaction_id"]),
                        "attempt_id": row["current_attempt_id"]}
            values = (row["username"], f"{row['adjust_amount']:,}", row.get("attempt_no") or "",
                      row["current_attempt_id"], row.get("claimed_at") or "", row.get("submit_started_at") or "",
                      row.get("submit_clicked_at") or "", row.get("submission_phase") or "",
                      "UNKNOWN" if row.get("click_crossed") is None else bool(row["click_crossed"]),
                      row.get("error_detail") or "", row.get("evidence_detail") or "")
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0: item.setData(Qt.UserRole, identity)
                self.table.setItem(index, column, item)

    def display_preview(self, cycle: dict, summary, rows: list[dict]) -> None:
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["ROW", "USER ID", "TRUE AMOUNT", "STATUS", "SOURCE TX_ID", "REASON"])
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
        self.set_execution_state(cycle["status"], {"pending": summary.ready})
