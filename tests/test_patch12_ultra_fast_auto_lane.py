"""PATCH-12 ultra-fast READY-lane regressions (no browser/network required)."""
from __future__ import annotations

from pathlib import Path

from core.performance_telemetry import configure, get_telemetry
from tests.test_patch00_auto_baseline import queue, row, run_worker, worker_dashboard


def test_ready_transaction_uses_cache_and_never_reads_manual_sheet(tmp_path):
    db, manager = queue(tmp_path, [row("A", "alice", 50_000)])
    manager.refill()
    fake, submits, _ = worker_dashboard(
        db, manager, refresh=lambda: (_ for _ in ()).throw(
            AssertionError("READY lane attempted a Google MANUAL read")))
    configure({"performance_telemetry_enabled": True,
               "performance_slow_threshold_ms": 9999})

    run_worker(fake)

    assert [call["user_id"] for call in submits] == ["alice"]
    assert get_telemetry().snapshot()["auto.local_manual_check"]["count"] == 1
    db.close()


def test_cached_manual_addition_skips_without_network(tmp_path):
    db, manager = queue(tmp_path, [row("A", "USER-A", 50_000)])
    manager.refill()
    fake, submits, _ = worker_dashboard(
        db, manager, refresh=lambda: (_ for _ in ()).throw(AssertionError))
    fake.cache.set_manual({"user-a"})

    run_worker(fake)

    assert submits == []
    assert db.has_known_auto_tx("A")
    db.close()


def test_new_sheet_manual_entry_is_intentionally_not_visible_mid_batch(tmp_path):
    """The accepted trade-off: USER-B added remotely after refill waits until
    the next snapshot; AUTO must not synchronously query Google before submit.
    """
    db, manager = queue(tmp_path, [row("B", "USER-B", 50_000)])
    manager.refill()
    fake, submits, _ = worker_dashboard(
        db, manager, refresh=lambda: (_ for _ in ()).throw(
            AssertionError("must not discover simulated remote USER-B")))

    run_worker(fake)

    assert [call["user_id"] for call in submits] == ["USER-B"]
    db.close()


def test_active_batch_has_no_refill_and_no_fixed_pacing(tmp_path):
    db, manager = queue(tmp_path, [row("A", "alice", 50_000),
                                   row("B", "bob", 50_000)])
    manager.refill()
    manager.refill = lambda: (_ for _ in ()).throw(
        AssertionError("MASTER refill entered active READY lane"))
    fake, submits, _ = worker_dashboard(db, manager)

    run_worker(fake)
    run_worker(fake)

    assert [call["user_id"] for call in submits] == ["alice", "bob"]
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    start = source.index("def _on_start")
    worker = source.index("def _worker_step")
    assert "worker_timer.start(500)" not in source[start:worker]
    assert 'worker_timer.start(500 if self.state == "monitoring" else 0)' in source
    db.close()


def test_empty_queue_enters_slow_monitoring_without_refill(tmp_path):
    db, manager = queue(tmp_path, [])
    manager.refill()
    fake, submits, _ = worker_dashboard(db, manager)

    run_worker(fake)

    assert fake.state == "monitoring"
    assert submits == []
    db.close()
