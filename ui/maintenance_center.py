"""
Production hardening v1.2.0 — Maintenance Center (C-1 + C-6).

A single tabbed dialog that surfaces every maintenance / diagnostics
capability added in v1.2:

    Database     — retention purge + integrity + ANALYZE + optional VACUUM
    Logs         — list, export folder, delete old
    Screenshots  — list, delete old, retention
    Backups      — one-click DB backup with checkpoint
    Diagnostics  — versions + config validation report
    Health       — live snapshot from `core.health.HealthMonitor`
                   with PASS / WARNING / FAILED score + export

The dialog never touches the production engine directly — it calls
`MaintenanceService` / `HealthMonitor` / `config_validator`.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.config_validator import ConfigReport, validate_configuration
from core.health import HealthMonitor, HealthSnapshot, collect_versions
from core.maintenance import MaintenanceReport, MaintenanceService


# --------------------------------------------------------------------------- helpers
def _fmt_size(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _open_in_file_manager(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


# =========================================================================
# Maintenance Center
# =========================================================================
class MaintenanceCenter(QDialog):
    RETENTION_CHOICES = ("3 days", "7 days", "15 days", "30 days", "Custom", "None")

    def __init__(
        self,
        *,
        parent: Optional[QWidget],
        maintenance: MaintenanceService,
        health: HealthMonitor,
        worker_running: bool,
        app_dir: Path,
        resource_dir: Path,
        config_path: Path,
        selectors_path: Path,
        credentials_path: Path,
        sqlite_path: Path,
        browser_profile_dir: Path,
        logs_dir: Path,
        screenshots_dir: Path,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("maintenance-center")
        self.setWindowTitle("Maintenance Center")
        self.resize(720, 560)

        self.maintenance = maintenance
        self.health = health
        self.worker_running = worker_running
        self.app_dir = app_dir
        self.resource_dir = resource_dir
        self.config_path = config_path
        self.selectors_path = selectors_path
        self.credentials_path = credentials_path
        self.sqlite_path = sqlite_path
        self.browser_profile_dir = browser_profile_dir
        self.logs_dir = logs_dir
        self.screenshots_dir = screenshots_dir

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        header = QLabel("MAINTENANCE CENTER")
        header.setStyleSheet(
            "color:#F5B301; font-size:13px; font-weight:800; letter-spacing:4px;"
        )
        root.addWidget(header)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("maintenance-tabs")
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_database_tab(), "Database")
        self.tabs.addTab(self._build_logs_tab(), "Logs")
        self.tabs.addTab(self._build_screenshots_tab(), "Screenshots")
        self.tabs.addTab(self._build_backups_tab(), "Backups")
        self.tabs.addTab(self._build_diagnostics_tab(), "Diagnostics")
        self.tabs.addTab(self._build_health_tab(), "Health")

        footer = QHBoxLayout()
        footer.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("maintenance-close-btn")
        close.setMinimumWidth(100)
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        root.addLayout(footer)

        # Initial refresh of every live-data tab.
        self._refresh_database_kpis()
        self._refresh_logs_list()
        self._refresh_screenshots_list()
        self._refresh_health()

    # ================================================================ DATABASE (C-2)
    def _build_database_tab(self) -> QWidget:
        w = QWidget(); w.setObjectName("maintenance-tab-database")
        v = QVBoxLayout(w); v.setSpacing(12)

        # KPIs
        kpi_box = QGroupBox("SQLite")
        grid = QGridLayout(kpi_box); grid.setHorizontalSpacing(28); grid.setVerticalSpacing(6)
        self.db_kpi_today = QLabel("0"); self.db_kpi_today.setObjectName("db-processed-today")
        self.db_kpi_total = QLabel("0"); self.db_kpi_total.setObjectName("db-total-processed")
        self.db_kpi_size  = QLabel("0"); self.db_kpi_size.setObjectName("db-size")
        self.db_kpi_vacuum = QLabel("never"); self.db_kpi_vacuum.setObjectName("db-last-vacuum")
        for row, (label, val) in enumerate([
            ("Processed Today", self.db_kpi_today),
            ("Total Processed", self.db_kpi_total),
            ("Database Size",   self.db_kpi_size),
            ("Last VACUUM",     self.db_kpi_vacuum),
        ]):
            l = QLabel(label); l.setStyleSheet("color:#6E7180; letter-spacing:2px; font-size:10px;")
            val.setStyleSheet("color:#F5B301; font-weight:700; font-size:14px;")
            grid.addWidget(l, row, 0); grid.addWidget(val, row, 1)
        v.addWidget(kpi_box)

        # Retention selector
        ret_box = QGroupBox("Retention & Maintenance")
        rl = QGridLayout(ret_box); rl.setHorizontalSpacing(14); rl.setVerticalSpacing(8)
        rl.addWidget(QLabel("Delete rows older than"), 0, 0)
        self.retention_combo = QComboBox()
        self.retention_combo.setObjectName("retention-combo")
        for name in self.RETENTION_CHOICES:
            self.retention_combo.addItem(name)
        self.retention_combo.currentIndexChanged.connect(self._on_retention_changed)
        rl.addWidget(self.retention_combo, 0, 1)

        self.retention_custom = QSpinBox()
        self.retention_custom.setObjectName("retention-custom-days")
        self.retention_custom.setRange(1, 3650)
        self.retention_custom.setValue(30)
        self.retention_custom.setEnabled(False)
        self.retention_custom.setSuffix(" days")
        rl.addWidget(self.retention_custom, 0, 2)

        self.btn_run_maint = QPushButton("Run Maintenance")
        self.btn_run_maint.setObjectName("run-maintenance-btn")
        self.btn_run_maint.clicked.connect(self._on_run_maintenance)
        rl.addWidget(self.btn_run_maint, 1, 0, 1, 3)

        self.maint_note = QLabel(
            "VACUUM only runs manually and never while monitoring."
        )
        self.maint_note.setStyleSheet("color:#8A8C99; font-size:11px;")
        rl.addWidget(self.maint_note, 2, 0, 1, 3)
        v.addWidget(ret_box)

        # Output
        self.db_output = QPlainTextEdit(); self.db_output.setObjectName("maintenance-output")
        self.db_output.setReadOnly(True)
        self.db_output.setMaximumBlockCount(500)
        v.addWidget(self.db_output, 1)

        return w

    def _on_retention_changed(self, index: int) -> None:
        label = self.retention_combo.currentText()
        self.retention_custom.setEnabled(label == "Custom")

    def _refresh_database_kpis(self) -> None:
        db = self.maintenance.db
        self.db_kpi_today.setText(f"{db.processed_today_count():,}")
        self.db_kpi_total.setText(f"{db.total_count():,}")
        self.db_kpi_size.setText(_fmt_size(db.size_bytes()))
        self.db_kpi_vacuum.setText(db.last_vacuum() or "never")

    def _resolve_retention_days(self) -> Optional[int]:
        label = self.retention_combo.currentText()
        if label == "None":
            return None
        if label == "Custom":
            return int(self.retention_custom.value())
        return int(label.split()[0])

    def _on_run_maintenance(self) -> None:
        days = self._resolve_retention_days()
        run_vacuum = not self.worker_running
        vacuum_note = "VACUUM included" if run_vacuum else "VACUUM skipped (worker running)"

        if days is not None:
            preview = self.maintenance.db.count_older_than(int(days))
            ans = QMessageBox.question(
                self,
                "Run Maintenance",
                f"This will delete {preview:,} row(s) older than {days} days,\n"
                f"then run integrity check, ANALYZE, PRAGMA optimize.\n"
                f"{vacuum_note}.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
        else:
            ans = QMessageBox.question(
                self,
                "Run Maintenance",
                f"This will run integrity check, ANALYZE, PRAGMA optimize.\n"
                f"{vacuum_note}.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return

        report = self.maintenance.full_maintenance(
            retention_days=days, run_vacuum=run_vacuum
        )
        self.db_output.appendPlainText(report.summary())
        self._refresh_database_kpis()

    # ================================================================ LOGS (C-4)
    def _build_logs_tab(self) -> QWidget:
        w = QWidget(); w.setObjectName("maintenance-tab-logs")
        v = QVBoxLayout(w); v.setSpacing(10)

        top = QHBoxLayout()
        self.logs_count_lbl = QLabel("0 files")
        self.logs_count_lbl.setStyleSheet("color:#F5B301; font-weight:700;")
        top.addWidget(self.logs_count_lbl)
        top.addStretch(1)

        self.btn_logs_open = QPushButton("Open Folder")
        self.btn_logs_open.setObjectName("logs-open-btn")
        self.btn_logs_open.clicked.connect(lambda: _open_in_file_manager(self.logs_dir))
        self.btn_logs_export = QPushButton("Export Buffer")
        self.btn_logs_export.setObjectName("logs-export-btn")
        self.btn_logs_export.clicked.connect(self._on_logs_export)
        self.btn_logs_delete = QPushButton("Delete Old Logs")
        self.btn_logs_delete.setObjectName("logs-delete-btn")
        self.btn_logs_delete.clicked.connect(self._on_logs_delete)
        for b in (self.btn_logs_open, self.btn_logs_export, self.btn_logs_delete):
            top.addWidget(b)
        v.addLayout(top)

        row = QHBoxLayout()
        row.addWidget(QLabel("Delete logs older than"))
        self.logs_days = QSpinBox()
        self.logs_days.setObjectName("logs-retention-days")
        self.logs_days.setRange(1, 365); self.logs_days.setValue(30); self.logs_days.setSuffix(" days")
        row.addWidget(self.logs_days)
        row.addStretch(1)
        v.addLayout(row)

        self.logs_list = QPlainTextEdit(); self.logs_list.setObjectName("logs-list")
        self.logs_list.setReadOnly(True)
        v.addWidget(self.logs_list, 1)
        return w

    def _refresh_logs_list(self) -> None:
        files = self.maintenance.list_logs()
        self.logs_count_lbl.setText(f"{len(files)} files")
        lines = []
        for p in files:
            try:
                st = p.stat()
                mtime = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
                lines.append(f"{p.name}    {_fmt_size(st.st_size):>10}    {mtime}")
            except Exception:
                lines.append(p.name)
        self.logs_list.setPlainText("\n".join(lines) or "(no log files)")

    def _on_logs_delete(self) -> None:
        days = int(self.logs_days.value())
        ans = QMessageBox.question(
            self, "Delete old logs",
            f"Delete rotated log files older than {days} days?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        report = self.maintenance.cleanup_logs(older_than_days=days)
        self._refresh_logs_list()
        QMessageBox.information(self, "Log cleanup", report.summary())

    def _on_logs_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export logs folder listing",
            datetime.now().strftime("logs-listing-%Y%m%d-%H%M%S.txt"),
            "Text (*.txt)",
        )
        if not path:
            return
        try:
            Path(path).write_text(self.logs_list.toPlainText(), encoding="utf-8")
            QMessageBox.information(self, "Export complete", f"Saved to {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    # ================================================================ SCREENSHOTS (C-5)
    def _build_screenshots_tab(self) -> QWidget:
        w = QWidget(); w.setObjectName("maintenance-tab-screenshots")
        v = QVBoxLayout(w); v.setSpacing(10)

        top = QHBoxLayout()
        self.sc_count_lbl = QLabel("0 files")
        self.sc_count_lbl.setStyleSheet("color:#F5B301; font-weight:700;")
        top.addWidget(self.sc_count_lbl)
        top.addStretch(1)
        self.btn_sc_open = QPushButton("Open Folder")
        self.btn_sc_open.setObjectName("screenshots-open-btn")
        self.btn_sc_open.clicked.connect(lambda: _open_in_file_manager(self.screenshots_dir))
        self.btn_sc_delete = QPushButton("Delete Old")
        self.btn_sc_delete.setObjectName("screenshots-delete-btn")
        self.btn_sc_delete.clicked.connect(self._on_screenshots_delete)
        top.addWidget(self.btn_sc_open)
        top.addWidget(self.btn_sc_delete)
        v.addLayout(top)

        row = QHBoxLayout()
        row.addWidget(QLabel("Delete screenshots older than"))
        self.sc_days = QSpinBox()
        self.sc_days.setObjectName("screenshots-retention-days")
        self.sc_days.setRange(1, 365); self.sc_days.setValue(14); self.sc_days.setSuffix(" days")
        row.addWidget(self.sc_days)
        row.addStretch(1)
        v.addLayout(row)

        self.sc_list = QPlainTextEdit(); self.sc_list.setObjectName("screenshots-list")
        self.sc_list.setReadOnly(True)
        v.addWidget(self.sc_list, 1)
        return w

    def _refresh_screenshots_list(self) -> None:
        files = self.maintenance.list_screenshots()
        self.sc_count_lbl.setText(f"{len(files)} files")
        lines = []
        for p in files:
            try:
                st = p.stat()
                mtime = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
                lines.append(f"{p.name}    {_fmt_size(st.st_size):>10}    {mtime}")
            except Exception:
                lines.append(p.name)
        self.sc_list.setPlainText("\n".join(lines) or "(no screenshots)")

    def _on_screenshots_delete(self) -> None:
        days = int(self.sc_days.value())
        ans = QMessageBox.question(
            self, "Delete old screenshots",
            f"Delete screenshots older than {days} days?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        report = self.maintenance.cleanup_screenshots(older_than_days=days)
        self._refresh_screenshots_list()
        QMessageBox.information(self, "Screenshot cleanup", report.summary())

    # ================================================================ BACKUPS
    def _build_backups_tab(self) -> QWidget:
        w = QWidget(); w.setObjectName("maintenance-tab-backups")
        v = QVBoxLayout(w); v.setSpacing(10)

        info = QLabel(
            "Backup checkpoints the WAL and copies the .db file. Safe to run\n"
            "while monitoring — the copy is atomic on Windows/NTFS."
        )
        info.setStyleSheet("color:#8A8C99;")
        v.addWidget(info)

        row = QHBoxLayout()
        self.btn_backup_now = QPushButton("Backup Now")
        self.btn_backup_now.setObjectName("backup-now-btn")
        self.btn_backup_now.clicked.connect(self._on_backup_now)
        self.btn_export_csv = QPushButton("Export CSV")
        self.btn_export_csv.setObjectName("export-csv-db-btn")
        self.btn_export_csv.clicked.connect(self._on_export_csv)
        row.addWidget(self.btn_backup_now)
        row.addWidget(self.btn_export_csv)
        row.addStretch(1)
        v.addLayout(row)

        self.backup_output = QPlainTextEdit(); self.backup_output.setObjectName("backup-output")
        self.backup_output.setReadOnly(True)
        v.addWidget(self.backup_output, 1)
        return w

    def _on_backup_now(self) -> None:
        default = datetime.now().strftime("processed_%Y-%m-%d_%H-%M-%S.db")
        path, _ = QFileDialog.getSaveFileName(
            self, "Backup Database", default, "SQLite DB (*.db)"
        )
        if not path:
            return
        try:
            self.maintenance.db.backup(path)
            msg = f"Backup saved to {path}"
            self.backup_output.appendPlainText(msg)
            QMessageBox.information(self, "Backup complete", msg)
        except Exception as exc:
            QMessageBox.critical(self, "Backup failed", str(exc))

    def _on_export_csv(self) -> None:
        default = datetime.now().strftime("processed-export-%Y%m%d-%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Database (CSV)", default, "CSV (*.csv)"
        )
        if not path:
            return
        try:
            n = self.maintenance.db.export_csv(path)
            msg = f"Exported {n:,} rows to {path}"
            self.backup_output.appendPlainText(msg)
            QMessageBox.information(self, "Export complete", msg)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    # ================================================================ DIAGNOSTICS (C-6 + C-7)
    def _build_diagnostics_tab(self) -> QWidget:
        w = QWidget(); w.setObjectName("maintenance-tab-diagnostics")
        v = QVBoxLayout(w); v.setSpacing(10)

        row = QHBoxLayout()
        self.btn_diag_refresh = QPushButton("Refresh")
        self.btn_diag_refresh.setObjectName("diag-refresh-btn")
        self.btn_diag_refresh.clicked.connect(self._refresh_diagnostics)
        self.btn_diag_export = QPushButton("Export Diagnostic Report")
        self.btn_diag_export.setObjectName("diag-export-btn")
        self.btn_diag_export.clicked.connect(self._on_diag_export)
        row.addWidget(self.btn_diag_refresh)
        row.addWidget(self.btn_diag_export)
        row.addStretch(1)
        v.addLayout(row)

        self.diag_view = QPlainTextEdit(); self.diag_view.setObjectName("diag-view")
        self.diag_view.setReadOnly(True)
        v.addWidget(self.diag_view, 1)

        self._refresh_diagnostics()
        return w

    def _diagnostics_report_text(self) -> str:
        versions = collect_versions(pw_browsers_dir=self.resource_dir / "pw-browsers")
        cfg_report: ConfigReport = validate_configuration(
            app_dir=self.app_dir,
            config_path=self.config_path,
            selectors_path=self.selectors_path,
            credentials_path=self.credentials_path,
            sqlite_path=self.sqlite_path,
            browser_profile_dir=self.browser_profile_dir,
        )
        lines = []
        lines.append("=== VERSIONS ===")
        for k, v in versions.items():
            lines.append(f"  {k:>12s}: {v}")
        lines.append("")
        lines.append(cfg_report.summary())
        return "\n".join(lines)

    def _refresh_diagnostics(self) -> None:
        self.diag_view.setPlainText(self._diagnostics_report_text())

    def _on_diag_export(self) -> None:
        default = datetime.now().strftime("diagnostics-%Y%m%d-%H%M%S.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Diagnostic Report", default, "Text (*.txt)"
        )
        if not path:
            return
        try:
            snap = self.health.snapshot()
            payload = "\n".join([
                self._diagnostics_report_text(),
                "",
                "=== HEALTH ===",
                f"  score      : {snap.score}",
                f"  memory     : {snap.memory_mb!r} MB",
                f"  threads    : {snap.thread_count!r}",
                f"  handles    : {snap.handle_count!r}",
                f"  browsers   : {snap.browser_contexts}",
                f"  qtimers    : {snap.qtimer_count!r}",
                f"  sqlite_ok  : {snap.sqlite_ok}",
                f"  google_ok  : {snap.google_ok}",
                f"  worker     : {snap.worker_state}",
                f"  queue_size : {snap.queue_size}",
                f"  warnings   : {snap.warnings}",
            ])
            Path(path).write_text(payload, encoding="utf-8")
            QMessageBox.information(self, "Export complete", f"Saved to {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    # ================================================================ HEALTH (C-6)
    def _build_health_tab(self) -> QWidget:
        w = QWidget(); w.setObjectName("maintenance-tab-health")
        v = QVBoxLayout(w); v.setSpacing(10)

        row = QHBoxLayout()
        self.health_score = QLabel("PASS")
        self.health_score.setObjectName("health-score")
        self.health_score.setStyleSheet(
            "color:#4ADE80; font-size:24px; font-weight:800; letter-spacing:4px;"
        )
        row.addWidget(self.health_score)
        row.addStretch(1)
        self.btn_health_refresh = QPushButton("Refresh")
        self.btn_health_refresh.setObjectName("health-refresh-btn")
        self.btn_health_refresh.clicked.connect(self._refresh_health)
        row.addWidget(self.btn_health_refresh)
        v.addLayout(row)

        grid = QGridLayout(); grid.setHorizontalSpacing(24); grid.setVerticalSpacing(6)
        self.h_mem = QLabel("-"); self.h_mem.setObjectName("health-memory")
        self.h_threads = QLabel("-"); self.h_threads.setObjectName("health-threads")
        self.h_handles = QLabel("-"); self.h_handles.setObjectName("health-handles")
        self.h_browsers = QLabel("-"); self.h_browsers.setObjectName("health-browsers")
        self.h_qtimers = QLabel("-"); self.h_qtimers.setObjectName("health-qtimers")
        self.h_sqlite = QLabel("-"); self.h_sqlite.setObjectName("health-sqlite")
        self.h_google = QLabel("-"); self.h_google.setObjectName("health-google")
        self.h_worker = QLabel("-"); self.h_worker.setObjectName("health-worker")
        self.h_queue = QLabel("-"); self.h_queue.setObjectName("health-queue")

        stats = [
            ("Memory (MB)",       self.h_mem),
            ("Threads",           self.h_threads),
            ("Handles",           self.h_handles),
            ("Browser contexts",  self.h_browsers),
            ("QTimer count",      self.h_qtimers),
            ("SQLite",            self.h_sqlite),
            ("Google",            self.h_google),
            ("Worker",            self.h_worker),
            ("Queue size",        self.h_queue),
        ]
        for i, (name, lbl) in enumerate(stats):
            cap = QLabel(name); cap.setStyleSheet("color:#8A8C99;")
            lbl.setStyleSheet("color:#ECEBE4; font-weight:700;")
            grid.addWidget(cap, i, 0); grid.addWidget(lbl, i, 1)
        v.addLayout(grid)

        self.h_warnings = QPlainTextEdit(); self.h_warnings.setObjectName("health-warnings")
        self.h_warnings.setReadOnly(True)
        v.addWidget(self.h_warnings, 1)
        return w

    def _refresh_health(self) -> None:
        s: HealthSnapshot = self.health.snapshot()
        self.h_mem.setText("-" if s.memory_mb is None else f"{s.memory_mb:.1f}")
        self.h_threads.setText("-" if s.thread_count is None else str(s.thread_count))
        self.h_handles.setText("-" if s.handle_count is None else str(s.handle_count))
        self.h_browsers.setText(str(s.browser_contexts))
        self.h_qtimers.setText("-" if s.qtimer_count is None else str(s.qtimer_count))
        self.h_sqlite.setText("OK" if s.sqlite_ok else "FAILED")
        self.h_google.setText("OK" if s.google_ok else "-")
        self.h_worker.setText(s.worker_state)
        self.h_queue.setText(str(s.queue_size))
        self.h_warnings.setPlainText("\n".join(s.warnings) if s.warnings else "no warnings")
        score = s.score
        colour = {"PASS": "#4ADE80", "WARNING": "#F5B301", "FAILED": "#EF4444"}.get(score, "#ECEBE4")
        self.health_score.setText(score)
        self.health_score.setStyleSheet(
            f"color:{colour}; font-size:24px; font-weight:800; letter-spacing:4px;"
        )
