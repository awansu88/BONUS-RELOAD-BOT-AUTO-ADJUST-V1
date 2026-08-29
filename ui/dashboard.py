"""
Dashboard - PySide6 Modern Dark UI (Amber / Gold accent).

Layout:
    LEFT COLUMN  : status + action buttons + counters
    RIGHT COLUMN : live log

The dashboard drives the whole session via QTimer ticks:
    - manual list refresh every N seconds
    - a single-worker "step" that processes one queue item per tick, so we
      never block the Qt event loop and never need extra threads.

STOP finishes the current transaction (already logged) and returns to Idle.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QComboBox,
    QStackedWidget,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.database import DatabaseService
from core.logger import AppLogger
from core.memory_cache import MemoryCache
from core.panel_service import PanelService
from core.queue_manager import QueueItem, QueueManager
from core.sheet_service import SheetService
from core.validator import Validator
from core.recovery import RetryExhausted, retry_with_ladder, safe_run
from core.health import HealthMonitor, LeakThresholds
from core.maintenance import MaintenanceService
from core.crash_state import CrashState, CrashStateStore
from core.manual_adjust_loader import ManualAdjustLoader
from core.manual_adjust_repository import ManualAdjustRepository
from ui.manual_adjust_state import ManualPreviewState, OperatingMode
from ui.manual_adjust_view import ManualAdjustView


# -------------------------------------------------------------------- theme
DARK_STYLE = """
* { font-family: 'Segoe UI', 'Inter', sans-serif; color: #ECEBE4; }
QMainWindow, QWidget { background-color: #0E0F13; }
QFrame#Card {
    background-color: #17181F;
    border: 1px solid #23252E;
    border-radius: 12px;
}
QLabel#SectionTitle {
    color: #F5B301;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
}
QLabel#StatValue {
    color: #F5B301;
    font-size: 20px;
    font-weight: 700;
}
QLabel#StatLabel {
    color: #6E7180;
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
}
QLabel#DotOk    { color: #4ADE80; font-size: 14px; }
QLabel#DotWarn  { color: #F5B301; font-size: 14px; }
QLabel#DotErr   { color: #EF4444; font-size: 14px; }
QLabel#DotIdle  { color: #6E7180; font-size: 14px; }

QLineEdit, QSpinBox, QPlainTextEdit, QTableWidget {
    background-color: #0E0F13;
    border: 1px solid #23252E;
    border-radius: 8px;
    padding: 8px 10px;
    color: #ECEBE4;
    selection-background-color: #F5B30133;
}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus, QTableWidget:focus {
    border: 1px solid #F5B301;
}
QPlainTextEdit {
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 12px;
    color: #C7C6BE;
}
QPushButton {
    background-color: transparent;
    border: 1px solid #2F313C;
    color: #ECEBE4;
    padding: 10px 16px;
    border-radius: 8px;
    font-weight: 600;
    letter-spacing: 0.5px;
}
QPushButton:hover { border-color: #F5B301; color: #F5B301; }
QPushButton:pressed { background-color: #F5B30122; }
QPushButton:disabled { color: #4A4C58; border-color: #23252E; }
QPushButton[cls="primary"], QPushButton#connect-sheet-btn, QPushButton#start-btn {
    background-color: #F5B301;
    color: #0E0F13;
    border: 1px solid #F5B301;
}
QPushButton[cls="primary"]:hover,
QPushButton#connect-sheet-btn:hover,
QPushButton#start-btn:hover { background-color: #FFC42B; border-color: #FFC42B; }
QPushButton[cls="primary"]:disabled,
QPushButton#connect-sheet-btn:disabled,
QPushButton#start-btn:disabled {
    background-color: #3B2E00; color: #7A6A1F; border-color: #3B2E00;
}
QPushButton#stop-btn {
    background-color: transparent;
    color: #EF4444;
    border: 1px solid #EF4444;
    font-weight: 700;
}
QPushButton#stop-btn:hover { background-color: #EF444422; }
QPushButton#stop-btn:disabled { color: #5A2626; border-color: #3B1616; }

