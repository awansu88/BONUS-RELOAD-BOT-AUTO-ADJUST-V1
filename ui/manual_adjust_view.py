"""Compact, state-aware presentation for Full Manual Adjust.

This widget intentionally owns presentation only.  All execution signals still
delegate to :class:`Dashboard` and the existing Manual controller.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


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
        self._sheet_connected = False
        self._execution_status = "PREVIEW"
        self._pause_requested = False
        self._pending_count = 0
        self._active_cycle_selected = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setObjectName("manual-dashboard-splitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_scroll())
        splitter.addWidget(self._build_workspace())
        splitter.setStretchFactor(0, 43)
        splitter.setStretchFactor(1, 57)
        splitter.setSizes([430, 570])
        root.addWidget(splitter)

    @staticmethod
    def _title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionTitle")
        return label

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background:#23252E;max-height:1px")
        return line

    def _build_left_scroll(self) -> QScrollArea:
        panel = QFrame(); panel.setObjectName("Card")
        box = QVBoxLayout(panel); box.setContentsMargins(16, 14, 16, 14); box.setSpacing(12)

        box.addWidget(self._title("Status"))
        status = QGridLayout(); status.setHorizontalSpacing(24); status.setVerticalSpacing(8)
        self.status_values = {}
        for row, (key, value) in enumerate((
            ("GOOGLE SHEETS", "Not connected"), ("PANEL", "Closed"),
            ("MANUAL CYCLE", "No snapshot"), ("EXECUTION GATE", "DISABLED"),
            ("LAST SNAPSHOT", "Never"),
        )):
            caption = QLabel(key.title()); caption.setObjectName("StatLabel")
            label = QLabel(value); label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            caption.setMinimumHeight(24); label.setMinimumHeight(24)
            status.addWidget(caption, row, 0); status.addWidget(label, row, 1)
            self.status_values[key] = label
        box.addLayout(status); box.addWidget(self._divider())

        box.addWidget(self._title("Actions"))
        self.action_grid = QGridLayout()
        self.action_grid.setHorizontalSpacing(10); self.action_grid.setVerticalSpacing(10)
        specs = (
            ("LOAD MANUAL DATA", "load", self.load_requested),
            ("OPEN PANEL", "open_panel", self.open_panel_requested),
            ("READY", "attach_panel", self.attach_panel_requested),
            ("START MANUAL ADJUST", "start", self.start_requested),
            ("PAUSE", "stop", self.stop_requested), ("CONTINUE", "resume", self.resume_requested),
            ("RETRY SELECTED", "retry", self.retry_requested),
            ("FINALIZE WITH FAILURES", "finalize", self.finalize_requested),
            ("RECONCILE UNKNOWN", "reconcile", self.reconcile_requested),
        )
        self.actions = {}
        for index, (text, key, signal) in enumerate(specs):
            button = QPushButton(text); button.setObjectName(f"manual-{key}-btn")
            button.setMinimumHeight(44 if key in ("start", "stop", "resume") else 38)
            if key == "stop": button.clicked.connect(self._request_pause)
            else: button.clicked.connect(signal)
            self.action_grid.addWidget(button, index // 2, index % 2)
            self.actions[key] = button
        self.load_button = self.actions["load"]  # compatibility for dashboard/tests
        box.addLayout(self.action_grid); box.addWidget(self._divider())

        box.addWidget(self._title("Current Execution"))
        current = QFrame(); current.setObjectName("SubCard")
        current.setMinimumHeight(72)
        current_grid = QGridLayout(current); current_grid.setContentsMargins(12, 12, 12, 12)
        current_grid.setVerticalSpacing(8)
        self.current_values = {}
        # The repository execution summary is aggregate-only.  Keep this card
        # deliberately limited to values the view can state authoritatively.
        for row, (key, value) in enumerate((("STATE", "Idle"),
                                            ("ACTIVE TRANSACTION", "—"))):
            caption = QLabel(key); caption.setObjectName("KpiCaption")
            label = QLabel(value); label.setAlignment(Qt.AlignRight); label.setObjectName("KpiSmall")
            current_grid.addWidget(caption, row, 0); current_grid.addWidget(label, row, 1)
            self.current_values[key] = label
        box.addWidget(current)

        box.addWidget(self._title("Progress"))
        progress_card = QFrame(); progress_card.setObjectName("SubCard")
        progress_card.setMinimumHeight(70)
        progress_box = QVBoxLayout(progress_card); progress_box.setContentsMargins(12, 12, 12, 12)
        progress_box.setSpacing(8)
        self.progress_text = QLabel("Processed 0 / 0"); self.progress_percent = QLabel("0%")
        progress_row = QHBoxLayout(); progress_row.addWidget(self.progress_text); progress_row.addStretch(1); progress_row.addWidget(self.progress_percent)
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 100); self.progress_bar.setValue(0); self.progress_bar.setTextVisible(False)
        progress_box.addLayout(progress_row); progress_box.addWidget(self.progress_bar); box.addWidget(progress_card)

        box.addWidget(self._title("Execution Summary"))
        summary_card = QFrame(); summary_card.setObjectName("SubCard")
        summary_card.setMinimumHeight(112)
        summary_grid = QGridLayout(summary_card); summary_grid.setContentsMargins(12, 12, 12, 12)
        summary_grid.setHorizontalSpacing(12); summary_grid.setVerticalSpacing(8)
        self.execution_values = {}
        colors = {"SUCCESS": "#4ADE80", "FAILED": "#EF4444", "UNKNOWN": "#F59E0B",
                  "PENDING": "#C7C6BE", "SUBMITTING": "#3B82F6",
                  "TOTAL ADJUSTED": "#F5B301"}
        for index, key in enumerate(colors):
            caption = QLabel(key); caption.setObjectName("KpiCaption")
            value = QLabel("0"); value.setStyleSheet(f"color:{colors[key]};font-size:15px;font-weight:700")
            cell = QVBoxLayout(); cell.setSpacing(3); cell.addWidget(value); cell.addWidget(caption)
            summary_grid.addLayout(cell, index // 2, index % 2)
            self.execution_values[key] = value
        box.addWidget(summary_card); box.addStretch(1)

        scroll = QScrollArea(); scroll.setObjectName("manual-controls-scroll")
        scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame); scroll.setWidget(panel)
        return scroll

    def _build_workspace(self) -> QFrame:
        panel = QFrame(); panel.setObjectName("Card")
        box = QVBoxLayout(panel); box.setContentsMargins(14, 12, 14, 12); box.setSpacing(9)
        box.addWidget(self._title("Manual Snapshot"))
        self.frozen = QLabel("NO SNAPSHOT LOADED"); self.frozen.setStyleSheet("color:#6E7180;font-weight:700;letter-spacing:2px")
        self.provenance = QLabel("Connect Google Sheets and click LOAD MANUAL DATA to freeze the current MASTER rows for review.")
        self.provenance.setWordWrap(True); self.provenance.setStyleSheet("color:#8A8C99;font-size:11px")
        box.addWidget(self.frozen); box.addWidget(self.provenance)

        box.addWidget(self._title("Previous / Recovery Cycles"))
        cycle_row = QHBoxLayout(); cycle_row.setSpacing(8)
        self.cycle_selector = QComboBox(); self.cycle_selector.setObjectName("manual-cycle-selector")
        self.cycle_selector.setPlaceholderText("SELECT PERSISTED CYCLE")
        self.open_cycle_button = QPushButton("OPEN")
        self.recover_button = QPushButton("RECOVER STALE CYCLE")
        self.open_cycle_button.clicked.connect(self._emit_open_cycle); self.recover_button.clicked.connect(self.recover_requested)
        cycle_row.addWidget(self.cycle_selector, 1); cycle_row.addWidget(self.open_cycle_button); cycle_row.addWidget(self.recover_button)
        box.addLayout(cycle_row)

        box.addWidget(self._title("Snapshot Summary"))
        metrics = QFrame(); metrics.setObjectName("SubCard")
        metric_grid = QGridLayout(metrics); metric_grid.setContentsMargins(10, 6, 10, 6); metric_grid.setSpacing(6)
        self.values = {}
        for index, key in enumerate(("SOURCE ROWS", "UNIQUE USERS", "READY", "DUPLICATE", "INVALID", "TOTAL ADJUSTMENT")):
            value = QLabel("0"); value.setStyleSheet("color:#ECEBE4;font-size:15px;font-weight:700")
            caption = QLabel(key); caption.setObjectName("KpiCaption")
            cell = QVBoxLayout(); cell.setSpacing(0); cell.addWidget(value); cell.addWidget(caption)
            metric_grid.addLayout(cell, index // 3, index % 3); self.values[key] = value
        box.addWidget(metrics)

        table_header = QHBoxLayout(); table_header.addWidget(self._title("Transactions")); table_header.addStretch(1)
        self.execution_status = QLabel("NO SNAPSHOT"); self.execution_status.setObjectName("StatusBadge"); table_header.addWidget(self.execution_status)
        box.addLayout(table_header)
        self.table = QTableWidget(0, 6); self.table.setObjectName("manual-transactions")
        self.table.setHorizontalHeaderLabels(["ROW", "USER ID", "TRUE AMOUNT", "STATUS", "SOURCE TX_ID", "REASON"])
        self.table.verticalHeader().setVisible(False); self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True); self._set_preview_column_widths()
        box.addWidget(self.table, 1)
        return panel

    def _set_preview_column_widths(self) -> None:
        header = self.table.horizontalHeader()
        for column in range(self.table.columnCount()): header.setSectionResizeMode(column, QHeaderView.Interactive)
        for column, width in enumerate((55, 150, 110, 95, 125)): self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(self.table.columnCount() - 1, QHeaderView.Stretch)

    def _request_pause(self) -> None:
        if self._execution_status != "RUNNING" or self._pause_requested: return
        self._pause_requested = True
        self.actions["stop"].setText("PAUSING..."); self.actions["stop"].setEnabled(False)
        self.current_values["STATE"].setText("Pausing")
        self.stop_requested.emit()

    def set_sheet_connected(self, connected: bool) -> None:
        self._sheet_connected = connected
        self.status_values["GOOGLE SHEETS"].setText("Connected" if connected else "Not connected")
        self._apply_action_state()

    @staticmethod
    def _operator_status(status: str) -> str:
        return {"STOPPED": "PAUSED", "FAILURE_REVIEW": "FAILURE REVIEW",
                "REVIEW_REQUIRED": "REVIEW REQUIRED", "HARD_STOPPED": "HARD STOPPED"}.get(status, status)

    def set_execution_state(self, status: str, summary: dict | None = None, *,
                            execution_enabled: bool = False, panel_attached: bool = False,
                            panel_open: bool = False,
                            active_cycle_selected: bool | None = None) -> None:
        summary = summary or {}; self._execution_status = status
        if active_cycle_selected is not None:
            self._active_cycle_selected = active_cycle_selected
        if status != "RUNNING": self._pause_requested = False
        shown = self._operator_status(status)
        self.execution_status.setText(shown); self.status_values["MANUAL CYCLE"].setText(shown)
        self.status_values["EXECUTION GATE"].setText("ENABLED" if execution_enabled else "DISABLED")
        self.status_values["EXECUTION GATE"].setStyleSheet(f"color:{'#4ADE80' if execution_enabled else '#EF4444'};font-weight:700")
        self.status_values["PANEL"].setText(
            "Attached" if panel_attached else "Open" if panel_open else "Closed")
        state_names = {"PREVIEW": "Idle", "RUNNING": "Pausing" if self._pause_requested else "Running",
                       "STOPPED": "Paused", "FAILURE_REVIEW": "Failure Review", "REVIEW_REQUIRED": "Review Required",
                       "COMPLETED": "Completed", "HARD_STOPPED": "Hard Stopped"}
        self.current_values["STATE"].setText(state_names.get(status, shown.title()))
        self.current_values["ACTIVE TRANSACTION"].setText("—")
        success, failed = int(summary.get("success", 0)), int(summary.get("failed", 0))
        unknown, pending = int(summary.get("unknown", 0)), int(summary.get("pending", 0))
        submitting = int(summary.get("submitting", 0))
        self._pending_count = pending
        total = success + failed + unknown + pending + submitting
        processed = success + failed + unknown
        percent = round(processed * 100 / total) if total else 0
        self.progress_text.setText(f"Processed {processed:,} / {total:,}"); self.progress_percent.setText(f"{percent}%"); self.progress_bar.setValue(percent)
        for key, value in (("SUCCESS", success), ("FAILED", failed), ("UNKNOWN", unknown),
                           ("PENDING", pending), ("SUBMITTING", submitting),
                           ("TOTAL ADJUSTED", int(summary.get("total_adjusted_successfully", 0)))):
            self.execution_values[key].setText(f"{value:,}")
        self._execution_enabled = execution_enabled; self._panel_attached = panel_attached
        self._apply_action_state()

    def reset_unselected_state(self, *, execution_enabled: bool, panel_attached: bool,
                               panel_open: bool = False) -> None:
        """Clear only the visible selection while preserving persisted cycles."""
        self._pause_requested = False
        self._active_cycle_selected = False
        self.frozen.setText("NO SNAPSHOT LOADED")
        self.provenance.setText(
            "Connect Google Sheets and click LOAD MANUAL DATA to freeze the current MASTER rows for review.")
        self.table.clearContents(); self.table.setRowCount(0); self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["ROW", "USER ID", "TRUE AMOUNT", "STATUS", "SOURCE TX_ID", "REASON"])
        self._set_preview_column_widths()
        for value in self.values.values(): value.setText("0")
        self.status_values["LAST SNAPSHOT"].setText("Never")
        self.set_execution_state("PREVIEW", {}, execution_enabled=execution_enabled,
                                 panel_attached=panel_attached, panel_open=panel_open)
        self.status_values["MANUAL CYCLE"].setText("No snapshot")
        self.execution_status.setText("NO SNAPSHOT")

    def _apply_action_state(self) -> None:
        status = self._execution_status
        for button in self.actions.values(): button.hide(); button.setEnabled(False)
        self.recover_button.setVisible(status == "RUNNING"); self.recover_button.setEnabled(status == "RUNNING")
        if status == "PREVIEW":
            for key in ("load", "open_panel", "attach_panel", "start"): self.actions[key].show()
            self.actions["load"].setEnabled(self._sheet_connected)
            self.actions["open_panel"].setEnabled(True); self.actions["attach_panel"].setEnabled(True)
            self.actions["start"].setEnabled(getattr(self, "_execution_enabled", False)
                                             and getattr(self, "_panel_attached", False)
                                             and self._active_cycle_selected
                                             and self._pending_count > 0)
        elif status == "RUNNING":
            self.actions["stop"].show(); self.actions["stop"].setText("PAUSING..." if self._pause_requested else "PAUSE")
            self.actions["stop"].setEnabled(not self._pause_requested)
        elif status == "STOPPED":
            self.actions["resume"].show(); self.actions["resume"].setEnabled(getattr(self, "_execution_enabled", False) and getattr(self, "_panel_attached", False))
        elif status == "FAILURE_REVIEW":
            for key in ("retry", "finalize"): self.actions[key].show(); self.actions[key].setEnabled(True)
        elif status == "REVIEW_REQUIRED":
            self.actions["reconcile"].show(); self.actions["reconcile"].setEnabled(True)
        navigation_enabled = status != "RUNNING"
        self.cycle_selector.setEnabled(navigation_enabled); self.open_cycle_button.setEnabled(navigation_enabled)

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "Manual snapshot failed", message)

    def display_nonterminal_cycles(self, cycles: list[dict]) -> None:
        self.cycle_selector.clear(); self.cycle_selector.addItem("SELECT PERSISTED CYCLE", None)
        for cycle in cycles:
            self.cycle_selector.addItem(f"{self._operator_status(cycle['status'])} • {cycle['created_at']} • {cycle['cycle_id']}", cycle["cycle_id"])

    def _emit_open_cycle(self) -> None:
        cycle_id = self.cycle_selector.currentData()
        if cycle_id: self.open_cycle_requested.emit(str(cycle_id))

    def selected_failure_ids(self) -> list[int]:
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.Checked: selected.append(int(item.data(Qt.UserRole)))
        return selected

    def selected_unknown(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0: return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def display_failure_review(self, rows: list[dict]) -> None:
        self.table.setColumnCount(6); self.table.setHorizontalHeaderLabels(["SELECT", "USER ID", "TRUE AMOUNT", "ATTEMPT", "PHASE", "ERROR / EVIDENCE"])
        self.table.setRowCount(len(rows)); self._set_preview_column_widths()
        for index, row in enumerate(rows):
            select = QTableWidgetItem(""); select.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable); select.setCheckState(Qt.Unchecked); select.setData(Qt.UserRole, int(row["transaction_id"])); self.table.setItem(index, 0, select)
            values = (row["username"], f"{row['adjust_amount']:,}", row.get("attempt_no") or "", row.get("submission_phase") or "", " • ".join(x for x in (row.get("error_detail"), row.get("evidence_detail")) if x))
            for column, value in enumerate(values, 1): self.table.setItem(index, column, QTableWidgetItem(str(value)))

    def display_unknown_review(self, rows: list[dict]) -> None:
        headers = ["USER ID", "TRUE AMOUNT", "ATTEMPT", "ATTEMPT ID", "CLAIMED", "SUBMIT STARTED", "CLICK BOUNDARY", "PHASE", "CLICK CROSSED", "ERROR", "EVIDENCE"]
        self.table.setColumnCount(len(headers)); self.table.setHorizontalHeaderLabels(headers); self.table.setRowCount(len(rows)); self._set_preview_column_widths()
        for index, row in enumerate(rows):
            identity = {"transaction_id": int(row["transaction_id"]), "attempt_id": row["current_attempt_id"]}
            values = (row["username"], f"{row['adjust_amount']:,}", row.get("attempt_no") or "", row["current_attempt_id"], row.get("claimed_at") or "", row.get("submit_started_at") or "", row.get("submit_clicked_at") or "", row.get("submission_phase") or "", "UNKNOWN" if row.get("click_crossed") is None else bool(row["click_crossed"]), row.get("error_detail") or "", row.get("evidence_detail") or "")
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0: item.setData(Qt.UserRole, identity)
                self.table.setItem(index, column, item)

    def display_preview(self, cycle: dict, summary, rows: list[dict]) -> None:
        self.table.setColumnCount(6); self.table.setHorizontalHeaderLabels(["ROW", "USER ID", "TRUE AMOUNT", "STATUS", "SOURCE TX_ID", "REASON"]); self._set_preview_column_widths()
        for key, value in (("SOURCE ROWS", summary.source_rows), ("UNIQUE USERS", summary.unique_users), ("READY", summary.ready), ("DUPLICATE", summary.duplicates), ("INVALID", summary.invalid), ("TOTAL ADJUSTMENT", summary.total_adjustment_amount)):
            self.values[key].setText(f"{value:,}")
        self.frozen.setText("SNAPSHOT FROZEN — READ ONLY")
        self.status_values["LAST SNAPSHOT"].setText(str(cycle["loaded_at"])); self.status_values["MANUAL CYCLE"].setText(self._operator_status(cycle["status"]))
        self.provenance.setText(f"{cycle['loaded_at']}  •  Cycle {cycle['cycle_id'][:12]}…  •  Source: {cycle['sheet_name']}  •  TRUE AMOUNT: Exact 1:1  •  Fingerprint {cycle['snapshot_fingerprint'][:12]}…")
        self.table.setRowCount(len(rows)); colors = {"READY": "#4ADE80", "DUPLICATE": "#F5B301", "INVALID": "#EF4444"}; source_rows_by_id = {row["source_row_id"]: row["source_row"] for row in rows}
        for index, row in enumerate(rows):
            amount = f"{row['parsed_amount']:,}" if row["parsed_amount"] is not None else str(row["amount_raw"] or "")
            winner = f" (first occurrence: row {source_rows_by_id.get(row['winner_source_row_id'], '?')})" if row["winner_source_row_id"] is not None else ""
            values = (row["source_row"], row["username"] or str(row["username_raw"] or ""), amount, row["classification"], row["source_tx_id"] or "", (row["reason"] or "") + winner)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 2: item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 3: item.setForeground(QColor(colors.get(row["classification"], "#ECEBE4")))
                self.table.setItem(index, column, item)
        self.set_execution_state(cycle["status"], {"pending": summary.ready})
