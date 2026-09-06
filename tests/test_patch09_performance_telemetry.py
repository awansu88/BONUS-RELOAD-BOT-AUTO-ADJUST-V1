"""PATCH-09 telemetry contract tests (no network or browser required)."""
from __future__ import annotations

import pytest

from core.performance_telemetry import (
    EventLoopStallDetector, PerformanceTelemetry, configure, get_telemetry, timed,
)
from core.sheet_service import SheetService


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


def test_ui_late_tick_records_excess_over_expected_interval():
    sink = LogSink(); telemetry = PerformanceTelemetry(slow_threshold_ms=9999, logger=sink)
    detector = EventLoopStallDetector(telemetry, 500, 1000, Clock(1.0, 2.75))
    assert detector.tick() == pytest.approx(1250)
    assert telemetry.snapshot()["ui.event_loop_stall"]["count"] == 1
    assert any("[PERF] UI_STALL duration_ms=1250.0" in line for line in sink.lines)


def test_ui_normal_tick_no_false_stall_and_shutdown_idempotent():
    telemetry = PerformanceTelemetry()
    detector = EventLoopStallDetector(telemetry, 500, 1000, Clock(1.0, 1.5))
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
