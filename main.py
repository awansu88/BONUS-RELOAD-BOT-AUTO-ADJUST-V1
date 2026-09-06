"""
Bonus Reload Automation - Entry point.

Persistent-runtime aware:

    * When frozen by PyInstaller (--onedir), `sys._MEIPASS` points at the
      `_internal/` folder that ships next to the .exe. Read-only bundled
      resources (Playwright Chromium + driver, config templates) live there.
    * APP_DIR is the disposable installation folder.  Frozen writable state
      lives in DATA_DIR (normally ``%LOCALAPPDATA%\\BonusReloadBot``).
    * Source runs intentionally default DATA_DIR to the project root.

Playwright is redirected to the bundled Chromium via
`PLAYWRIGHT_BROWSERS_PATH` before *any* Playwright import so the app
never touches `%LOCALAPPDATA%\\ms-playwright`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# =============================================================================
# Portable-mode helpers  (must run BEFORE importing Playwright/Qt)
# =============================================================================
def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _resource_dir() -> Path:
    """Directory holding *read-only* bundled resources."""
    if _is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)              # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def _app_dir() -> Path:
    """Installation directory sitting next to the executable."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


RESOURCE_DIR = _resource_dir()
APP_DIR = _app_dir()

from core.runtime_paths import (  # noqa: E402
    RuntimeLayoutError, mark_initialized, prepare_config, prepare_runtime,
    resolve_runtime_paths,
)
from core.single_instance import (  # noqa: E402
    InstanceAlreadyRunning, SingleInstanceError, SingleInstanceGuard,
)


def _resolve_startup_paths(environ=None):  # type: ignore[no-untyped-def]
    """Resolve writable paths only inside the controlled startup boundary."""
    return resolve_runtime_paths(
        app_dir=APP_DIR, resource_dir=RESOURCE_DIR, frozen=_is_frozen(),
        environ=environ,
    )


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _prepare_startup(environ=None):  # type: ignore[no-untyped-def]
    """Prepare persistent state; callers provide the UI error boundary."""
    runtime_paths = _resolve_startup_paths(environ)
    prepare_config(runtime_paths)
    config = _load_json(runtime_paths.config_path)
    selectors = _load_json(runtime_paths.selectors_path)
    runtime = prepare_runtime(runtime_paths, config)
    return runtime_paths, config, selectors, runtime

def _prime_playwright_env() -> None:
    """Point Playwright at the bundled Chromium."""
    bundled = RESOURCE_DIR / "pw-browsers"
    if bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
    # Prevent Playwright from ever trying to download Chromium at runtime.
    os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")


# Run before the Playwright/Qt imports.
_prime_playwright_env()


# =============================================================================
# Now the app can import Playwright/Qt safely.
# =============================================================================
from PySide6.QtWidgets import QApplication, QMessageBox     # noqa: E402

from core.database import DatabaseService                    # noqa: E402
from core.diagnostics import run_diagnostics                 # noqa: E402
from core.logger import AppLogger                            # noqa: E402
from core.crash_state import CrashState, CrashStateStore     # noqa: E402
from core.recovery import safe_run                           # noqa: E402
from core.maintenance import MaintenanceService              # noqa: E402
from ui.dashboard import Dashboard                           # noqa: E402


def _install_uncaught_exception_handler(logger: AppLogger) -> None:
    """B-6: log every un-caught exception before Python's default hook.

    This never suppresses the exception — it only adds a timestamped
    entry with module + stack trace to the app log so post-mortem
    debugging on the operator's machine has full context.
    """
    original_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        try:
            import traceback as _tb

            stack = "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
            logger.error(
                f"UNCAUGHT {exc_type.__name__}: {exc_value}"
            )
            logger.error(stack.rstrip())
        except Exception:  # pragma: no cover
            pass
        try:
            original_hook(exc_type, exc_value, exc_tb)
        except Exception:  # pragma: no cover
            pass

    sys.excepthook = _hook


