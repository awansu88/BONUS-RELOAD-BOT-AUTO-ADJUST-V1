"""OS-owned, product-wide single-instance process ownership.

The lock is deliberately independent of the install and data directories.  Windows
uses a named kernel mutex; POSIX uses ``flock`` in the operating-system temp area so
source runs and tests exercise the same lifetime semantics without a PID file.
"""

from __future__ import annotations

import errno
import hashlib
import os
import sys
import tempfile
from pathlib import Path


PRODUCT_KEY = "BonusReloadBotAutoAdjustV1"
# ``Local`` is session-scoped, works for ordinary non-admin users, and avoids the
# privileges/security policy complications of Windows' machine-wide namespace.
WINDOWS_MUTEX_NAME = rf"Local\{PRODUCT_KEY}.SingleInstance"
ERROR_ALREADY_EXISTS = 183


class SingleInstanceError(RuntimeError):
    """The application could not safely establish process ownership."""


class InstanceAlreadyRunning(SingleInstanceError):
    """Another process already holds the product ownership lock."""


class SingleInstanceGuard:
    """Retain an OS lock until :meth:`release` or process termination."""

    def __init__(self, *, handle=None, file=None) -> None:  # type: ignore[no-untyped-def]
        self._handle = handle
        self._file = file
        self._owned = True

    @property
    def owns_lock(self) -> bool:
        return self._owned

    @classmethod
    def acquire(cls, product_key: str = PRODUCT_KEY) -> "SingleInstanceGuard":
        if not product_key:
            raise ValueError("product_key must not be empty")
        if sys.platform == "win32":
            return cls._acquire_windows(product_key)
        return cls._acquire_posix(product_key)

    @classmethod
    def _acquire_windows(cls, product_key: str) -> "SingleInstanceGuard":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        create_mutex.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        name = WINDOWS_MUTEX_NAME if product_key == PRODUCT_KEY else rf"Local\{product_key}.SingleInstance"
        ctypes.set_last_error(0)
        handle = create_mutex(None, True, name)
        error = ctypes.get_last_error()
        if not handle:
            raise SingleInstanceError(
                f"CreateMutexW failed: {ctypes.FormatError(error).strip()} (error {error})"
            )
        if error == ERROR_ALREADY_EXISTS:
            close_handle(handle)
            raise InstanceAlreadyRunning("application mutex already exists")
        return cls(handle=handle)

    @classmethod
    def _acquire_posix(cls, product_key: str) -> "SingleInstanceGuard":
        try:
            import fcntl

            digest = hashlib.sha256(product_key.encode("utf-8")).hexdigest()
            lock_dir = Path(tempfile.gettempdir()) / "bonus-reload-single-instance"
            lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            file = (lock_dir / f"{digest}.lock").open("a+b")
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                file.close()
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise InstanceAlreadyRunning("application lock is already held") from exc
                raise SingleInstanceError(f"flock failed: {exc}") from exc
            return cls(file=file)
        except InstanceAlreadyRunning:
            raise
        except SingleInstanceError:
            raise
        except Exception as exc:
            raise SingleInstanceError(f"could not create application lock: {exc}") from exc

    def release(self) -> None:
        """Release and close the OS resource; repeated calls are harmless."""
        if not self._owned:
            return
        self._owned = False
        if self._handle is not None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._file is not None:
            # Closing the descriptor releases flock; the file's existence is never
            # used as evidence of ownership and therefore cannot become stale.
            self._file.close()
            self._file = None

    def __enter__(self) -> "SingleInstanceGuard":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:  # type: ignore[no-untyped-def]
        self.release()

