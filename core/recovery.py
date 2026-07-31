"""
Production hardening v1.2.0 — Recovery utilities.

Category B (Infrastructure). Wraps the frozen production engine without
touching it. Two public entry points:

    retry_with_ladder(callable, ...)
        Executes a callable with the fixed retry ladder mandated by
        Production Hardening v1.2 B-1:  5s -> 10s -> 20s -> 40s -> 60s.
        The ladder is fixed on purpose so behaviour is auditable.

    safe_run(callable, ...)
        Catches any exception, logs a full stack trace with timestamp /
        module / recovery action (Production Hardening v1.2 B-6), and
        returns a sentinel instead of propagating the failure. Used for
        the health watchdog and background maintenance so a bug in one
        of them never terminates monitoring.

Both helpers are pure Python; no Qt dependency, so they can be unit
tested without a display.
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime
from typing import Any, Callable, Iterable, Optional, Tuple, Type

# Fixed retry ladder — do NOT tune per-call. Deviating from this ladder
# would be a business-visible change (operator sees different delays in
# the log). Kept constant on purpose.
DEFAULT_LADDER: Tuple[int, ...] = (5, 10, 20, 40, 60)


class RetryExhausted(RuntimeError):
    """Raised when every step of the retry ladder failed."""

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(
            f"Retry ladder exhausted after {attempts} attempts: "
            f"{type(last_error).__name__}: {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


def retry_with_ladder(
    fn: Callable[..., Any],
    *args: Any,
    ladder: Iterable[int] = DEFAULT_LADDER,
    retry_on: Tuple[Type[BaseException], ...] = (Exception,),
    on_retry: Optional[Callable[[int, int, BaseException], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """Call `fn(*args, **kwargs)` with the v1.2 B-1 retry ladder.

    Sleeps between attempts follow the ladder. After the LAST ladder
    step also fails, `RetryExhausted` is raised with the last exception
    attached (never a bare `except:` — callers can distinguish between
    "temporary Google blip" and "hard failure").

    `on_retry(attempt_index, delay_sec, error)` is called after every
    failed attempt that is going to be retried. Never called for the
    final failure. Kept optional so tests can inspect the ladder.
    """
    delays = tuple(int(d) for d in ladder)
    last_exc: Optional[BaseException] = None
    total = len(delays) + 1  # first attempt + one per ladder step
    for attempt in range(total):
        try:
            return fn(*args, **kwargs)
        except retry_on as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= len(delays):
                break
            delay = delays[attempt]
            if on_retry is not None:
                try:
                    on_retry(attempt + 1, delay, exc)
                except Exception:  # pragma: no cover - callback must never crash
                    pass
            if delay > 0:
                sleep(delay)
    assert last_exc is not None
    raise RetryExhausted(total, last_exc)


def safe_run(
    fn: Callable[..., Any],
    *args: Any,
    module: str = "",
    recovery_action: str = "continue",
    logger: Any = None,
    default: Any = None,
    **kwargs: Any,
) -> Any:
    """Run `fn` and swallow every exception (Production Hardening B-6).

    On failure, logs the full stack trace with:
      * timestamp
      * module (caller-supplied — keeps the log clean; we don't guess)
      * exception type + message
      * recovery action

    Returns `default` on failure, and the function's return value
    otherwise. `logger` accepts anything with `.warn` / `.error` methods
    (the app's AppLogger fits). When `logger` is None we fall back to
    printing to stderr so the message is at least visible in a portable
    console build.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        ts = datetime.now().isoformat(timespec="seconds")
        stack = traceback.format_exc()
        header = f"[{ts}] {module or fn.__module__} :: {type(exc).__name__}: {exc}"
        body = f"recovery_action={recovery_action}\n{stack}"
        if logger is not None and hasattr(logger, "error"):
            try:
                logger.error(header)
                logger.error(body.rstrip())
            except Exception:  # pragma: no cover
                pass
        else:  # pragma: no cover - fallback for smoke tests
            import sys

            print(header, file=sys.stderr)
            print(body, file=sys.stderr)
        return default
