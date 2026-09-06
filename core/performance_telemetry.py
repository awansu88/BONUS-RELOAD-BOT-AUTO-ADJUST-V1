"""Low-overhead, best-effort runtime performance telemetry.

Measurements use ``perf_counter`` and retain only a bounded recent window.
Every public operation is fail-open: telemetry must never affect business work.
"""
from __future__ import annotations

import functools
import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Optional


def _safe_context(context: Optional[dict]) -> str:
    if not context:
        return ""
    parts = []
    for key in sorted(context):
        value = context[key]
        if value is None:
            continue
        text = str(value).replace("\n", " ").replace("\r", " ")[:120]
        parts.append(f"{key}={text}")
    return (" " + " ".join(parts)) if parts else ""


@dataclass
class Metric:
    samples: deque = field(default_factory=deque)
    count: int = 0
    total_ms: float = 0.0
    minimum_ms: float = math.inf
    maximum_ms: float = 0.0


class PerformanceTelemetry:
    """Thread-safe bounded aggregates with slow-event and window summaries."""

    def __init__(self, enabled=True, slow_threshold_ms=1000,
                 sample_limit=512, logger=None, clock=None):
        self.enabled = bool(enabled)
        self.slow_threshold_ms = max(0.0, float(slow_threshold_ms))
        self.sample_limit = max(1, int(sample_limit))
        self.logger = logger
        self.clock = clock or time.perf_counter
        self._metrics: Dict[str, Metric] = {}
        self._lock = threading.Lock()
        self._closed = False

    def _log(self, message: str, warning: bool = False) -> None:
        try:
            if self.logger:
                fn = getattr(self.logger, "warn" if warning else "info", self.logger)
                fn(message)
        except Exception:
            pass

    def record(self, metric: str, duration_ms: float, **context) -> None:
        try:
            if not self.enabled or self._closed:
                return
            value = max(0.0, float(duration_ms))
            with self._lock:
                data = self._metrics.get(metric)
                if data is None:
                    data = self._metrics[metric] = Metric(deque(maxlen=self.sample_limit))
                data.samples.append(value)
                data.count += 1
                data.total_ms += value
                data.minimum_ms = min(data.minimum_ms, value)
                data.maximum_ms = max(data.maximum_ms, value)
            if value >= self.slow_threshold_ms:
                self._log(f"[PERF] SLOW metric={metric} duration_ms={value:.1f}" +
                          _safe_context(context), warning=True)
        except Exception as exc:
            self._log(f"[PERF] ERROR operation=record detail={type(exc).__name__}", warning=True)

    def record_since(self, metric: str, started: float, **context) -> None:
        try:
            self.record(metric, (self.clock() - started) * 1000.0, **context)
        except Exception:
            pass

    @contextmanager
    def measure(self, metric: str, **context):
        if not self.enabled or self._closed:
            yield
            return
        try:
            started = self.clock()
        except Exception:
            yield
            return
        try:
            yield
        finally:
            self.record_since(metric, started, **context)

    @staticmethod
    def percentile(samples: Iterable[float], percent: float) -> float:
        values = sorted(float(v) for v in samples)
        if not values:
            return 0.0
        # Nearest-rank is deterministic and does not invent sampled values.
        rank = max(1, math.ceil((float(percent) / 100.0) * len(values)))
        return values[min(rank, len(values)) - 1]

    def snapshot(self) -> dict:
        try:
            with self._lock:
                return {name: {
                    "count": m.count, "total_ms": m.total_ms,
                    "min_ms": 0.0 if m.count == 0 else m.minimum_ms,
                    "max_ms": m.maximum_ms, "samples": list(m.samples),
                } for name, m in self._metrics.items()}
        except Exception:
            return {}

    def summarize(self, window_seconds: int = 60, **resources) -> list[str]:
        lines = []
        try:
            snapshot = self.snapshot()
            for name, data in sorted(snapshot.items()):
                samples = data["samples"]
                if not samples:
                    continue
                avg = data["total_ms"] / data["count"]
                line = (f"[PERF] SUMMARY scope=cumulative window_hint={int(window_seconds)}s "
                        f"metric={name} count={data['count']} avg_ms={avg:.1f} "
                        f"p50_ms={self.percentile(samples, 50):.1f} "
                        f"p95_ms={self.percentile(samples, 95):.1f} "
                        f"p99_ms={self.percentile(samples, 99):.1f} "
                        f"max_ms={data['max_ms']:.1f}" + _safe_context(resources))
                lines.append(line)
                self._log(line)
        except Exception:
            pass
        return lines

    @property
    def active_metric_count(self) -> int:
        return len(self._metrics)

    def shutdown(self) -> None:
        self._closed = True


class EventLoopStallDetector:
    """Pure heartbeat calculator used by the Dashboard's UI-thread QTimer.

    Stall is callback elapsed time minus the configured expected interval.
    """
    def __init__(self, telemetry: PerformanceTelemetry, interval_ms=500,
                 threshold_ms=1000, clock=None):
        self.telemetry = telemetry
        self.interval_ms = float(interval_ms)
        self.threshold_ms = float(threshold_ms)
        self.clock = clock or time.perf_counter
        # Construction can happen well before Qt starts dispatching events.
        # The first callback therefore establishes the runtime baseline rather
        # than comparing event-loop startup time with the heartbeat interval.
        self._last = None
        self._closed = False

    def tick(self) -> float:
        if self._closed:
            return 0.0
        now = self.clock()
        if self._last is None:
            self._last = now
            return 0.0
        stall = max(0.0, (now - self._last) * 1000.0 - self.interval_ms)
        self._last = now
        if stall >= self.threshold_ms:
            self.telemetry.record("ui.event_loop_stall", stall,
                                  threshold_ms=int(self.threshold_ms))
            self.telemetry._log(f"[PERF] UI_STALL duration_ms={stall:.1f} "
                                f"threshold_ms={self.threshold_ms:.0f}", warning=True)
        return stall

    def shutdown(self) -> None:
        self._closed = True


_telemetry = PerformanceTelemetry(enabled=False)


def get_telemetry() -> PerformanceTelemetry:
    return _telemetry


def configure(config: dict, logger=None) -> PerformanceTelemetry:
    global _telemetry
    try:
        _telemetry = PerformanceTelemetry(
            enabled=config.get("performance_telemetry_enabled", True),
            slow_threshold_ms=config.get("performance_slow_threshold_ms", 1000),
            sample_limit=config.get("performance_sample_limit", 512), logger=logger)
    except Exception:
        _telemetry = PerformanceTelemetry(enabled=False, logger=logger)
    return _telemetry


def timed(metric: str, context: Optional[Callable] = None):
    """Fail-open timing decorator; preserves return values and exceptions."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            telemetry = get_telemetry()
            details = {}
            should_measure = True
            try:
                if context:
                    resolved = context(*args, **kwargs)
                    should_measure = resolved is not None
                    details = resolved or {}
            except Exception:
                details = {}
            if not should_measure:
                return fn(*args, **kwargs)
            with telemetry.measure(metric, **details):
                return fn(*args, **kwargs)
        return wrapped
    return decorate
