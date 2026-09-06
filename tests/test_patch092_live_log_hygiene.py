"""PATCH-09.2 regression tests for persistent versus operator logging."""
from __future__ import annotations

import logging

import pytest

from core.logger import AppLogger
from core.health import classify_warning_transitions
from core.performance_telemetry import EventLoopStallDetector, PerformanceTelemetry


class CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def app_logger(tmp_path):
    AppLogger.reset()
    logger = AppLogger.get(str(tmp_path / "logs"))
    capture = CaptureHandler()
    logger.logger.addHandler(capture)
    yield logger, capture
    AppLogger.reset()


def test_operational_levels_are_persistent_buffered_and_published(app_logger):
    logger, capture = app_logger
    live = []
    logger.add_listener(live.append)

    logger.info("connected")
    logger.warn("operator warning")
    logger.error("critical failure")

    assert capture.messages == ["connected", "operator warning", "critical failure"]
    assert [line.split("  ", 1)[1] for line in logger.buffer()] == [
        "connected", "WARN  operator warning", "ERROR  critical failure",
    ]
    assert live == logger.buffer()


def test_diagnostics_are_persistent_only_and_buffer_stays_bounded(app_logger):
    logger, capture = app_logger
    live = []
    logger.add_listener(live.append)

    logger.diagnostic("detail")
    logger.diagnostic_warn("slow detail")

    assert capture.messages == ["detail", "slow detail"]
    assert logger.buffer() == []
    assert live == []
    assert logger.MAX_BUFFER == 500
    for number in range(501):
        logger.info(str(number))
    assert len(logger.buffer()) == 500
    assert logger.buffer()[0].endswith("1")


def test_perf_slow_summary_and_ui_stall_are_persistent_only(app_logger):
    logger, capture = app_logger
    live = []
    logger.add_listener(live.append)
    telemetry = PerformanceTelemetry(logger=logger, slow_threshold_ms=100)

    telemetry.record("panel.submit.total", 150)
    summaries = telemetry.summarize(60, queue_size=1)
    detector = EventLoopStallDetector(
        telemetry, interval_ms=500, threshold_ms=1000,
        clock=iter((3.0, 5.0)).__next__,
    )
    detector.tick()
    assert detector.tick() == pytest.approx(1500)

    assert any("[PERF] SLOW metric=panel.submit.total" in line for line in capture.messages)
    assert any("[PERF] SUMMARY" in line for line in capture.messages)
    assert any("[PERF] UI_STALL" in line for line in capture.messages)
    assert summaries and telemetry.snapshot()["ui.event_loop_stall"]["count"] == 1
    assert live == []
    assert logger.buffer() == []


def test_watchdog_deduplicates_by_category_and_reports_transitions():
    state, new, changed, recovered = classify_warning_transitions(
        {}, ["handles 1829 > 800"]
    )
    assert (new, changed, recovered) == (["handles 1829 > 800"], [], [])
    state, new, changed, recovered = classify_warning_transitions(
        state, ["handles 1852 > 800"]
    )
    assert (new, changed, recovered) == ([], ["handles 1852 > 800"], [])
    state, new, changed, recovered = classify_warning_transitions(state, [])
    assert (new, changed, recovered) == ([], [], ["handles"])
    state, new, changed, recovered = classify_warning_transitions(
        state, ["handles 1844 > 800"]
    )
    assert (new, changed, recovered) == (["handles 1844 > 800"], [], [])
