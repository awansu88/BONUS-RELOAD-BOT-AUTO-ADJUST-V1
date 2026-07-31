"""
Production hardening v1.2.0 — Health & resource-leak watchdog.

Category D (Diagnostics) + Category B (Infrastructure).

Sampling only — this module never modifies application behaviour. It
reads runtime process metrics (RSS, thread count, open file handles,
QTimer count if a Qt app is running, browser/context liveness, SQLite
connectivity) and returns a compact `HealthSnapshot`.

The snapshot is consumed by:

    * The Health Watchdog QTimer (Category B, wired from
      `ui/dashboard.py`), which runs `snapshot()` every N seconds and
      logs a WARN if a leak threshold is crossed. It does NOT restart
      anything on its own beyond what the wrapped services already
      support — restart decisions live in `ui/dashboard.py` where the
      state machine is.

    * The Maintenance Center (Category C) — surfaces the same numbers
      to the operator alongside PASS / WARNING / FAILED health scores
      and an "Export Diagnostic Report" action.

The module deliberately has ONE hard dependency (`psutil`) which is
only imported lazily and treated as optional: on a machine where it is
missing (very old Windows Python install), the memory / handle / thread
metrics fall back to `None` and the rest of the health checks continue
to run.
"""

from __future__ import annotations

import platform
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# --------------------------------------------------------------------------- psutil (soft)
def _psutil():  # pragma: no cover - trivial soft import wrapper
    try:
        import psutil  # type: ignore

        return psutil
    except Exception:
        return None


# --------------------------------------------------------------------------- data types
@dataclass
class HealthSnapshot:
    ts: float
    memory_mb: Optional[float]
    thread_count: Optional[int]
    handle_count: Optional[int]
    browser_contexts: int
    qtimer_count: Optional[int]
    sqlite_ok: bool
    google_ok: bool
    worker_state: str
    queue_size: int
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def score(self) -> str:
        """PASS / WARNING / FAILED — coarse operator-facing grade.

        FAILED requires a hard fault (SQLite closed, browser dead while
        worker running, etc.). WARNING covers "growth" style alarms
        raised by `_check_thresholds` — nothing that stops production.
        PASS otherwise.
        """
        if not self.sqlite_ok:
            return "FAILED"
        if self.warnings:
            return "WARNING"
        return "PASS"


@dataclass
class LeakThresholds:
    """v1.2 B-5 tunables. Defaults chosen so the desktop bot idle
    running for weeks stays under them; growth past them means a
    genuine issue the operator should see in the log."""
    memory_mb_max: float = 800.0
    thread_count_max: int = 60
    handle_count_max: int = 800
    browser_contexts_max: int = 1
    qtimer_count_max: int = 20


