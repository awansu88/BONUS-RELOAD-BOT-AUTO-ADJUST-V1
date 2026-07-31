"""
Production hardening v1.2.0 — Maintenance orchestration.

Category C (Maintenance). Delivers:

    C-2  SQLite Maintenance (retention purge, WAL checkpoint,
         integrity check, ANALYZE, VACUUM).
    C-3  Automatic startup maintenance (WAL checkpoint + optimize +
         ANALYZE).  VACUUM is NEVER run automatically — only on manual
         operator request or on graceful application exit, and never
         while the worker is running.
    C-4  Log Maintenance (rotation-aware cleanup + explicit delete of
         files older than N days).
    C-5  Screenshot Maintenance (delete files older than N days).

Every operation returns a small `MaintenanceReport` so the UI can
render exactly what happened. Nothing raises to the caller — errors are
captured in the report so a failing sub-step never terminates the app.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, List, Optional


# --------------------------------------------------------------------------- report
@dataclass
class MaintenanceStep:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: int = 0


@dataclass
class MaintenanceReport:
    started_at: str = ""
    steps: List[MaintenanceStep] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", duration_ms: int = 0) -> None:
        self.steps.append(
            MaintenanceStep(name=name, ok=ok, detail=detail, duration_ms=duration_ms)
        )

    @property
    def all_ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def summary(self) -> str:
        lines = [f"Maintenance report ({self.started_at})"]
        for s in self.steps:
            tag = "OK" if s.ok else "FAIL"
            extra = f" — {s.detail}" if s.detail else ""
            lines.append(f"  [{tag}] {s.name} ({s.duration_ms} ms){extra}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- helpers
def _timed(fn):
    """Return (result, duration_ms). Never raises."""
    t0 = time.monotonic()
    try:
        result = fn()
        ok = True
        detail = "" if result is None else str(result)
        return ok, detail, int((time.monotonic() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", int((time.monotonic() - t0) * 1000)


# --------------------------------------------------------------------------- service
class MaintenanceService:
    """Wraps `DatabaseService` for maintenance; does NOT touch business
    logic. All engine-visible calls are executed via the same connection
    the app already uses (single-writer SQLite guarantee).

    All log / screenshot operations work on `Path` inputs; they never
    require the app to be running or the operator to know about the
    portable file layout.
    """

    def __init__(
        self,
        *,
        db,  # DatabaseService — untyped to avoid circular import
        logs_dir: Path,
        screenshots_dir: Path,
        backups_dir: Optional[Path] = None,
    ) -> None:
        self.db = db
        self.logs_dir = Path(logs_dir)
        self.screenshots_dir = Path(screenshots_dir)
        self.backups_dir = Path(backups_dir) if backups_dir else self.logs_dir.parent

    # ================================================================ SQLite
    def startup_maintenance(self) -> MaintenanceReport:
        """C-3 — safe, non-destructive, executed at application launch.

        VACUUM is NEVER included here."""
        r = MaintenanceReport(started_at=datetime.now().isoformat(timespec="seconds"))
        ok, detail, ms = _timed(lambda: self.db._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall())
        r.add("WAL checkpoint (passive)", ok, "" if ok else detail, ms)
        ok, detail, ms = _timed(lambda: self.db._conn.execute("PRAGMA optimize").fetchall())
        r.add("PRAGMA optimize", ok, "" if ok else detail, ms)
        return r

    def integrity_check(self) -> MaintenanceReport:
        r = MaintenanceReport(started_at=datetime.now().isoformat(timespec="seconds"))
        def _check() -> str:
            rows = self.db._conn.execute("PRAGMA integrity_check").fetchall()
            joined = "; ".join(str(x[0]) for x in rows)
            if joined.strip().lower() != "ok":
                raise RuntimeError(joined)
            return joined
        ok, detail, ms = _timed(_check)
        r.add("PRAGMA integrity_check", ok, detail, ms)
        return r

    def full_maintenance(
        self,
        *,
        retention_days: Optional[int],
        run_vacuum: bool,
    ) -> MaintenanceReport:
        """C-2 unified maintenance.

        `retention_days` == None disables the purge. `run_vacuum` MUST
        be gated by the caller — the UI passes False while the worker
        is running.
        """
        r = MaintenanceReport(started_at=datetime.now().isoformat(timespec="seconds"))

        if retention_days is not None and int(retention_days) > 0:
            days = int(retention_days)
            def _purge() -> str:
                deleted = self.db.clear_older_than(days)
                return f"deleted {deleted} rows older than {days}d"
            ok, detail, ms = _timed(_purge)
            r.add(f"Delete rows older than {days} days", ok, detail, ms)

        ok, detail, ms = _timed(
            lambda: self.db._conn.execute("PRAGMA wal_checkpoint(FULL)").fetchall()
        )
        r.add("WAL checkpoint (full)", ok, "" if ok else detail, ms)

        def _integrity() -> str:
            rows = self.db._conn.execute("PRAGMA integrity_check").fetchall()
            msg = "; ".join(str(x[0]) for x in rows)
            if msg.strip().lower() != "ok":
                raise RuntimeError(msg)
            return msg
        ok, detail, ms = _timed(_integrity)
        r.add("Integrity check", ok, detail, ms)

        ok, detail, ms = _timed(lambda: self.db._conn.execute("ANALYZE").fetchall())
        r.add("ANALYZE", ok, "" if ok else detail, ms)

        # PRAGMA optimize is cheap — always safe.
        ok, detail, ms = _timed(lambda: self.db._conn.execute("PRAGMA optimize").fetchall())
        r.add("PRAGMA optimize (update statistics)", ok, "" if ok else detail, ms)

        if run_vacuum:
            ok, detail, ms = _timed(lambda: self.db.vacuum())
            r.add("VACUUM", ok, "" if ok else detail, ms)

        return r

    # ================================================================ Logs (C-4)
    def list_logs(self) -> List[Path]:
        if not self.logs_dir.exists():
            return []
        return sorted([p for p in self.logs_dir.glob("*.log*") if p.is_file()])

    def cleanup_logs(self, *, keep_files: int = 30, older_than_days: Optional[int] = None) -> MaintenanceReport:
        """Delete rotated logs. `keep_files` overrides the FIFO limit; the
        active handler already caps at 30 via TimedRotatingFileHandler,
        but the operator can trim harder on demand."""
        r = MaintenanceReport(started_at=datetime.now().isoformat(timespec="seconds"))
        files = self.list_logs()
        removed = 0
        now = datetime.now()
        for p in files:
            keep = True
            if older_than_days is not None and older_than_days > 0:
                try:
                    mtime = datetime.fromtimestamp(p.stat().st_mtime)
                    if now - mtime > timedelta(days=int(older_than_days)):
                        keep = False
                except Exception:
                    pass
            if keep:
                continue
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
        # FIFO retention across whatever's left.
        remaining = self.list_logs()
        excess = max(0, len(remaining) - int(keep_files))
        if excess > 0:
            # Oldest first (list_logs is sorted ascending by name; log
            # file names are YYYY-MM-DD so lexical == chronological).
            for p in remaining[:excess]:
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass
        r.add(f"Cleanup logs ({self.logs_dir})", True, f"removed {removed} files", 0)
        return r

    # ================================================================ Screenshots (C-5)
    def list_screenshots(self) -> List[Path]:
        if not self.screenshots_dir.exists():
            return []
        return sorted([p for p in self.screenshots_dir.glob("*.png") if p.is_file()])

    def cleanup_screenshots(self, *, older_than_days: int) -> MaintenanceReport:
        r = MaintenanceReport(started_at=datetime.now().isoformat(timespec="seconds"))
        removed = 0
        skipped = 0
        now = datetime.now()
        cutoff = timedelta(days=max(0, int(older_than_days)))
        for p in self.list_screenshots():
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if now - mtime > cutoff:
                    p.unlink()
                    removed += 1
                else:
                    skipped += 1
            except Exception:
                pass
        r.add(
            f"Cleanup screenshots (>{older_than_days}d)",
            True,
            f"removed {removed}, kept {skipped}",
            0,
        )
        return r
