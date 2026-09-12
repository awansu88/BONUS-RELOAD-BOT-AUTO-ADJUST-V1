"""PATCH-09 telemetry contract tests (no network or browser required)."""
from __future__ import annotations

import ast
from pathlib import Path
import pytest
from types import SimpleNamespace

from core.performance_telemetry import (
    EventLoopStallDetector, PerformanceTelemetry, configure, get_telemetry, timed,
)
from core.sheet_service import SheetService
from core.panel_service import AutoSubmitOutcome
from tests.test_patch00_auto_baseline import (
    queue, row, run_worker, worker_dashboard,
)
from tests.test_patch02_auto_submit_classification import FakePage, service


class LogSink:
    def __init__(self):
        self.lines = []

    def info(self, line): self.lines.append(line)
    def warn(self, line): self.lines.append(line)


class Clock:
    def __init__(self, *values): self.values = iter(values)
    def __call__(self): return next(self.values)


def test_duration_uses_perf_counter_and_aggregates():
    telemetry = PerformanceTelemetry(clock=Clock(10.0, 10.125))
    with telemetry.measure("work"): pass
    value = telemetry.snapshot()["work"]
    assert value["count"] == 1
    assert value["min_ms"] == pytest.approx(125)
    assert value["max_ms"] == pytest.approx(125)
    assert value["total_ms"] == pytest.approx(125)


def test_disabled_is_noop():
    telemetry = PerformanceTelemetry(enabled=False, clock=lambda: (_ for _ in ()).throw(RuntimeError()))
    with telemetry.measure("never"): pass
    assert telemetry.snapshot() == {}