QTableWidget { gridline-color: #23252E; }
QHeaderView::section {
    background-color: #17181F;
    color: #F5B301;
    border: none;
    padding: 8px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    font-size: 10px;
}
QScrollBar:vertical, QScrollBar:horizontal {
    background: transparent;
    width: 8px;
    height: 8px;
}
QScrollBar::handle {
    background: #2F313C;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:hover { background: #F5B301; }
QScrollBar::add-line, QScrollBar::sub-line { background: transparent; height: 0; width: 0; }

QProgressBar {
    background-color: #0E0F13;
    border: 1px solid #23252E;
    border-radius: 6px;
    text-align: center;
    color: #ECEBE4;
    font-weight: 700;
    letter-spacing: 1px;
    height: 20px;
}
QProgressBar::chunk {
    background-color: #F5B301;
    border-radius: 5px;
}

QFrame#SubCard {
    background-color: #0E0F13;
    border: 1px solid #23252E;
    border-radius: 10px;
}
QLabel#Kpi { color: #F5B301; font-size: 22px; font-weight: 800; letter-spacing: 1px; }
QLabel#KpiSmall { color: #ECEBE4; font-size: 15px; font-weight: 700; }
QLabel#KpiCaption { color: #6E7180; font-size: 9px; letter-spacing: 2px; text-transform: uppercase; }
QLabel#StatusBadge {
    color: #F5B301; font-size: 11px; font-weight: 700; letter-spacing: 2px;
    padding: 3px 10px; border: 1px solid #F5B30155; border-radius: 4px;
}
"""


# -------------------------------------------------------------------- helpers
def status_color(status: str) -> str:
    s = (status or "").upper()
    return {
        "READY": "#4ADE80",
        "PROCESSED": "#4ADE80",
        "LIMIT": "#F5B301",
        "MANUAL BONUS": "#3B82F6",
        "INVALID": "#EF4444",
        "FAILED": "#EF4444",
    }.get(s, "#ECEBE4")


# =========================================================================
# Settings dialog
# =========================================================================
class SettingsDialog(QDialog):
    def __init__(self, config: dict, config_path: Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self.config = config
        self.config_path = config_path

        form = QFormLayout()

        self.panel_edit = QLineEdit(config.get("panel_url", ""))
        self.panel_edit.setPlaceholderText("https://admin.example.com/deposit")
        self.panel_edit.setObjectName("panel-url-input")
        form.addRow("Panel URL", self.panel_edit)

        self.daily_limit = QSpinBox()
        self.daily_limit.setRange(0, 10_000_000)
        self.daily_limit.setValue(int(config["bonus_rules"]["daily_limit"]))
        form.addRow("Daily Bonus Limit", self.daily_limit)

        self.batch = QSpinBox()
        self.batch.setRange(1, 1000)
        self.batch.setValue(int(config.get("batch_size", 100)))
        form.addRow("Batch Size", self.batch)

        self.reload_iv = QSpinBox()
        self.reload_iv.setRange(5, 3600)
        self.reload_iv.setValue(int(config.get("manual_reload_interval_sec", 30)))
        form.addRow("Manual Reload Interval (s)", self.reload_iv)

        self.polling = QSpinBox()
        self.polling.setRange(0, 60)
        self.polling.setValue(int(config.get("polling_delay_sec", 2)))
        form.addRow("Polling Delay (s)", self.polling)

        self.monitoring_iv = QSpinBox()
        self.monitoring_iv.setRange(2, 3600)
        self.monitoring_iv.setValue(int(config.get("monitoring_interval_sec", 10)))
        form.addRow("Monitoring Interval (s)", self.monitoring_iv)

        self.remark = QLineEdit(config.get("remark", "BONUS RELOAD AUTO"))
        form.addRow("Remark", self.remark)

        self.creds = QLineEdit(config.get("google_credentials", "credentials/service_account.json"))
        form.addRow("Google Credentials", self.creds)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _save(self) -> None:
        self.config["panel_url"] = self.panel_edit.text().strip()
        self.config["bonus_rules"]["daily_limit"] = int(self.daily_limit.value())
        self.config["batch_size"] = int(self.batch.value())
        self.config["manual_reload_interval_sec"] = int(self.reload_iv.value())
        self.config["polling_delay_sec"] = int(self.polling.value())
        self.config["monitoring_interval_sec"] = int(self.monitoring_iv.value())
        self.config["remark"] = self.remark.text().strip() or "BONUS RELOAD AUTO"
        self.config["google_credentials"] = self.creds.text().strip()

        self.config_path.write_text(json.dumps(self.config, indent=4), encoding="utf-8")
        self.accept()


# =========================================================================
# Preview dialog
# =========================================================================
class PreviewDialog(QDialog):
    def __init__(self, items: list[QueueItem], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preview Queue")
        self.resize(760, 520)

        table = QTableWidget(len(items), 4, self)
        table.setObjectName("preview-table")
        table.setHorizontalHeaderLabels(["User ID", "Deposit", "Bonus", "Status"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)

        for row, it in enumerate(items):
            table.setItem(row, 0, QTableWidgetItem(it.username))
            table.setItem(row, 1, QTableWidgetItem(f"{it.amount:,}"))
            table.setItem(row, 2, QTableWidgetItem(f"{it.bonus:,}"))
            s = QTableWidgetItem(it.status)
            s.setForeground(QColor(status_color(it.status)))
            table.setItem(row, 3, s)

        close = QPushButton("Close")
        close.setObjectName("start-btn")
        close.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(table)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)


# =========================================================================
# Database dialog
# =========================================================================
class DatabaseDialog(QDialog):
    """Read-only overview + three maintenance actions."""

    def __init__(self, db, worker_running: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Database")
        self.setMinimumWidth(460)
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        title = QLabel("DATABASE")
        title.setStyleSheet(
            "color:#F5B301; font-size:12px; font-weight:800; letter-spacing:3px;"
        )
        layout.addWidget(title)

        kpi = QFrame(); kpi.setObjectName("SubCard")
        g = QGridLayout(kpi)
        g.setContentsMargins(16, 12, 16, 12)
        g.setHorizontalSpacing(24); g.setVerticalSpacing(6)

        rows = [
            ("Processed Today",   self._fmt_int(db.processed_today_count()), "db-processed-today"),
            ("Total Processed",   self._fmt_int(db.total_count()),           "db-total-processed"),
            ("Database Size",     self._fmt_size(db.size_bytes()),           "db-size"),
            ("Status",            "Connected",                               "db-status"),
        ]
        for i, (label, value, tid) in enumerate(rows):
            cap = QLabel(label)
            cap.setStyleSheet("color:#6E7180; font-size:10px; letter-spacing:2px; text-transform:uppercase;")
            val = QLabel(value)
            val.setObjectName(tid)
            val.setStyleSheet("color:#F5B301; font-size:15px; font-weight:700;")
            g.addWidget(cap, i, 0)
            g.addWidget(val, i, 1)
        layout.addWidget(kpi)

        btn_row = QHBoxLayout(); btn_row.setSpacing(10)
        self.export_btn = QPushButton("EXPORT DATABASE")
        self.export_btn.setObjectName("db-export-btn")
        self.backup_btn = QPushButton("BACKUP DATABASE")
        self.backup_btn.setObjectName("db-backup-btn")
        self.maint_btn = QPushButton("MAINTENANCE")
        self.maint_btn.setObjectName("db-maintenance-btn")
        for b in (self.export_btn, self.backup_btn, self.maint_btn):
            b.setMinimumHeight(36)

        # Maintenance is destructive-ish; block it while worker is running.
        self.maint_btn.setEnabled(not worker_running)
        if worker_running:
            self.maint_btn.setToolTip("Stop the worker before running maintenance.")

        self.export_btn.clicked.connect(self._on_export)
        self.backup_btn.clicked.connect(self._on_backup)
        self.maint_btn.clicked.connect(self._on_maintenance)

        btn_row.addWidget(self.export_btn)
        btn_row.addWidget(self.backup_btn)
        btn_row.addWidget(self.maint_btn)
        layout.addLayout(btn_row)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("start-btn")
        close.clicked.connect(self.accept)
        close_row.addWidget(close)
        layout.addLayout(close_row)

    # ---------------- actions ----------------
    def _on_export(self) -> None:
        default = datetime.now().strftime("processed-export-%Y%m%d-%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Database (CSV)", default, "CSV (*.csv)"
        )
        if not path:
            return
        try:
            n = self.db.export_csv(path)
            QMessageBox.information(self, "Export complete", f"Exported {n:,} rows to\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def _on_backup(self) -> None:
        default = datetime.now().strftime("processed_%Y-%m-%d_%H-%M-%S.db")
        path, _ = QFileDialog.getSaveFileName(
            self, "Backup Database", default, "SQLite DB (*.db)"
        )
        if not path:
            return
        try:
            self.db.backup(path)
            QMessageBox.information(self, "Backup complete", f"Backup saved to\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Backup failed", str(exc))

    def _on_maintenance(self) -> None:
        ans = QMessageBox.question(
            self,
            "Run maintenance?",
            "This will run VACUUM followed by ANALYZE on the SQLite database.\n"
            "It may take a few seconds. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        try:
            self.db.vacuum()
            self.db._conn.execute("ANALYZE")
            QMessageBox.information(self, "Maintenance complete", "VACUUM + ANALYZE finished.")
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Maintenance failed", str(exc))

    # ---------------- helpers ----------------
    @staticmethod
    def _fmt_int(n: int) -> str:
        return f"{int(n):,}"

    @staticmethod
    def _fmt_size(n: int) -> str:
        n = int(n)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
            n /= 1024.0
        return f"{n:.1f} TB"


# =========================================================================
# Main dashboard
# =========================================================================
class Dashboard(QMainWindow):
    log_line = Signal(str)

    def __init__(self, config: dict, selectors: dict, config_path: Path,
                 db: DatabaseService,
                 app_dir: Optional[Path] = None,
                 resource_dir: Optional[Path] = None,
                 credentials_path: Optional[Path] = None,
                 crash_store: Optional[CrashStateStore] = None,
                 previous_state: Optional[CrashState] = None) -> None:
        super().__init__()
        self.setWindowTitle("Bonus Reload Automation")
        self.resize(1280, 780)
        self.setStyleSheet(DARK_STYLE)

        self.config = config
        self.selectors = selectors
        self.config_path = config_path

        # v1.2 hardening plumbing (optional so existing tests that
        # construct Dashboard(config, selectors, config_path, db) keep
        # passing without changes).
        self.app_dir: Path = Path(app_dir) if app_dir else Path.cwd()
        self.resource_dir: Path = Path(resource_dir) if resource_dir else self.app_dir
        self.credentials_path: Path = (
            Path(credentials_path)
            if credentials_path
            else self.app_dir / config.get("google_credentials", "credentials/service_account.json")
        )
        self.crash_store: Optional[CrashStateStore] = crash_store
        self.previous_state: Optional[CrashState] = previous_state

        self.logger = AppLogger.get()
        self.logger.add_listener(lambda line: self.log_line.emit(line))
        self.log_line.connect(self._append_log)

        # Core services
        self.db = db
        self.cache = MemoryCache()
        self.sheet = SheetService(config["google_credentials"], config)
        self.validator = Validator(config["bonus_rules"])
        self.queue: Optional[QueueManager] = None
        self.panel = PanelService(config, selectors)
        # Manual persistence is lazy so an unavailable additive schema can
        # never prevent the frozen AUTO application from starting.
        self.manual_repository: Optional[ManualAdjustRepository] = None
        self.manual_loader: Optional[ManualAdjustLoader] = None
        self.manual_state = ManualPreviewState()

        # Timers
        self.manual_timer = QTimer(self)
        self.manual_timer.timeout.connect(self._reload_manual_list)

        self.worker_timer = QTimer(self)
        self.worker_timer.setSingleShot(False)
        self.worker_timer.timeout.connect(self._worker_step)

        # Periodic panel-alive poll: detects operator closing the browser
        # window (X) so Panel Status can auto-return to "Closed".
        self.panel_timer = QTimer(self)
        self.panel_timer.setInterval(2000)
        self.panel_timer.timeout.connect(self._poll_panel_alive)
        self.panel_timer.start()

        self.state = "idle"     # idle | running | monitoring | stopping
        self.stop_requested = False
        self.current_item: Optional[QueueItem] = None
        self._manual_refresh_pending = False
        self._manual_last_refresh_ts: float = 0.0
        self._panel_was_open = False

        # --- Session metrics (reset on START) ---
        self._run_start_ts: Optional[float] = None
        self._processed_count: int = 0
        self._bonus_paid_total: int = 0
        self._submit_duration_sum: float = 0.0
        self._submit_duration_count: int = 0
        self._queue_start_size: int = 0     # ready count when this queue was loaded
        self._session_total: int = 0        # cumulative READY seen across refills
        self._current_submit_ts: Optional[float] = None

        # Continuous monitoring
        self._monitoring_interval: int = int(
            self.config.get("monitoring_interval_sec", 10)
        )
        self._next_refresh_ts: Optional[float] = None

        # 1 s timer for elapsed / rate / ETA updates
        self.metrics_timer = QTimer(self)
        self.metrics_timer.setInterval(1000)
        self.metrics_timer.timeout.connect(self._refresh_metrics)

        # v1.2 hardening infrastructure ----------------------------------
        hardening_cfg = self.config.get("hardening", {}) or {}
        retry_ladder = tuple(
            int(x) for x in hardening_cfg.get("google_retry_ladder_sec", [5, 10, 20, 40, 60])
        )
        self._retry_ladder: tuple = retry_ladder

        # Maintenance service — shared with MaintenanceCenter dialog.
        self.maintenance_service = MaintenanceService(
            db=self.db,
            logs_dir=self.app_dir / "logs",
            screenshots_dir=self.app_dir / "screenshots",
        )

        # Health monitor. Callables read live state from `self`; the
        # monitor itself never mutates anything.
        thresholds_cfg = hardening_cfg.get("leak_thresholds", {}) or {}
        self.health_monitor = HealthMonitor(
            db_probe=lambda: self.db.is_open(),
            google_probe=lambda: self.sheet.is_connected,
            panel_probe=lambda: 1 if self.panel.is_alive() else 0,
            worker_state_probe=lambda: self.state,
            queue_size_probe=lambda: self.queue.ready_count() if self.queue else 0,
            qtimer_count_probe=self._count_active_qtimers,
            thresholds=LeakThresholds(
                memory_mb_max=float(thresholds_cfg.get("memory_mb_max", 800)),
                thread_count_max=int(thresholds_cfg.get("thread_count_max", 60)),
                handle_count_max=int(thresholds_cfg.get("handle_count_max", 800)),
                browser_contexts_max=int(thresholds_cfg.get("browser_contexts_max", 1)),
                qtimer_count_max=int(thresholds_cfg.get("qtimer_count_max", 20)),
            ),
        )

        # Watchdog QTimer (B-3, B-5). Runs on the Qt event loop — no
        # background thread.
        self.watchdog_timer = QTimer(self)
        watchdog_interval = int(hardening_cfg.get("watchdog_interval_sec", 30)) * 1000
        self.watchdog_timer.setInterval(max(5_000, watchdog_interval))
        self.watchdog_timer.timeout.connect(self._watchdog_tick)
        self.watchdog_timer.start()
        self._last_watchdog_warnings: list[str] = []

        self._auto_recover_panel: bool = bool(hardening_cfg.get("auto_recover_panel", True))

        self._build_ui()
        self._refresh_stats()
        # v1.2 B-8: restore last URL / window geometry.
        self._restore_previous_state()

    # ---------------------------------------------------------------- v1.2 helpers
    def _count_active_qtimers(self) -> int:
        """Count QTimer instances owned by this window. Cheap; no reflection."""
        return sum(
            1 for name in dir(self)
            if isinstance(getattr(self, name, None), QTimer)
        )

    def _watchdog_tick(self) -> None:
        """Category B watchdog step (B-3, B-5). Never raises, logs warnings
        only on state changes to keep the log clean."""
        snap = safe_run(
            self.health_monitor.snapshot,
            module="watchdog",
            recovery_action="skip snapshot",
            logger=self.logger,
        )
        if snap is None:
            return
        # De-dup warnings so a stable condition doesn't spam the log.
        new_warnings = [w for w in snap.warnings if w not in self._last_watchdog_warnings]
        for w in new_warnings:
            self.logger.warn(f"Watchdog: {w}")
        if not snap.warnings and self._last_watchdog_warnings:
            self.logger.info("Watchdog: all metrics back within thresholds")
        self._last_watchdog_warnings = list(snap.warnings)

        # B-2 auto-recover: panel was previously attached but is now
        # dead; try one gentle reopen using the persistent profile so
        # cookies survive.
        if (
            self._auto_recover_panel
            and self._panel_was_open
            and not self.panel.is_alive()
            and self.state in ("running", "monitoring")
        ):
            self._attempt_panel_recovery()

    def _attempt_panel_recovery(self) -> None:
        """B-2 — one attempt per watchdog tick to bring the browser back."""
        url = self.config.get("panel_url", "").strip()
        if not url:
            return
        self.logger.warn("Panel appears dead — attempting auto-recovery")
        try:
            self.panel.open_panel(url)
            # Re-attach so submit_deposit works again.
            self.panel.attach()
            self._set_dot(self.dot_panel, "ok")
            self.txt_panel.setText("Recovered")
            self._panel_was_open = True
            self.logger.info("Panel auto-recovered (persistent profile reused)")
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"Panel auto-recovery failed: {exc}")

    def _restore_previous_state(self) -> None:
        """B-8: reinstate the last spreadsheet URL / window geometry
        after a clean or crash exit. Never automatically presses
        CONNECT SHEET — that stays operator-driven."""
        prev = self.previous_state
        if not prev:
            return
        try:
            if prev.spreadsheet_url and not self.url_input.text().strip():
                self.url_input.setText(prev.spreadsheet_url)
            if prev.window_geometry:
                try:
                    self.restoreGeometry(bytes.fromhex(prev.window_geometry))
                except Exception:
                    pass
            if prev.window_state:
                try:
                    self.restoreState(bytes.fromhex(prev.window_state))
                except Exception:
                    pass
        except Exception:
            pass

    def _persist_crash_state(self, *, clean_exit: bool) -> None:
        if not self.crash_store:
            return
        try:
            geom = self.saveGeometry().toHex().data().decode()
        except Exception:
            geom = ""
        try:
            state_bytes = self.saveState().toHex().data().decode()
        except Exception:
            state_bytes = ""
        overrides = dict(
            version=str(self.config.get("version", "")),
            spreadsheet_url=self.url_input.text().strip() if hasattr(self, "url_input") else "",
            monitoring_active=self.state == "monitoring",
            window_geometry=geom,
            window_state=state_bytes,
            last_panel_url=self.config.get("panel_url", ""),
        )
        if clean_exit:
            self.crash_store.mark_clean_exit(**overrides)
        else:
            self.crash_store.mark_dirty(**overrides)

    # ---------------------------------------------------------------- build
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        # ----- Top bar
        top = QHBoxLayout()
        title = QLabel("BONUS RELOAD")
        title.setStyleSheet("color:#F5B301; font-size:22px; font-weight:800; letter-spacing:6px;")
        subtitle = QLabel("automation console")
        subtitle.setStyleSheet("color:#6E7180; font-size:11px; letter-spacing:4px; margin-left:12px;")

        self.top_sync = QLabel("Last sync: never")
        self.top_sync.setObjectName("top-last-sync")
        self.top_sync.setStyleSheet("color:#8A8C99; font-size:11px; letter-spacing:1px;")
        self.top_version = QLabel(self.config.get("version", "v1.0.0"))
        self.top_version.setObjectName("top-version")
        self.top_version.setStyleSheet(
            "color:#F5B301; font-size:11px; letter-spacing:2px; font-weight:700; "
            "padding:4px 10px; border:1px solid #F5B30155; border-radius:6px; margin-left:12px;"
        )

        settings_btn = QPushButton("SETTINGS")
        settings_btn.setObjectName("settings-btn")
        settings_btn.clicked.connect(self._open_settings)

        db_btn = QPushButton("DATABASE")
        db_btn.setObjectName("database-btn")
        db_btn.clicked.connect(self._open_database)

        maint_btn = QPushButton("MAINTENANCE")
        maint_btn.setObjectName("maintenance-btn")
        maint_btn.clicked.connect(self._open_maintenance_center)

        top.addWidget(title)
        top.addWidget(subtitle)
        self.mode_selector = QComboBox()
        self.mode_selector.setObjectName("operating-mode-selector")
        self.mode_selector.addItems([mode.value for mode in OperatingMode])
        self.mode_selector.currentTextChanged.connect(self._on_mode_selected)
        top.addWidget(self.mode_selector)
        top.addStretch(1)
        top.addWidget(self.top_sync)
        top.addWidget(self.top_version)
        top.addWidget(db_btn)
        top.addWidget(maint_btn)
        top.addWidget(settings_btn)
        root.addLayout(top)

        # ----- Connection row
        conn_card = QFrame()
        conn_card.setObjectName("Card")
        conn_row = QHBoxLayout(conn_card)
        conn_row.setContentsMargins(16, 12, 16, 12)
        conn_row.setSpacing(12)

        self.url_input = QLineEdit()
        self.url_input.setObjectName("spreadsheet-url-input")
        self.url_input.setPlaceholderText("Paste Google Spreadsheet URL")
        self.url_input.setText(self.config.get("spreadsheet_url", ""))
        self.connect_btn = QPushButton("CONNECT SHEET")
        self.connect_btn.setObjectName("connect-sheet-btn")
        self.connect_btn.clicked.connect(self._on_connect)

        conn_row.addWidget(self.url_input, 1)
        conn_row.addWidget(self.connect_btn)
        root.addWidget(conn_card)

        # ----- Splitter (left status/buttons | right live log)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # LEFT
        left = QFrame()
        left.setObjectName("Card")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(20, 20, 20, 20)
        left_layout.setSpacing(16)

        left_layout.addWidget(self._section_title("Status"))
        left_layout.addLayout(self._build_status_grid())

        left_layout.addWidget(self._divider())
        left_layout.addWidget(self._section_title("Actions"))
        left_layout.addLayout(self._build_actions())

        left_layout.addWidget(self._divider())
        left_layout.addWidget(self._section_title("Current Processing"))
        left_layout.addWidget(self._build_current_card())

        left_layout.addWidget(self._section_title("Progress"))
        left_layout.addWidget(self._build_progress_card())

        left_layout.addWidget(self._section_title("Queue Summary"))
        left_layout.addWidget(self._build_queue_summary_card())

        left_layout.addWidget(self._divider())
        left_layout.addWidget(self._section_title("Statistics"))
        left_layout.addLayout(self._build_stats_grid())

        left_layout.addStretch(1)

        # RIGHT
        right = QFrame()
        right.setObjectName("Card")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(20, 20, 20, 20)
        right_layout.setSpacing(10)

        header = QHBoxLayout()
        header.addWidget(self._section_title("Live Log"))
        header.addStretch(1)
        self.export_txt = QPushButton("EXPORT .TXT")
        self.export_txt.setObjectName("export-txt-btn")
        self.export_csv = QPushButton("EXPORT .CSV")
        self.export_csv.setObjectName("export-csv-btn")
        self.export_txt.clicked.connect(lambda: self._export_log("txt"))
        self.export_csv.clicked.connect(lambda: self._export_log("csv"))
        header.addWidget(self.export_txt)
        header.addWidget(self.export_csv)
        right_layout.addLayout(header)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("live-log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(
            int(self.config.get("live_log_max_lines", 500))
        )
        right_layout.addWidget(self.log_view, 1)

        splitter.addWidget(self._wrap_scroll(left))
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        self.auto_view = splitter
        self.manual_view = ManualAdjustView(self)
        self.manual_view.load_requested.connect(self._on_manual_load)
        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self.auto_view)
        self.mode_stack.addWidget(self.manual_view)
        root.addWidget(self.mode_stack, 1)

    def _on_mode_selected(self, text: str) -> None:
        requested = OperatingMode(text)
        allowed, message = self.manual_state.select_mode(
            requested,
            self.state,
            self._ensure_manual_backend if requested is OperatingMode.MANUAL else None,
        )
        if not allowed:
            self.mode_selector.blockSignals(True)
            self.mode_selector.setCurrentText(OperatingMode.AUTO.value)
            self.mode_selector.blockSignals(False)
            if message.startswith("Full Manual Adjust is unavailable:"):
                self.logger.error(f"[MANUAL] Backend initialization failed: {message}")
            QMessageBox.warning(self, "Mode switch blocked", message)
            return
        manual = requested is OperatingMode.MANUAL
        self.mode_stack.setCurrentWidget(self.manual_view if manual else self.auto_view)
        if manual:
            # AUTO business timers must be dormant in the isolated Manual view.
            self.manual_timer.stop()
            self.worker_timer.stop()
            self.metrics_timer.stop()
            self.manual_view.set_sheet_connected(self.sheet.is_connected)
            current = self.manual_state.current_preview(self.manual_repository)
            if current:
                self.manual_view.display_preview(*current)
            self.logger.info("[MANUAL] Mode selected")
        elif self.sheet.is_connected:
            interval = int(self.config.get("manual_reload_interval_sec", 90)) * 1000
            self.manual_timer.start(interval)

    def _ensure_manual_backend(self) -> None:
        """Initialize the additive Manual backend once, isolated from AUTO."""
        if self.manual_repository is not None and self.manual_loader is not None:
            return
        repository = None
        try:
            repository = ManualAdjustRepository(self.db.path)
            repository.initialize_schema()
            loader = ManualAdjustLoader(self.sheet, repository)
        except Exception:
            if repository is not None:
                try:
                    repository.close()
                except Exception:
                    pass
            raise
        self.manual_repository = repository
        self.manual_loader = loader

    def _on_manual_load(self) -> None:
        if self.manual_state.mode is not OperatingMode.MANUAL:
            return
        if not self.sheet.is_connected:
            self.manual_view.show_error("Connect the Google Sheet before loading Manual data.")
            return
        self.logger.info("[MANUAL] Loading snapshot")
        try:
            if self.manual_repository is None or self.manual_loader is None:
                raise RuntimeError("Manual backend is not initialized")
            cycle, summary, rows = self.manual_state.load_snapshot(
                self.manual_loader, self.manual_repository
            )
        except Exception as exc:
            self.logger.error(f"[MANUAL] Snapshot load failed: {exc}")
            self.manual_view.show_error(str(exc))
            return
        self.manual_view.display_preview(cycle, summary, rows)
        self.logger.info(f"[MANUAL] Snapshot frozen — cycle {cycle['cycle_id']}")
        self.logger.info(
            f"[MANUAL] {summary.ready} READY / {summary.duplicates} DUPLICATE / "
            f"{summary.invalid} INVALID"
        )

    def _wrap_scroll(self, widget: QWidget) -> QScrollArea:
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(widget)
        return sc

    # ---------------- helpers ----------------
    def _section_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("SectionTitle")
        return lbl

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#23252E; background:#23252E; max-height:1px;")
        return line

    def _build_status_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)

        self.dot_sheet = QLabel("●"); self.dot_sheet.setObjectName("DotIdle")
        self.txt_sheet = QLabel("Not connected")
        self.dot_panel = QLabel("●"); self.dot_panel.setObjectName("DotIdle")
        self.txt_panel = QLabel("Closed")
        self.dot_bot = QLabel("●"); self.dot_bot.setObjectName("DotIdle")
        self.txt_bot = QLabel("Idle")
        self.txt_sync = QLabel("Never")
        self.txt_current = QLabel("—")
        self.txt_queue = QLabel("0")

        rows = [
            ("Google Sheets", self.dot_sheet, self.txt_sheet, "sheet-status"),
            ("Panel", self.dot_panel, self.txt_panel, "panel-status"),
            ("Bot", self.dot_bot, self.txt_bot, "bot-status"),
        ]
        for i, (label, dot, text, tid) in enumerate(rows):
            l = QLabel(label); l.setObjectName("StatLabel")
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(dot)
            row.addWidget(text)
            row.addStretch(1)
            wrapper = QWidget(); wrapper.setLayout(row); wrapper.setObjectName(tid)
            grid.addWidget(l, i, 0)
            grid.addWidget(wrapper, i, 1)

        misc = [
            ("Last Sheet Sync", self.txt_sync, "last-sync"),
        ]
        for j, (label, w, tid) in enumerate(misc, start=len(rows)):
            l = QLabel(label); l.setObjectName("StatLabel")
            w.setObjectName(tid)
            grid.addWidget(l, j, 0)
            grid.addWidget(w, j, 1)

        return grid

    def _build_actions(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        self.btn_open_panel = QPushButton("OPEN PANEL")
        self.btn_open_panel.setObjectName("open-panel-btn")
        self.btn_ready = QPushButton("READY")
        self.btn_ready.setObjectName("ready-btn")
        self.btn_refresh = QPushButton("REFRESH QUEUE")
        self.btn_refresh.setObjectName("refresh-queue-btn")
        self.btn_preview = QPushButton("PREVIEW QUEUE")
        self.btn_preview.setObjectName("preview-queue-btn")
        self.btn_start = QPushButton("START")
        self.btn_start.setObjectName("start-btn")
        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setObjectName("stop-btn")

        for b in (self.btn_open_panel, self.btn_ready, self.btn_refresh, self.btn_preview):
            b.setMinimumHeight(38)
        self.btn_start.setMinimumHeight(44)
        self.btn_stop.setMinimumHeight(44)

        self.btn_open_panel.clicked.connect(self._on_open_panel)
        self.btn_ready.clicked.connect(self._on_ready)
        self.btn_refresh.clicked.connect(self._on_refresh_queue)
        self.btn_preview.clicked.connect(self._on_preview)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)

        self.btn_ready.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.btn_preview.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)

        grid.addWidget(self.btn_open_panel, 0, 0)
        grid.addWidget(self.btn_ready,      0, 1)
        grid.addWidget(self.btn_refresh,    1, 0)
        grid.addWidget(self.btn_preview,    1, 1)
        grid.addWidget(self.btn_start,      2, 0)
        grid.addWidget(self.btn_stop,       2, 1)
        return grid

    def _build_stats_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        # 8 KPIs, 2 columns × 4 rows.
        stat_style = "color:#F5B301; font-size:20px; font-weight:800;"
        self.stat_pending    = QLabel("0"); self.stat_pending.setStyleSheet(stat_style)
        self.stat_processed  = QLabel("0"); self.stat_processed.setStyleSheet(stat_style)
        self.stat_skipped    = QLabel("0"); self.stat_skipped.setStyleSheet(stat_style)
        self.stat_failed     = QLabel("0"); self.stat_failed.setStyleSheet(stat_style)
        self.stat_bonus_paid = QLabel("0"); self.stat_bonus_paid.setStyleSheet(stat_style)
        self.stat_rate       = QLabel("0.0"); self.stat_rate.setStyleSheet(stat_style)
        self.stat_avg_submit = QLabel("0.0s"); self.stat_avg_submit.setStyleSheet(stat_style)
        self.stat_elapsed    = QLabel("00:00"); self.stat_elapsed.setStyleSheet(stat_style)

        pairs = [
            ("Queue Ready",      self.stat_pending,    "stat-pending"),
            ("Processed",        self.stat_processed,  "stat-processed"),
            ("Skipped",          self.stat_skipped,    "stat-skipped"),
            ("Failed",           self.stat_failed,     "stat-failed"),
            ("Today's Bonus",    self.stat_bonus_paid, "stat-bonus-paid"),
            ("Adj / Min",        self.stat_rate,       "stat-rate"),
            ("Avg Submit",       self.stat_avg_submit, "stat-avg-submit"),
            ("Elapsed",          self.stat_elapsed,    "stat-elapsed"),
        ]
        for i, (label, value, tid) in enumerate(pairs):
            box = QFrame(); box.setObjectName("SubCard")
            inner = QVBoxLayout(box)
            inner.setContentsMargins(12, 10, 12, 10)
            inner.setSpacing(2)
            value.setObjectName(tid)
            cap = QLabel(label); cap.setObjectName("KpiCaption")
            inner.addWidget(value)
            inner.addWidget(cap)
            grid.addWidget(box, i // 2, i % 2)
        return grid

    # ---------------- Phase 2 cards ----------------
    def _build_current_card(self) -> QWidget:
        card = QFrame(); card.setObjectName("SubCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        row1 = QHBoxLayout(); row1.setSpacing(10)
        self.cur_user = QLabel("—")
        self.cur_user.setObjectName("current-user")
        self.cur_user.setStyleSheet("color:#F5B301; font-size:22px; font-weight:800; letter-spacing:1px;")
        cap_user = QLabel("USER"); cap_user.setObjectName("KpiCaption")
        col_u = QVBoxLayout(); col_u.setSpacing(0); col_u.addWidget(self.cur_user); col_u.addWidget(cap_user)
        row1.addLayout(col_u, 2)

        self.cur_status = QLabel("Idle")
        self.cur_status.setObjectName("current-status")
        self.cur_status.setStyleSheet(
            "color:#F5B301; font-size:11px; font-weight:700; letter-spacing:2px; "
            "padding:3px 10px; border:1px solid #F5B30155; border-radius:4px;"
        )
        self.cur_status.setAlignment(Qt.AlignCenter)
        row1.addStretch(1)
        row1.addWidget(self.cur_status)
        v.addLayout(row1)

        row2 = QHBoxLayout(); row2.setSpacing(24)
        self.cur_deposit = QLabel("0")
        self.cur_deposit.setObjectName("current-deposit")
        self.cur_deposit.setStyleSheet("color:#ECEBE4; font-size:15px; font-weight:700;")
        self.cur_bonus = QLabel("0")
        self.cur_bonus.setObjectName("current-bonus")
        self.cur_bonus.setStyleSheet("color:#ECEBE4; font-size:15px; font-weight:700;")
        for text_lbl, cap_text in ((self.cur_deposit, "DEPOSIT"), (self.cur_bonus, "GRANTED BONUS")):
            c = QVBoxLayout(); c.setSpacing(0)
            c.addWidget(text_lbl)
            cap = QLabel(cap_text); cap.setObjectName("KpiCaption")
            c.addWidget(cap)
            row2.addLayout(c)
        row2.addStretch(1)
        v.addLayout(row2)

        # Keep old aliases so existing worker code that touches txt_current
        # / txt_queue keeps working.
        self.txt_current = self.cur_user
        return card

    def _build_progress_card(self) -> QWidget:
        card = QFrame(); card.setObjectName("SubCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        head = QHBoxLayout()
        self.prog_label = QLabel("Processed 0 / 0")
        self.prog_label.setObjectName("progress-label")
        self.prog_label.setStyleSheet("color:#ECEBE4; font-weight:700;")
        self.eta_label = QLabel("ETA 00:00")
        self.eta_label.setObjectName("eta-label")
        self.eta_label.setStyleSheet("color:#8A8C99; font-size:11px; letter-spacing:1px;")
        head.addWidget(self.prog_label)
        head.addStretch(1)
        head.addWidget(self.eta_label)
        v.addLayout(head)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progress-bar")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        v.addWidget(self.progress_bar)

        # Legacy alias (kept for old code paths).
        self.txt_queue = QLabel("0")
        return card

    def _build_queue_summary_card(self) -> QWidget:
        card = QFrame(); card.setObjectName("SubCard")
        g = QGridLayout(card)
        g.setContentsMargins(14, 10, 14, 10)
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(4)

        self.qs_ready   = QLabel("0"); self.qs_ready.setObjectName("qs-ready")
        self.qs_manual  = QLabel("0"); self.qs_manual.setObjectName("qs-manual")
        self.qs_invalid = QLabel("0"); self.qs_invalid.setObjectName("qs-invalid")
        self.qs_limit   = QLabel("0"); self.qs_limit.setObjectName("qs-limit")

        chips = [
            ("READY",        self.qs_ready,   "#4ADE80"),
            ("MANUAL BONUS", self.qs_manual,  "#3B82F6"),
            ("INVALID",      self.qs_invalid, "#EF4444"),
            ("LIMIT",        self.qs_limit,   "#F5B301"),
        ]
        for i, (label, value, color) in enumerate(chips):
            cap = QLabel(label)
            cap.setStyleSheet(f"color:{color}; font-size:10px; letter-spacing:2px; font-weight:700;")
            value.setStyleSheet("color:#ECEBE4; font-size:18px; font-weight:800;")
            g.addWidget(cap,   i // 2, (i % 2) * 2)
            g.addWidget(value, i // 2, (i % 2) * 2 + 1)
        return card

    # ---------------- log & stats ----------------
    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _refresh_stats(self) -> None:
        if self.queue is None:
            self.stat_pending.setText("0")
            self.stat_processed.setText("0")
            self.stat_skipped.setText("0")
            self.stat_failed.setText("0")
            self.qs_ready.setText("0")
            self.qs_manual.setText("0")
            self.qs_invalid.setText("0")
            self.qs_limit.setText("0")
            self.prog_label.setText("Processed 0 / 0")
            self.progress_bar.setRange(0, 1); self.progress_bar.setValue(0)
            self.eta_label.setText("ETA 00:00")
            return

        s = self.queue.stats()
        remaining = self.queue.ready_count()
        processed_in_queue = s.processed + s.failed

        self.stat_pending.setText(str(remaining))
        self.stat_processed.setText(str(self._processed_count))
        self.stat_skipped.setText(str(s.skipped))
        self.stat_failed.setText(str(s.failed))

        # Queue summary card
        self.qs_ready.setText(str(s.ready))
        self.qs_manual.setText(str(s.manual))
        self.qs_invalid.setText(str(s.invalid))
        self.qs_limit.setText(str(s.limit))

        # Progress card
        total = max(s.ready, 1)
        done = min(processed_in_queue, s.ready)
        self.prog_label.setText(f"Processed {done} / {s.ready}")
        self.progress_bar.setRange(0, max(s.ready, 1))
        self.progress_bar.setValue(done)

        # ETA (uses avg submit time when we have one). Skipped while we're
        # in monitoring mode — that label shows the countdown instead.
        if self.state == "monitoring":
            self._update_countdown_label()
        else:
            avg = self._avg_submit_seconds()
            if remaining > 0 and avg > 0:
                eta_seconds = int(remaining * avg)
                self.eta_label.setText(f"ETA {self._fmt_duration(eta_seconds)}")
            else:
                self.eta_label.setText("ETA 00:00")

    # ---------------- session metrics ----------------
    def _reset_session(self) -> None:
        self._run_start_ts = time.monotonic()
        self._processed_count = 0
        self._bonus_paid_total = 0
        self._submit_duration_sum = 0.0
        self._submit_duration_count = 0
        self._current_submit_ts = None
        self.stat_bonus_paid.setText("0")
        self.stat_rate.setText("0.0")
        self.stat_avg_submit.setText("0.0s")
        self.stat_elapsed.setText("00:00")

    def _avg_submit_seconds(self) -> float:
        if self._submit_duration_count == 0:
            return 0.0
        return self._submit_duration_sum / self._submit_duration_count

    def _refresh_metrics(self) -> None:
        """Fires every 1 s while the worker is running (also once on stop)."""
        if self._run_start_ts is None:
            return
        elapsed = time.monotonic() - self._run_start_ts
        self.stat_elapsed.setText(self._fmt_duration(int(elapsed)))
        rate = (self._processed_count / (elapsed / 60.0)) if elapsed >= 1.0 else 0.0
        self.stat_rate.setText(f"{rate:.1f}")
        self.stat_avg_submit.setText(f"{self._avg_submit_seconds():.1f}s")
        self.stat_bonus_paid.setText(f"{self._bonus_paid_total:,}")
        if self.state == "monitoring":
            self._update_countdown_label()

    @staticmethod
    def _fmt_duration(sec: int) -> str:
        sec = max(0, int(sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    # ---------------- indicator setters ----------------
    def _set_dot(self, dot: QLabel, kind: str) -> None:
        dot.setObjectName({"ok":"DotOk","warn":"DotWarn","err":"DotErr","idle":"DotIdle"}.get(kind, "DotIdle"))
        dot.style().unpolish(dot); dot.style().polish(dot)

    def _stamp_sync(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        short = datetime.now().strftime("%H:%M:%S")
        self.txt_sync.setText(short)
        self.top_sync.setText(f"Last sync: {now}")

    # =============================================================
    # BUTTON HANDLERS
    # =============================================================
    def _on_connect(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Paste the Google Spreadsheet URL first.")
            return

        # v1.2 B-1: wrap the initial connect in the retry ladder so a
        # transient Google outage no longer forces the operator to click
        # CONNECT SHEET again. Each retry emits a WARN with the delay.
        def _do_connect():
            info = self.sheet.connect(url)
            if not info.ok:
                # Raise so the ladder retries. `RuntimeError` is caught
                # by the ladder's default `retry_on=(Exception,)`.
                raise RuntimeError(info.error or "Connection failed")
            return info

        def _on_retry(attempt: int, delay: int, exc: BaseException) -> None:
            self.logger.warn(
                f"Google connect attempt {attempt} failed ({exc}); retrying in {delay}s"
            )

        try:
            info = retry_with_ladder(
                _do_connect,
                ladder=self._retry_ladder,
                on_retry=_on_retry,
            )
        except RetryExhausted as exc:
            self._set_dot(self.dot_sheet, "err")
            self.txt_sheet.setText("Error")
            self.logger.error(f"Connect failed after retry ladder: {exc.last_error}")
            QMessageBox.critical(self, "Connection failed", str(exc.last_error))
            self.btn_start.setEnabled(False)
            self.btn_refresh.setEnabled(False)
            self.btn_preview.setEnabled(False)
            self.queue = None
            return

        self._set_dot(self.dot_sheet, "ok")
        self.txt_sheet.setText(info.title or "Connected")
        self._stamp_sync()
        self.logger.info(f"Connected: {info.title} ({info.spreadsheet_id})")

        # Persist URL for convenience
        self.config["spreadsheet_url"] = url
        try:
            self.config_path.write_text(json.dumps(self.config, indent=4), encoding="utf-8")
        except Exception:
            pass

        # Build queue backed by the SQLite database.
        self.queue = QueueManager(
            self.sheet, self.cache, self.validator, self.db,
            self.config.get("batch_size", 100),
        )
        # Seed daily-bonus map from SQLite (single source of truth).
        self.cache.set_daily_bonus(self.db.daily_bonus_map())
        try:
            manual = self.sheet.read_manual_set()
            self.cache.set_manual(manual)
            self.logger.info(f"Manual bonus list: {len(manual)} users")
        except Exception as exc:
            self.logger.error(f"Failed reading manual list: {exc}")

        interval = int(self.config.get("manual_reload_interval_sec", 90)) * 1000
        self.manual_timer.start(interval)

        self.btn_preview.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.btn_ready.setEnabled(self.panel.is_open)
        self.btn_start.setEnabled(self.panel.is_attached)
        self._refresh_stats()
        self.manual_view.set_sheet_connected(True)
        if self.manual_state.mode is OperatingMode.MANUAL:
            self.manual_timer.stop()

    def _reload_manual_list(self) -> None:
        if self.manual_state.mode is OperatingMode.MANUAL:
            return
        if not self.sheet.is_connected:
            return
        # Defer while worker is actively processing to avoid extra Google API
        # requests during a run. Refresh will fire once the queue is drained.
        if self.state == "running":
            self._manual_refresh_pending = True
            return
        try:
            manual = self.sheet.read_manual_set()
            self.cache.set_manual(manual)
            self._stamp_sync()
            self.logger.info(f"Manual list refreshed ({len(manual)})")
        except Exception as exc:
            self.logger.error(f"Manual list refresh failed: {exc}")

    # BUG-012 helper — TTL-throttled fresh read used right before every
    # adjustment. Cheap enough that we can afford one API call per submit
    # in the worst case, but usually amortised by the TTL below.
    _MANUAL_FRESH_TTL_SEC = 2.0

    def _refresh_manual_list_now(self) -> None:
        """Force a fresh MANUAL BONUS RELOAD read if the last one is older
        than `_MANUAL_FRESH_TTL_SEC` seconds. Silent on transient failures
        — we keep processing with whatever cache we have, which never
        loses safety because the queue-refill path already primed it.
        """
        if not self.sheet.is_connected:
            return
        now = time.monotonic()
        if now - getattr(self, "_manual_last_refresh_ts", 0.0) < self._MANUAL_FRESH_TTL_SEC:
            return
        try:
            manual = self.sheet.read_manual_set()
            self.cache.set_manual(manual)
            self._manual_last_refresh_ts = now
        except Exception as exc:
            # Do NOT crash the worker on a transient Google API blip; the
            # queue-refill path will retry on the next monitoring cycle.
            self.logger.warn(f"Fresh manual-list fetch failed: {exc}")

    def _on_open_panel(self) -> None:
        url = self.config.get("panel_url", "").strip()
        if not url:
            QMessageBox.warning(
                self,
                "Panel URL missing",
                "Set the Panel URL in Settings before opening the panel.",
            )
            return
        try:
            self.panel.open_panel(url)
            self._set_dot(self.dot_panel, "warn")
            self.txt_panel.setText("Awaiting login")
            self.btn_ready.setEnabled(True)
            self._panel_was_open = True
            self.logger.info(f"Panel opened: {url}")
        except Exception as exc:
            self._set_dot(self.dot_panel, "err")
            self.txt_panel.setText("Error")
            self.logger.error(f"Open panel failed: {exc}")
            QMessageBox.critical(self, "Panel error", str(exc))

    def _on_ready(self) -> None:
        try:
            self.panel.attach()
            self._set_dot(self.dot_panel, "ok")
            self.txt_panel.setText("Attached")
            self._panel_was_open = True
            self.logger.info("Panel attached")
            self.btn_start.setEnabled(self.queue is not None)
        except Exception as exc:
            self.logger.error(f"Attach failed: {exc}")
            QMessageBox.critical(self, "Attach error", str(exc))

    def _poll_panel_alive(self) -> None:
        """Detects operator closing the Chromium window (X button).
        Idempotent; runs every 2 s and does nothing if the state is unchanged."""
        alive = self.panel.is_alive()
        if self._panel_was_open and not alive:
            self._panel_was_open = False
            # Only announce if the state machine wasn't already handling this.
            if self.state != "running":
                self._on_panel_lost()
                if self.queue is not None:
                    self.btn_start.setEnabled(False)
        elif alive and not self._panel_was_open:
            self._panel_was_open = True

    def _on_refresh_queue(self) -> None:
        """Re-read pending rows from Google Sheets and rebuild the preview
        queue (no dialog). Disabled while the worker is running to keep API
        traffic minimal."""
        if self.manual_state.mode is OperatingMode.MANUAL or self.queue is None:
            return
        if self.state == "running":
            QMessageBox.information(
                self,
                "Worker is running",
                "Stop the worker before refreshing the queue.",
            )
            return
        try:
            stats = self.queue.refill()
            self._stamp_sync()
            self._log_queue_summary(stats)
        except Exception as exc:
            self.logger.error(f"Refresh queue failed: {exc}")
            QMessageBox.critical(self, "Sheet read error", str(exc))
            return
        self._refresh_stats()

    def _on_preview(self) -> None:
        """Show the currently-loaded queue. If nothing has been loaded yet,
        do one refill so the operator isn't looking at an empty dialog."""
        if self.manual_state.mode is OperatingMode.MANUAL or self.queue is None:
            return
        if not self.queue.items():
            try:
                stats = self.queue.refill()
                self._stamp_sync()
                self._log_queue_summary(stats)
            except Exception as exc:
                self.logger.error(f"Read pending failed: {exc}")
                QMessageBox.critical(self, "Sheet read error", str(exc))
                return
        dlg = PreviewDialog(self.queue.items(), self)
        dlg.exec()
        self._refresh_stats()

    def _on_start(self) -> None:
        if self.manual_state.mode is OperatingMode.MANUAL:
            return
        if self.state in ("running", "monitoring"):
            return
        if self.queue is None or not self.panel.is_attached:
            QMessageBox.warning(self, "Not ready", "Connect the sheet and attach the panel first.")
            return

        # Try to fill the queue once so the operator sees something happening.
        if self.queue.is_empty():
            try:
                stats = self.queue.refill()
                self._log_queue_summary(stats)
                self._stamp_sync()
            except Exception as exc:
                self.logger.error(f"Read pending failed: {exc}")
                return

        self.state = "running"
        self.stop_requested = False
        self._set_dot(self.dot_bot, "ok")
        self.txt_bot.setText("Running")
        self.btn_start.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._reset_session()
        self.metrics_timer.start()
        self.logger.info("Worker started")

        # If refill produced zero READY items, jump straight to monitoring so
        # the operator doesn't need to press START again.
        if self.queue.ready_count() == 0:
            self._enter_monitoring(reason="No READY yet")

        # Fast tick — the panel submit itself is what paces us during
        # processing, and 500 ms is enough to keep the monitoring countdown
        # visually smooth without wasting CPU.
        self.worker_timer.start(500)

    def _on_stop(self) -> None:
        if self.state not in ("running", "monitoring"):
            return
        self.stop_requested = True
        # If we're in the middle of monitoring (nothing to submit), we can
        # finalise immediately. Otherwise, wait for the current transaction
        # to finish and let the worker step call _finalise_stop.
        if self.state == "monitoring":
            self._finalise_stop("STOP requested during monitoring")
            return
        self.state = "stopping"
        self._set_dot(self.dot_bot, "warn")
        self.txt_bot.setText("Stopping...")
        self.logger.info("STOP requested - finishing current transaction")

    # =============================================================
    # WORKER STEP
    # =============================================================
    def _worker_step(self) -> None:
        if self.manual_state.mode is OperatingMode.MANUAL:
            self.worker_timer.stop()
            return
        if self.queue is None:
            self.worker_timer.stop()
            return

        # If the operator closed the browser mid-run, bail cleanly.
        if not self.panel.is_alive():
            self._on_panel_lost()
            self._finalise_stop("Worker halted: browser closed")
            return

        item = self.queue.next_ready()

        # ----------------------------------------------------------
        # No READY items => Monitoring mode (Improvement #1)
        # ----------------------------------------------------------
        if item is None:
            if self.stop_requested:
                self._finalise_stop()
                return

            if self.state != "monitoring":
                self._enter_monitoring()
                return

            # Already monitoring: tick down the countdown, refresh on 0.
            self._tick_monitoring()
            return

        # ----------------------------------------------------------
        # We just got a READY item — leave monitoring if we were in it.
        # ----------------------------------------------------------
        if self.state == "monitoring":
            self._exit_monitoring()

        # ----------------------------------------------------------
        # FINAL PRE-SUBMIT VALIDATION SEQUENCE  (BUG-012 + BUG-015)
        #
        # The order below is contractual — do NOT reorder:
        #   1. SQLite duplicate validation.
        #   2. Latest Manual Bonus validation (fresh read from Google
        #      Sheets, TTL-throttled) — closes the queue-vs-manual race.
        #   3. Daily bonus validation, keyed by the ORIGINAL TRANSACTION
        #      DATE from Google Sheets (never `processed_at`).
        #   4. Submit adjustment.
        # ----------------------------------------------------------

        # (1) SQLite duplicate protection — belt-and-braces vs pre-filter.
        if self.db.has_tx(item.tx_id):
            self.queue.mark_processed(item, False)
            self.logger.info(f"{item.username}  tx {item.tx_id} already in DB - skipped")
            self._refresh_stats()
            if self.stop_requested:
                self._finalise_stop()
            return

        # (2) Latest Manual Bonus validation — BUG-012.
        # The operator may have added this user to MANUAL BONUS RELOAD
        # AFTER the queue was refilled. Re-read the list (short TTL) so a
        # concurrent addition wins before we submit.
        self._refresh_manual_list_now()
        manual_set = self.cache.manual_set()
        if item.username and str(item.username).strip() in manual_set:
            try:
                self.db.insert(
                    tx_id=item.tx_id, username=item.username,
                    amount=item.amount, bonus=0,
                    result="MANUAL BONUS", sheet_name=item.sheet_name,
                    timestamp=item.timestamp,
                )
            except Exception as exc:
                self.logger.error(f"{item.username}  DB insert failed: {exc}")
            self.queue.mark_processed(item, False)
            self.logger.info(
                f"{item.username}  MANUAL BONUS (fresh-check) - skipped"
            )
            self._refresh_stats()
            if self.stop_requested:
                self._finalise_stop()
            return

        # (3) Daily bonus validation — BUG-015.
        # Key by the TRANSACTION DATE parsed from the sheet cell, NOT by
        # `processed_at`.
        from core.timestamp_utils import parse_transaction_date
        from datetime import date as _date

        tx_date = parse_transaction_date(item.timestamp)
        if tx_date is None:
            self.logger.warn(
                f"{item.username}  unparseable timestamp "
                f"{item.timestamp!r} - falling back to today for daily bonus rule"
            )
            tx_date = _date.today()
        tx_date_iso = tx_date.isoformat()
        current = self.db.daily_bonus_for_transaction_date(
            item.username, tx_date_iso
        )
        revalidated = self.validator.validate(
            user_id=item.username,
            deposit_raw=item.amount,
            current_daily_bonus=current,
            manual_set=manual_set,
        )
        if revalidated.status != "READY":
            # Downgraded (usually to LIMIT) — record and skip.
            try:
                self.db.insert(
                    tx_id=item.tx_id, username=item.username,
                    amount=item.amount, bonus=0,
                    result=revalidated.status, sheet_name=item.sheet_name,
                    timestamp=item.timestamp,
                )
            except Exception as exc:
                self.logger.error(f"{item.username}  DB insert failed: {exc}")
            self.queue.mark_processed(item, False)
            self.logger.info(f"{item.username}  {revalidated.status} (revalidated) - skipped")
            self._refresh_stats()
            if self.stop_requested:
                self._finalise_stop()
            return

        # Apply the (possibly reduced) bonus.
        item.bonus = revalidated.bonus
        item.status = "READY"

        # --- Process READY item ---
        self.current_item = item
        self.cur_user.setText(item.username or "—")
        self.cur_deposit.setText(f"{item.amount:,}")
        self.cur_bonus.setText(f"{item.bonus:,}")
        self.cur_status.setText("PROCESSING")
        self.cur_status.setStyleSheet(
            "color:#F5B301; font-size:11px; font-weight:700; letter-spacing:2px; "
            "padding:3px 10px; border:1px solid #F5B30155; border-radius:4px;"
        )

        submit_start = time.monotonic()
        result = self.panel.submit_deposit(
            user_id=item.username,
            bonus=item.bonus,
            remark=self.config.get("remark", "BONUS RELOAD AUTO"),
        )
        submit_duration = time.monotonic() - submit_start
        self._submit_duration_sum += submit_duration
        self._submit_duration_count += 1

        if result.ok:
            # SQLite is now the single source of truth. INSERT OR IGNORE
            # gives us a final duplicate barrier via the PRIMARY KEY.
            try:
                self.db.insert(
                    tx_id=item.tx_id, username=item.username,
                    amount=item.amount, bonus=item.bonus,
                    result="SUCCESS", sheet_name=item.sheet_name,
                    timestamp=item.timestamp,
                )
                self.cache.add_bonus(item.username, item.bonus)
                self.queue.mark_processed(item, True)
                self._processed_count += 1
                self._bonus_paid_total += int(item.bonus)
                self.cur_status.setText("SUCCESS")
                self.cur_status.setStyleSheet(
                    "color:#4ADE80; font-size:11px; font-weight:700; letter-spacing:2px; "
                    "padding:3px 10px; border:1px solid #4ADE8055; border-radius:4px;"
                )
                self.logger.info(
                    f"{item.username}  Deposit {item.amount:,}  "
                    f"Bonus {item.bonus:,}  SUCCESS"
                )
            except Exception as exc:
                self.queue.mark_processed(item, False)
                self.logger.error(f"{item.username}  DB insert failed: {exc}")
        else:
            try:
                self.db.insert(
                    tx_id=item.tx_id, username=item.username,
                    amount=item.amount, bonus=0,
                    result="FAILED", sheet_name=item.sheet_name,
                    timestamp=item.timestamp,
                )
            except Exception as exc:
                self.logger.error(f"{item.username}  DB insert failed: {exc}")
            self.queue.mark_processed(item, False)
            self._save_screenshot(item.username)
            self.cur_status.setText("FAILED")
            self.cur_status.setStyleSheet(
                "color:#EF4444; font-size:11px; font-weight:700; letter-spacing:2px; "
                "padding:3px 10px; border:1px solid #EF444455; border-radius:4px;"
            )
            self.logger.error(f"{item.username}  FAILED  {result.detail}")

            if result.detail == "browser closed":
                self._on_panel_lost()
                self._finalise_stop("Worker halted: browser closed")
                return

        self.current_item = None
        self._refresh_metrics()
        self._refresh_stats()

        if self.stop_requested:
            self._finalise_stop()

    def _log_queue_summary(self, stats) -> None:
        """Emit the compact queue-loaded summary block (performance win)."""
        self.logger.info("Queue Loaded")
        self.logger.info(f"  READY        : {stats.ready}")
        self.logger.info(f"  MANUAL BONUS : {stats.manual} (Skipped)")
        self.logger.info(f"  INVALID      : {stats.invalid} (Skipped)")
        self.logger.info(f"  LIMIT        : {stats.limit} (Skipped)")

    # ---------------- monitoring mode ----------------
    def _enter_monitoring(self, reason: str = "Queue empty") -> None:
        """Switch the running worker into a low-noise waiting mode."""
        self.state = "monitoring"
        self._set_dot(self.dot_bot, "warn")
        self.txt_bot.setText("Monitoring")
        self.cur_status.setText("MONITORING")
        self.cur_status.setStyleSheet(
            "color:#3B82F6; font-size:11px; font-weight:700; letter-spacing:2px; "
            "padding:3px 10px; border:1px solid #3B82F655; border-radius:4px;"
        )
        self.cur_user.setText("—")
        self.cur_deposit.setText("0")
        self.cur_bonus.setText("0")
        self.prog_label.setText("Waiting for New Transactions")
        self._next_refresh_ts = time.monotonic() + self._monitoring_interval
        self._update_countdown_label()
        self.logger.info(f"Monitoring for new transactions ({reason})")

    def _exit_monitoring(self) -> None:
        self.state = "running"
        self._set_dot(self.dot_bot, "ok")
        self.txt_bot.setText("Running")
        self._next_refresh_ts = None

    def _tick_monitoring(self) -> None:
        """Called from _worker_step while state == 'monitoring'."""
        if self._next_refresh_ts is None:
            self._next_refresh_ts = time.monotonic() + self._monitoring_interval

        remaining = self._next_refresh_ts - time.monotonic()
        if remaining > 0:
            self._update_countdown_label()
            return

        # Countdown hit zero -> refresh the queue.
        try:
            stats = self.queue.refill()
            self._stamp_sync()
            if stats.ready > 0:
                self._log_queue_summary(stats)
                # Next worker tick will process the first READY item.
                # No log spam if nothing changed.
                self._exit_monitoring()
            else:
                # Reset countdown silently.
                self._next_refresh_ts = time.monotonic() + self._monitoring_interval
                self._update_countdown_label()
        except Exception as exc:
            self.logger.error(f"Monitoring refresh failed: {exc}")
            # Keep monitoring; retry after the normal interval.
            self._next_refresh_ts = time.monotonic() + self._monitoring_interval

        self._refresh_stats()

    def _update_countdown_label(self) -> None:
        if self._next_refresh_ts is None:
            return
        secs = max(0, int(round(self._next_refresh_ts - time.monotonic())))
        m, s = divmod(secs, 60)
        self.eta_label.setText(f"Next Refresh {m:02d}:{s:02d}")

    def _on_panel_lost(self) -> None:
        """Called when we detect the operator closed the Chromium window."""
        self.panel._dispose()
        self._set_dot(self.dot_panel, "idle")
        self.txt_panel.setText("Closed")
        self.btn_ready.setEnabled(False)
        self.logger.warn("Panel window closed by operator")

    def _finalise_stop(self, note: str = "Worker stopped") -> None:
        self.worker_timer.stop()
        self.metrics_timer.stop()
        self._refresh_metrics()      # final tick so labels show final values
        self.stop_requested = False
        self.state = "idle"
        self._set_dot(self.dot_bot, "idle")
        self.txt_bot.setText("Idle")
        self.cur_status.setText("Idle")
        self.btn_start.setEnabled(self.panel.is_attached and self.queue is not None)
        self.btn_refresh.setEnabled(self.queue is not None)
        self.btn_stop.setEnabled(False)
        self.logger.info(note)

        # A manual-list refresh may have been postponed while the worker was
        # running. Now that the queue is idle, do the deferred pull.
        if self._manual_refresh_pending:
            self._manual_refresh_pending = False
            self._reload_manual_list()

    def _save_screenshot(self, user_id: str) -> None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch for ch in user_id if ch.isalnum() or ch in "._-") or "user"
        path = Path("screenshots") / f"{ts}_{safe}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.panel.screenshot(str(path))

    # =============================================================
    # EXPORT + SETTINGS
    # =============================================================
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.config, self.config_path, self)
        if dlg.exec() == QDialog.Accepted:
            self.panel.panel_url = self.config.get("panel_url", "")
            self.validator = Validator(self.config["bonus_rules"])
            self._monitoring_interval = int(self.config.get("monitoring_interval_sec", 10))
            self.logger.info("Settings updated")

    def _open_database(self) -> None:
        running = self.state in ("running", "monitoring", "stopping")
        dlg = DatabaseDialog(self.db, worker_running=running, parent=self)
        dlg.exec()

    def _open_maintenance_center(self) -> None:
        """v1.2 C-1 entry-point."""
        # Local import to avoid a circular import at module load time
        # (ui.maintenance_center imports from core.* only).
        from ui.maintenance_center import MaintenanceCenter

        running = self.state in ("running", "monitoring", "stopping")
        dlg = MaintenanceCenter(
            parent=self,
            maintenance=self.maintenance_service,
            health=self.health_monitor,
            worker_running=running,
            app_dir=self.app_dir,
            resource_dir=self.resource_dir,
            config_path=self.config_path,
            selectors_path=self.app_dir / "config" / "selectors.json",
            credentials_path=self.credentials_path,
            sqlite_path=Path(str(self.db.path)),
            browser_profile_dir=Path(self.config.get("browser", {}).get("user_data_dir", "browser_profile_bonus_reload")),
            logs_dir=self.app_dir / "logs",
            screenshots_dir=self.app_dir / "screenshots",
        )
        dlg.exec()

    def _export_log(self, fmt: str) -> None:
        lines = self.logger.buffer()
        if not lines:
            QMessageBox.information(self, "Nothing to export", "Log buffer is empty.")
            return
        default_name = datetime.now().strftime("bonus-log-%Y%m%d-%H%M%S")
        filt = "Text (*.txt)" if fmt == "txt" else "CSV (*.csv)"
        path, _ = QFileDialog.getSaveFileName(self, "Export Log", f"{default_name}.{fmt}", filt)
        if not path:
            return
        try:
            if fmt == "txt":
                Path(path).write_text("\n".join(lines), encoding="utf-8")
            else:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["time", "message"])
                    for line in lines:
                        parts = line.split("  ", 1)
                        if len(parts) == 2:
                            writer.writerow(parts)
                        else:
                            writer.writerow(["", line])
            self.logger.info(f"Log exported: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    # =============================================================
    def closeEvent(self, event) -> None:
        # v1.2 B-7: graceful shutdown checklist.
        # Stop timers first so nothing races us.
        for name in (
            "manual_timer", "worker_timer", "panel_timer",
            "metrics_timer", "watchdog_timer",
        ):
            t = getattr(self, name, None)
            if isinstance(t, QTimer):
                try:
                    t.stop()
                except Exception:
                    pass

        # Checkpoint WAL so a hard OS shutdown after this cannot leave a
        # partially-written journal.
        try:
            self.db.checkpoint_wal("FULL")
        except Exception:
            pass

        # Close browser.
        try:
            self.panel.close()
        except Exception:
            pass

        # Persist current URL / window geometry as a clean-exit snapshot.
        try:
            self._persist_crash_state(clean_exit=True)
        except Exception:
            pass

        # Additive Manual repository cleanup must never obstruct the frozen
        # AUTO shutdown/checkpoint/browser/crash-state sequence above.
        try:
            if self.manual_repository is not None:
                self.manual_repository.close()
        except Exception:
            pass

        super().closeEvent(event)
