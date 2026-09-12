"""PATCH-12 ultra-fast READY-lane regressions (no browser/network required)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.performance_telemetry import configure, get_telemetry
from core.panel_service import PanelService
from tests.test_patch00_auto_baseline import (
    dashboard_method, queue, row, run_worker, worker_dashboard,
)


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


class OptionalLocator:
    def __init__(self, present):
        self.present = present
        self.selections = []

    @property
    def first(self):
        return self

    def count(self):
        return int(self.present)

    def select_option(self, **kwargs):
        if not self.present:
            raise AssertionError("absent dropdown entered select autowait")
        self.selections.append(kwargs)


class OptionalPage:
    def __init__(self, present):
        self.loc = OptionalLocator(present)

    def locator(self, _selector):
        return self.loc


def test_optional_dropdown_absence_returns_before_select_wait():
    page = OptionalPage(present=False)
    PanelService._maybe_select(page, "#optional", "Bank Transfer")
    assert page.loc.selections == []


def test_present_dropdown_keeps_selection_with_bounded_timeout():
    page = OptionalPage(present=True)
    PanelService._maybe_select(page, "#payment", "Bank Transfer")
    assert page.loc.selections == [{"label": "Bank Transfer", "timeout": 500}]


def test_two_consecutive_ready_transactions_record_exactly_one_gap(tmp_path):
    telemetry = configure({"performance_telemetry_enabled": True,
                           "performance_slow_threshold_ms": 9999})
    db, manager = queue(tmp_path, [row("A", "alice", 50_000),
                                   row("B", "bob", 50_000)])
    manager.refill()
    fake, _, _ = worker_dashboard(db, manager)
    fake._last_auto_tx_completed_at = None

    run_worker(fake)
    run_worker(fake)

    assert telemetry.snapshot()["auto.inter_transaction_gap"]["count"] == 1
    db.close()


def _monitoring_host():
    label = SimpleNamespace(setText=lambda *_: None, setStyleSheet=lambda *_: None)
    return SimpleNamespace(
        state="running", _last_auto_tx_completed_at=1.0,
        worker_timer=SimpleNamespace(setInterval=lambda *_: None),
        _set_dot=lambda *_: None, dot_bot=None, txt_bot=label,
        cur_status=label, cur_user=label, cur_deposit=label, cur_bonus=label,
        prog_label=label, _monitoring_interval=10, _next_refresh_ts=None,
        _update_countdown_label=lambda: None,
        logger=SimpleNamespace(info=lambda *_: None),
    )


def test_entering_monitoring_discards_previous_batch_gap():
    host = _monitoring_host()
    enter = dashboard_method("_enter_monitoring", {"time": __import__("time")})
    enter(host)
    assert host.state == "monitoring"
    assert host._last_auto_tx_completed_at is None


def test_session_reset_discards_stale_gap_before_first_transaction():
    label = SimpleNamespace(setText=lambda *_: None)
    host = SimpleNamespace(
        _last_auto_tx_completed_at=1.0, stat_bonus_paid=label, stat_rate=label,
        stat_avg_submit=label, stat_elapsed=label,
    )
    reset = dashboard_method("_reset_session", {"time": __import__("time")})
    reset(host)
    assert host._last_auto_tx_completed_at is None