def _run_owned(app: QApplication) -> int:
    """Run startup and the UI while the caller retains process ownership."""
    try:
        runtime_paths, config, selectors, runtime = _prepare_startup()
    except Exception as exc:
        QMessageBox.critical(
            None, "Persistent runtime startup error",
            f"Persistent runtime preparation failed.\n\n{exc}",
        )
        return 1

    data_dir = runtime_paths.data_dir
    config_path = runtime_paths.config_path
    crash_state_path = runtime_paths.crash_state_path
    cred_path = runtime.credentials_path
    db_path = runtime.database_path
    profile_path = runtime.browser_profile_path

    AppLogger.get(log_dir=str(runtime_paths.logs_dir))
    logger = AppLogger.get()
    logger.info(f"Application started ({config.get('version', 'v1.0.0')})")
    logger.info(
        f"Portable mode: {'frozen' if _is_frozen() else 'source'} "
        f"| app={APP_DIR} | res={RESOURCE_DIR} | data={data_dir}"
    )

    # v1.2 B-6: capture every uncaught exception before Python's default
    # hook so post-mortem debugging always has a stack trace in the app log.
    _install_uncaught_exception_handler(logger)

    # v1.2 B-7 / B-8: crash-state store — marks the process as running,
    # remembers the URL / window geometry across restarts.
    crash_store = CrashStateStore(crash_state_path)
    previous_state = crash_store.load()
    crash_store.mark_dirty(version=str(config.get("version", "")))
    if not previous_state.clean_exit and previous_state.saved_at:
        logger.warn(
            f"Previous session did NOT exit cleanly "
            f"(last saved at {previous_state.saved_at}) — recovering."
        )

    try:
        db = DatabaseService(str(db_path))
    except Exception as exc:
        QMessageBox.critical(None, "SQLite error", f"Could not open database:\n{db_path}\n\n{exc}")
        return 2

    try:
        mark_initialized(runtime_paths, db_path)
    except RuntimeLayoutError as exc:
        db.close()
        QMessageBox.critical(None, "Runtime layout error", str(exc))
        return 3

    # Diagnostics run only after migration policy and DatabaseService have
    # safely selected/opened the authoritative database.
    try:
        diag = run_diagnostics(
            app_dir=data_dir, resource_dir=RESOURCE_DIR,
            config_path=config_path, selectors_path=runtime_paths.selectors_path,
            credentials_path=cred_path, sqlite_path=db_path,
            logs_dir=runtime_paths.logs_dir,
            screenshots_dir=runtime_paths.screenshots_dir,
            browser_profile_dir=profile_path,
            logger_file_handler_ok=logger.file_handler_ok,
            logger_file_handler_error=logger.file_handler_error,
        )
        for line in diag.summary().splitlines():
            (logger.info if diag.all_ok else logger.warn)(line)
    except Exception as exc:
        logger.warn(f"Startup diagnostics failed: {exc}")

    AppLogger.get().info(f"SQLite ready: {db_path.name} ({db.total_count():,} rows)")

    # v1.2 C-3: automatic startup maintenance (checkpoint + optimize).
    # Never blocking, never VACUUM, never interrupts monitoring.
    if bool(config.get("hardening", {}).get("auto_startup_maintenance", True)):
        maintenance_startup = MaintenanceService(
            db=db,
            logs_dir=runtime_paths.logs_dir,
            screenshots_dir=runtime_paths.screenshots_dir,
        )
        report = safe_run(
            maintenance_startup.startup_maintenance,
            module="startup_maintenance",
            recovery_action="continue without startup optimize",
            logger=logger,
        )
        if report is not None:
            for line in report.summary().splitlines():
                logger.info(line)

    window = Dashboard(
        config=config, selectors=selectors,
        config_path=config_path, db=db,
        app_dir=data_dir, resource_dir=RESOURCE_DIR,
        credentials_path=cred_path,
        crash_store=crash_store, previous_state=previous_state,
    )
    window.show()
    exit_code = app.exec()
    db.close()

    # v1.2 B-7: graceful shutdown checkpoint. Flushing the crash-state
    # file is what tells the NEXT launch this was a clean exit.
    try:
        crash_store.mark_clean_exit()
    except Exception:
        pass
    return exit_code


DUPLICATE_INSTANCE_EXIT_CODE = 4
SINGLE_INSTANCE_FAILURE_EXIT_CODE = 5


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Bonus Reload Automation")

    # This product-wide lock intentionally precedes _prepare_startup: a rejected
    # process must not migrate runtime data, dirty crash state, or open SQLite.
    try:
        guard = SingleInstanceGuard.acquire()
    except InstanceAlreadyRunning:
        QMessageBox.critical(
            None,
            "Bonus Reload Automation",
            "Bonus Reload Automation is already running.\n\n"
            "Close the existing instance before starting another.",
        )
        return DUPLICATE_INSTANCE_EXIT_CODE
    except SingleInstanceError as exc:
        QMessageBox.critical(
            None,
            "Bonus Reload Automation",
            "Could not establish single-instance ownership.\n"
            "Startup stopped for safety.\n\n"
            f"{exc}",
        )
        return SINGLE_INSTANCE_FAILURE_EXIT_CODE

    try:
        return _run_owned(app)
    finally:
        # _run_owned closes the database and marks the crash state clean before
        # returning.  All early returns and Python exception unwinding land here.
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