def test_telemetry_failures_do_not_escape_business_call(monkeypatch):
    configure({"performance_telemetry_enabled": True})
    monkeypatch.setattr(get_telemetry(), "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    @timed("broken")
    def business(): return 42
    assert business() == 42


def test_samples_are_bounded_but_cumulative_aggregates_are_correct():
    telemetry = PerformanceTelemetry(sample_limit=3, slow_threshold_ms=9999)
    for value in (1, 2, 3, 4): telemetry.record("m", value)
    data = telemetry.snapshot()["m"]
    assert data["samples"] == [2, 3, 4]
    assert (data["count"], data["min_ms"], data["max_ms"], data["total_ms"]) == (4, 1, 4, 10)


def test_percentiles_are_deterministic_nearest_rank():
    samples = [10, 20, 30, 40, 50]
    assert PerformanceTelemetry.percentile(samples, 50) == 30
    assert PerformanceTelemetry.percentile(samples, 95) == 50
    assert PerformanceTelemetry.percentile(samples, 99) == 50


def test_only_slow_events_are_reported():
    sink = LogSink(); telemetry = PerformanceTelemetry(slow_threshold_ms=100, logger=sink)
    telemetry.record("fast", 99); telemetry.record("slow", 100, row_count=7)
    assert len(sink.lines) == 1
    assert "[PERF] SLOW metric=slow" in sink.lines[0] and "row_count=7" in sink.lines[0]


def test_periodic_summary_output():
    sink = LogSink(); telemetry = PerformanceTelemetry(slow_threshold_ms=9999, logger=sink)
    telemetry.record("m", 10); telemetry.record("m", 30)
    lines = telemetry.summarize(60, queue_size=2)
    assert len(lines) == 1
    assert "metric=m count=2 avg_ms=20.0" in lines[0]
    assert "p50_ms=10.0" in lines[0] and "queue_size=2" in lines[0]


def test_ui_first_tick_after_long_startup_delay_only_sets_baseline():
    sink = LogSink(); telemetry = PerformanceTelemetry(slow_threshold_ms=9999, logger=sink)
    detector = EventLoopStallDetector(telemetry, 500, 1000, Clock(3.0))
    assert detector.tick() == 0
    assert telemetry.snapshot() == {}
    assert not any("UI_STALL" in line for line in sink.lines)


def test_ui_second_late_tick_records_excess_over_expected_interval():
    sink = LogSink(); telemetry = PerformanceTelemetry(slow_threshold_ms=9999, logger=sink)
    detector = EventLoopStallDetector(telemetry, 500, 1000, Clock(3.0, 5.0))
    assert detector.tick() == 0
    assert detector.tick() == pytest.approx(1500)
    assert telemetry.snapshot()["ui.event_loop_stall"]["count"] == 1
    assert any("[PERF] UI_STALL duration_ms=1500.0" in line for line in sink.lines)


def test_ui_normal_tick_no_false_stall_and_shutdown_idempotent():
    telemetry = PerformanceTelemetry()
    detector = EventLoopStallDetector(telemetry, 500, 1000, Clock(3.0, 3.5))
    assert detector.tick() == 0
    assert detector.tick() == 0
    assert telemetry.snapshot() == {}
    detector.shutdown(); detector.shutdown(); telemetry.shutdown(); telemetry.shutdown()
    assert detector.tick() == 0


def _sheet_config():
    return {"columns": {"user_id": 2, "true_amount": 6, "tx_id": 9, "time_stamp": 5},
            "required_headers": {}, "sheet_names": {"master": "MASTER", "manual_bonus_reload": "MANUAL"}}


class Worksheet:
    title = "MASTER"
    def __init__(self, values=None, error=None): self.values, self.error = values, error
    def get_all_values(self):
        if self.error: raise self.error
        return self.values
    def col_values(self, column):
        assert column == 2
        if self.error: raise self.error
        return [row[1] if len(row) > 1 else "" for row in self.values]


def test_master_instrumentation_preserves_value_and_exception():
    configure({"performance_telemetry_enabled": True, "performance_slow_threshold_ms": 9999})
    service = SheetService("unused", _sheet_config())
    service._master = Worksheet([["h"] * 9, ["", "user", "", "", "2026-01-01", "100", "", "", "tx"]])
    assert service.read_master_rows()[0].tx_id == "tx"
    assert get_telemetry().snapshot()["sheet.master_read"]["count"] == 1
    service._master = Worksheet(error=ValueError("source"))
    with pytest.raises(ValueError, match="source"): service.read_master_rows()
    assert get_telemetry().snapshot()["sheet.master_read"]["count"] == 2


def test_manual_instrumentation_preserves_value_and_exception():
    configure({"performance_telemetry_enabled": True, "performance_slow_threshold_ms": 9999})
    service = SheetService("unused", _sheet_config())
    service._manual = Worksheet([["h", "user"], ["", "alice"]])
    assert service.read_manual_set() == {"alice"}
    service._manual = Worksheet(error=RuntimeError("source"))
    with pytest.raises(RuntimeError, match="source"): service.read_manual_set()


def test_old_and_enabled_config_defaults_are_backward_compatible():
    old = configure({})
    assert old.enabled and old.slow_threshold_ms == 1000
    configured = configure({"performance_telemetry_enabled": False,
                            "performance_slow_threshold_ms": 25,
                            "performance_sample_limit": 7})
    assert not configured.enabled and configured.sample_limit == 7


def test_timed_decorator_preserves_exception():
    configure({})
    @timed("failure")
    def fail(): raise KeyError("business")
    with pytest.raises(KeyError, match="business"): fail()


def test_queue_instrumentation_preserves_ready_order_and_content(tmp_path):
    configure({"performance_telemetry_enabled": True,
               "performance_slow_threshold_ms": 9999})
    db, manager = queue(tmp_path, [row("A", "alice", 50_000),
                                   row("B", "bob", 100_000)])
    stats = manager.refill()
    assert (stats.total, stats.ready, stats.limit, stats.invalid) == (2, 2, 0, 0)
    assert [(item.tx_id, item.username, item.bonus, item.status)
            for item in manager.preview_items()] == [
                ("A", "alice", 5_000, "READY"),
                ("B", "bob", 10_000, "READY"),
            ]
    assert manager.next_ready().tx_id == "A"
    manager.mark_processed(manager.next_ready(), True)
    assert manager.next_ready().tx_id == "B"
    assert {"queue.refill.total", "queue.refill.sheet_read", "queue.refill.dedup",
            "queue.refill.validation_build"} <= set(get_telemetry().snapshot())
    db.close()


@pytest.mark.parametrize(("failure", "expected"), [
    ("", AutoSubmitOutcome.SUCCESS),
    ("fill:#user", AutoSubmitOutcome.FAILED_NOT_SUBMITTED),
    ("success", AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT),
])
def test_panel_telemetry_preserves_submit_classification(failure, expected):
    configure({"performance_telemetry_enabled": True,
               "performance_slow_threshold_ms": 9999})
    result = service(FakePage(fail=failure)).submit_deposit_classified(
        "alice", 5000, "BONUS RELOAD AUTO")
    assert result.outcome is expected
    assert get_telemetry().snapshot()["panel.submit.total"]["count"] == 1


def test_panel_result_remains_authoritative_when_telemetry_record_fails(monkeypatch):
    telemetry = configure({"performance_telemetry_enabled": True})
    monkeypatch.setattr(
        telemetry, "record",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("telemetry")),
    )
    result = service(FakePage(fail="success")).submit_deposit_classified(
        "alice", 5000, "BONUS RELOAD AUTO")
    assert result.outcome is AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT


def test_old_success_alert_has_no_field_wait_penalty_or_manual_reload():
    page = FakePage(stale_visible=True)
    result = service(page).submit_deposit_classified(
        "alice", 5000, "BONUS RELOAD AUTO")
    assert result.outcome is AutoSubmitOutcome.SUCCESS
    assert page.events.index(("navigation", "armed")) < page.events.index(
        ("submit", "#submit"))
    assert not any(event[0] == "locator-wait" and event[-1] == "hidden"
                   for event in page.events)
    assert page.reloads == 0 and page.gotos == 0


def test_auto_worker_telemetry_preserves_success_state(tmp_path):
    configure({"performance_telemetry_enabled": True,
               "performance_slow_threshold_ms": 9999})
    db, manager = queue(tmp_path, [row("tx", "alice", 50_000)])
    manager.refill()
    dashboard, submits, finalised = worker_dashboard(db, manager)
    run_worker(dashboard)
    assert submits == [{"user_id": "alice", "bonus": 5000,
                        "remark": "BONUS RELOAD AUTO"}]
    assert manager.stats().processed == 1 and manager.next_ready() is None
    assert dashboard.state == "running" and finalised == []
    assert get_telemetry().snapshot()["auto.transaction.total"]["count"] == 1
    db.close()


def test_dashboard_shutdown_stops_telemetry_timers_and_services():
    class FakeTimer:
        def __init__(self): self.stop_count = 0
        def stop(self): self.stop_count += 1

    class Shutdown:
        def __init__(self): self.count = 0
        def shutdown(self): self.count += 1

    timers = {name: FakeTimer() for name in (
        "manual_timer", "worker_timer", "recovery_timer", "panel_timer",
        "metrics_timer", "watchdog_timer", "manual_worker_timer",
        "manual_heartbeat_timer", "performance_heartbeat_timer",
        "performance_summary_timer",
    )}
    stall, telemetry = Shutdown(), Shutdown()
    host = SimpleNamespace(
        **timers, performance_stall_detector=stall, performance=telemetry,
        manual_controller=None, manual_repository=None,
        logger=SimpleNamespace(error=lambda *_: None),
        db=SimpleNamespace(checkpoint_wal=lambda *_: None),
        panel=SimpleNamespace(close=lambda: None),
        _cancel_panel_recovery=lambda: None,
        _persist_crash_state=lambda **_: None,
    )
    tree = ast.parse((Path(__file__).parents[1] / "ui" / "dashboard.py").read_text())
    dashboard_class = next(node for node in tree.body
                           if isinstance(node, ast.ClassDef) and node.name == "Dashboard")
    method = next(node for node in dashboard_class.body
                  if isinstance(node, ast.FunctionDef) and node.name == "closeEvent")
    method.body.pop()  # omit only QMainWindow's zero-argument super call in this fake host
    namespace = {"QTimer": FakeTimer}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                 "ui/dashboard.py", "exec"), namespace)
    close_event = namespace["closeEvent"]
    for _ in range(2):
        close_event(host, None)
    assert timers["performance_heartbeat_timer"].stop_count == 2
    assert timers["performance_summary_timer"].stop_count == 2
    assert stall.count == 2 and telemetry.count == 2
