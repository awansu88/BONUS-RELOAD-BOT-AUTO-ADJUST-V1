"""
v1.2.0 Production Hardening — Recovery utilities tests.

Category: B-1 (Google Sheets Auto Reconnect) + B-6 (Graceful Error
Recovery).

The retry ladder is fixed by contract at (5, 10, 20, 40, 60) — these
tests pin that contract so a future refactor cannot silently change
the retry behaviour operators rely on.
"""

from __future__ import annotations

import io
import sys

import pytest

from core.recovery import (
    DEFAULT_LADDER,
    RetryExhausted,
    retry_with_ladder,
    safe_run,
)


def test_default_ladder_matches_contract():
    assert DEFAULT_LADDER == (5, 10, 20, 40, 60)


def test_retry_ladder_succeeds_on_first_attempt():
    def _ok():
        return "hello"

    slept = []
    result = retry_with_ladder(_ok, sleep=slept.append)
    assert result == "hello"
    assert slept == []


def test_retry_ladder_succeeds_after_two_failures():
    calls = {"n": 0}
    slept = []
    events = []

    def _fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError(f"transient #{calls['n']}")
        return "ok"

    def _on_retry(attempt, delay, exc):
        events.append((attempt, delay, type(exc).__name__))

    result = retry_with_ladder(_fn, on_retry=_on_retry, sleep=slept.append)
    assert result == "ok"
    # 2 failures -> 2 sleeps using ladder[0], ladder[1]
    assert slept == [5, 10]
    assert events == [(1, 5, "ConnectionError"), (2, 10, "ConnectionError")]


def test_retry_ladder_exhausts_after_full_ladder():
    calls = {"n": 0}
    slept = []

    def _always_fail():
        calls["n"] += 1
        raise TimeoutError("nope")

    with pytest.raises(RetryExhausted) as excinfo:
        retry_with_ladder(_always_fail, sleep=slept.append)
    # 1 initial + 5 retries = 6 total attempts, 5 sleeps
    assert calls["n"] == 6
    assert slept == [5, 10, 20, 40, 60]
    assert isinstance(excinfo.value.last_error, TimeoutError)
    assert excinfo.value.attempts == 6


def test_retry_ladder_only_retries_selected_exceptions():
    calls = {"n": 0}

    class Fatal(RuntimeError):
        pass

    def _fn():
        calls["n"] += 1
        raise Fatal("stop")

    with pytest.raises(Fatal):
        # retry_on excludes Fatal so the ladder does not swallow it
        retry_with_ladder(
            _fn,
            retry_on=(ValueError,),
            sleep=lambda *_: None,
        )
    assert calls["n"] == 1


def test_retry_ladder_accepts_custom_ladder():
    slept = []
    with pytest.raises(RetryExhausted):
        retry_with_ladder(
            lambda: (_ for _ in ()).throw(IOError("x")),
            ladder=(1, 2),
            sleep=slept.append,
        )
    assert slept == [1, 2]


def test_on_retry_callback_never_breaks_ladder():
    slept = []
    attempts = {"n": 0}

    def _bad_callback(*_):
        raise RuntimeError("callback broken")

    def _fn():
        attempts["n"] += 1
        raise ConnectionError("blip")

    with pytest.raises(RetryExhausted):
        retry_with_ladder(
            _fn, ladder=(1, 1), on_retry=_bad_callback, sleep=slept.append
        )
    # Ladder still ran to completion even with a broken callback.
    assert slept == [1, 1]
    assert attempts["n"] == 3  # 1 initial + 2 retries


class _Logger:
    def __init__(self):
        self.errors = []

    def error(self, msg):
        self.errors.append(msg)


def test_safe_run_returns_default_and_logs_stack_trace():
    log = _Logger()
    def _boom():
        raise ValueError("kaboom")

    result = safe_run(
        _boom, module="unit-test", recovery_action="continue",
        logger=log, default="fallback",
    )
    assert result == "fallback"
    assert len(log.errors) == 2
    header, body = log.errors
    assert "ValueError: kaboom" in header
    assert "unit-test" in header
    assert "recovery_action=continue" in body
    assert "Traceback" in body


def test_safe_run_returns_value_on_success_and_does_not_log():
    log = _Logger()
    assert safe_run(lambda x: x + 1, 41, logger=log) == 42
    assert log.errors == []


def test_safe_run_prints_to_stderr_when_no_logger(capsys):
    result = safe_run(
        lambda: (_ for _ in ()).throw(RuntimeError("plain")),
        module="no-logger",
    )
    assert result is None
    captured = capsys.readouterr()
    assert "RuntimeError: plain" in captured.err
    assert "no-logger" in captured.err