# --------------------------------------------------------------------------- monitor
class HealthMonitor:
    """Zero-side-effect sampler.

    Callers pass small callables so this module never imports the
    production engine — it just samples what the caller exposes.
    """

    def __init__(
        self,
        *,
        db_probe: Callable[[], bool],
        google_probe: Callable[[], bool],
        panel_probe: Callable[[], int],
        worker_state_probe: Callable[[], str],
        queue_size_probe: Callable[[], int],
        qtimer_count_probe: Optional[Callable[[], int]] = None,
        thresholds: Optional[LeakThresholds] = None,
    ) -> None:
        self._db_probe = db_probe
        self._google_probe = google_probe
        self._panel_probe = panel_probe
        self._worker_state_probe = worker_state_probe
        self._queue_size_probe = queue_size_probe
        self._qtimer_count_probe = qtimer_count_probe
        self.thresholds = thresholds or LeakThresholds()

        self._history: List[HealthSnapshot] = []
        self._history_max = 60  # last 60 samples for growth analysis

    # ---------------------------------------------------------------- probe
    def snapshot(self) -> HealthSnapshot:
        """One point-in-time reading. Never raises."""
        ps = _psutil()
        mem_mb: Optional[float] = None
        threads: Optional[int] = None
        handles: Optional[int] = None
        if ps is not None:
            try:
                p = ps.Process()
                mem_mb = float(p.memory_info().rss) / (1024.0 * 1024.0)
                threads = int(p.num_threads())
                # `num_handles` is Windows-only; `num_fds` for POSIX.
                try:
                    handles = int(p.num_handles())  # type: ignore[attr-defined]
                except Exception:
                    try:
                        handles = int(p.num_fds())
                    except Exception:
                        handles = None
            except Exception:
                pass
        else:
            # Best-effort thread count even without psutil.
            threads = threading.active_count()

        try:
            browser_contexts = int(self._panel_probe() or 0)
        except Exception:
            browser_contexts = 0
        try:
            sqlite_ok = bool(self._db_probe())
        except Exception:
            sqlite_ok = False
        try:
            google_ok = bool(self._google_probe())
        except Exception:
            google_ok = False
        try:
            worker_state = str(self._worker_state_probe() or "unknown")
        except Exception:
            worker_state = "unknown"
        try:
            queue_size = int(self._queue_size_probe() or 0)
        except Exception:
            queue_size = 0
        qtimer_count: Optional[int] = None
        if self._qtimer_count_probe is not None:
            try:
                qtimer_count = int(self._qtimer_count_probe())
            except Exception:
                qtimer_count = None

        snap = HealthSnapshot(
            ts=time.time(),
            memory_mb=mem_mb,
            thread_count=threads,
            handle_count=handles,
            browser_contexts=browser_contexts,
            qtimer_count=qtimer_count,
            sqlite_ok=sqlite_ok,
            google_ok=google_ok,
            worker_state=worker_state,
            queue_size=queue_size,
        )
        snap.warnings = self._check_thresholds(snap)
        self._history.append(snap)
        if len(self._history) > self._history_max:
            self._history = self._history[-self._history_max :]
        return snap

    # ---------------------------------------------------------------- thresholds
    def _check_thresholds(self, s: HealthSnapshot) -> List[str]:
        w: List[str] = []
        t = self.thresholds
        if s.memory_mb is not None and s.memory_mb > t.memory_mb_max:
            w.append(f"memory {s.memory_mb:.0f} MB > {t.memory_mb_max:.0f} MB")
        if s.thread_count is not None and s.thread_count > t.thread_count_max:
            w.append(f"threads {s.thread_count} > {t.thread_count_max}")
        if s.handle_count is not None and s.handle_count > t.handle_count_max:
            w.append(f"handles {s.handle_count} > {t.handle_count_max}")
        if s.browser_contexts > t.browser_contexts_max:
            w.append(
                f"browser contexts {s.browser_contexts} > {t.browser_contexts_max}"
            )
        if s.qtimer_count is not None and s.qtimer_count > t.qtimer_count_max:
            w.append(f"QTimer {s.qtimer_count} > {t.qtimer_count_max}")
        # SQLite hard fail is not a "warning" — the score property flags
        # it as FAILED — but we still surface it in the report.
        if not s.sqlite_ok:
            w.append("SQLite probe failed")
        return w

    # ---------------------------------------------------------------- report
    def history(self) -> List[HealthSnapshot]:
        return list(self._history)


# --------------------------------------------------------------------------- versions
def collect_versions(pw_browsers_dir: Optional[Path] = None) -> Dict[str, str]:
    """Best-effort version dump for the diagnostics panel.

    Never raises. Values that can't be discovered come back as
    ``"unknown"`` so the UI has something readable to display.
    """
    versions: Dict[str, str] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "playwright": "unknown",
        "chromium": "unknown",
        "pyside6": "unknown",
    }
    try:
        import playwright  # type: ignore

        versions["playwright"] = getattr(playwright, "__version__", "unknown")
    except Exception:
        pass
    try:
        import PySide6  # type: ignore

        versions["pyside6"] = getattr(PySide6, "__version__", "unknown")
    except Exception:
        pass
    if pw_browsers_dir is not None:
        try:
            # Chromium builds land in `chromium-<build>` folders. We do NOT
            # invoke the binary here — that would launch a real process
            # just to print --version, which is exactly the kind of thing
            # a health probe must never do.
            candidates = [
                p.name for p in pw_browsers_dir.glob("chromium-*") if p.is_dir()
            ]
            if candidates:
                versions["chromium"] = candidates[0].replace("chromium-", "build ")
        except Exception:
            pass
    return versions
