"""
v1.2.0 Production Hardening — Health watchdog tests.

Category: B-3 (Health Watchdog), B-5 (Resource Leak Detection),
C-6 (Health diagnostics score).

The monitor is designed to sample only — it must never raise, even
when every probe blows up. These tests pin that contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.health import HealthMonitor, HealthSnapshot, LeakThresholds, collect_versions


def _monitor(**overrides) -> HealthMonitor:
    kw = dict(
        db_probe=lambda: True,
        google_probe=lambda: True,
        panel_probe=lambda: 1,
        worker_state_probe=lambda: "idle",
        queue_size_probe=lambda: 0,
        qtimer_count_probe=lambda: 4,
        thresholds=LeakThresholds(
            memory_mb_max=1000,
            thread_count_max=200,
            handle_count_max=5000,
            browser_contexts_max=3,
            qtimer_count_max=50,
        ),
    )
    kw.update(overrides)
    return HealthMonitor(**kw)


def test_snapshot_returns_pass_when_healthy():
    m = _monitor()
    s = m.snapshot()
    assert s.sqlite_ok
    assert s.google_ok
    assert s.worker_state == "idle"
    assert s.queue_size == 0
    assert s.score == "PASS"
    assert s.warnings == []


def test_snapshot_marks_failed_when_sqlite_probe_returns_false():
    m = _monitor(db_probe=lambda: False)
    s = m.snapshot()
    assert not s.sqlite_ok
    assert s.score == "FAILED"


def test_snapshot_never_raises_when_probes_crash():
    m = _monitor(
        db_probe=lambda: (_ for _ in ()).throw(RuntimeError("db down")),
        google_probe=lambda: (_ for _ in ()).throw(RuntimeError("net down")),
        panel_probe=lambda: (_ for _ in ()).throw(RuntimeError("browser gone")),
        worker_state_probe=lambda: (_ for _ in ()).throw(RuntimeError("weird")),
        queue_size_probe=lambda: (_ for _ in ()).throw(RuntimeError("q")),
        qtimer_count_probe=lambda: (_ for _ in ()).throw(RuntimeError("t")),
    )
    s = m.snapshot()
    assert isinstance(s, HealthSnapshot)
    # Everything failed safely.
    assert s.sqlite_ok is False
    assert s.google_ok is False
    assert s.browser_contexts == 0
    assert s.worker_state == "unknown"
    assert s.queue_size == 0
    assert s.qtimer_count is None
    assert s.score == "FAILED"


def test_warning_on_extra_browser_context():
    m = _monitor(
        panel_probe=lambda: 5,
        thresholds=LeakThresholds(browser_contexts_max=1),
    )
    s = m.snapshot()
    assert any("browser contexts" in w for w in s.warnings)
    assert s.score in ("WARNING", "FAILED")


def test_warning_on_qtimer_growth():
    m = _monitor(
        qtimer_count_probe=lambda: 50,
        thresholds=LeakThresholds(qtimer_count_max=10),
    )
    s = m.snapshot()
    assert any("QTimer" in w for w in s.warnings)
    assert s.score == "WARNING"


def test_history_capped_at_60():
    m = _monitor()
    for _ in range(70):
        m.snapshot()
    hist = m.history()
    assert len(hist) == 60


def test_collect_versions_returns_readable_dict():
    versions = collect_versions()
    assert "python" in versions
    assert versions["python"]
    assert "sqlite" in versions
    # These keys must always exist even when the discovery fails.
    assert "playwright" in versions
    assert "chromium" in versions


def test_collect_versions_reads_chromium_from_pw_browsers(tmp_path: Path):
    (tmp_path / "chromium-1181").mkdir()
    versions = collect_versions(pw_browsers_dir=tmp_path)
    assert versions["chromium"] == "build 1181"


def test_snapshot_ts_is_populated():
    s = _monitor().snapshot()
    assert s.ts > 0
