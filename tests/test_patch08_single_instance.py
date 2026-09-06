"""PATCH-08 product-wide single-instance ownership regression tests."""
from __future__ import annotations

import importlib
import multiprocessing
import os
import sys
import types
import uuid
from unittest.mock import Mock

import pytest

from core.single_instance import (
    PRODUCT_KEY, WINDOWS_MUTEX_NAME, InstanceAlreadyRunning,
    SingleInstanceError, SingleInstanceGuard,
)


def _key() -> str:
    return f"{PRODUCT_KEY}.test.{uuid.uuid4().hex}"


def _lock_child(key: str, connection, explicit_release: bool) -> None:
    guard = SingleInstanceGuard.acquire(key)
    connection.send("owned")
    connection.recv()
    if explicit_release:
        guard.release()


def test_basic_ownership_conflict_different_key_and_idempotent_release():
    key = _key()
    first = SingleInstanceGuard.acquire(key)
    assert first.owns_lock
    with pytest.raises(InstanceAlreadyRunning):
        SingleInstanceGuard.acquire(key)
    other = SingleInstanceGuard.acquire(key + ".other")
    other.release()
    first.release()
    first.release()
    assert not first.owns_lock
    replacement = SingleInstanceGuard.acquire(key)
    replacement.release()


def test_context_manager_releases():
    key = _key()
    with SingleInstanceGuard.acquire(key) as guard:
        assert guard.owns_lock
    with SingleInstanceGuard.acquire(key) as replacement:
        assert replacement.owns_lock


def test_process_normal_exit_releases_without_sleep():
    key = _key()
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=_lock_child, args=(key, child, True))
    process.start()
    assert parent.recv() == "owned"
    with pytest.raises(InstanceAlreadyRunning):
        SingleInstanceGuard.acquire(key)
    parent.send("exit")
    process.join(timeout=10)
    assert process.exitcode == 0
    SingleInstanceGuard.acquire(key).release()


def test_process_termination_releases_os_ownership_without_cleanup():
    key = _key()
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=_lock_child, args=(key, child, False))
    process.start()
    assert parent.recv() == "owned"
    with pytest.raises(InstanceAlreadyRunning):
        SingleInstanceGuard.acquire(key)
    process.terminate()
    process.join(timeout=10)
    assert not process.is_alive()
    SingleInstanceGuard.acquire(key).release()


def _import_main(monkeypatch):
    """Import the entry point without loading native Qt/Chromium dependencies."""
    widgets = types.ModuleType("PySide6.QtWidgets")
    widgets.QApplication = Mock()
    widgets.QMessageBox = Mock()
    pyside = types.ModuleType("PySide6")
    pyside.QtWidgets = widgets
    dashboard = types.ModuleType("ui.dashboard")
    dashboard.Dashboard = Mock()
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", widgets)
    monkeypatch.setitem(sys.modules, "ui.dashboard", dashboard)
    sys.modules.pop("main", None)
    return importlib.import_module("main")


def _patch_qt(monkeypatch, main_module):
    app = Mock()
    monkeypatch.setattr(main_module, "QApplication", Mock(return_value=app))
    monkeypatch.setattr(main_module.QMessageBox, "critical", Mock())
    return app


def test_main_duplicate_rejected_before_every_mutable_startup_step(monkeypatch, tmp_path):
    main = _import_main(monkeypatch)

    app = _patch_qt(monkeypatch, main)
    prepare = Mock()
    database = Mock()
    dashboard = Mock()
    diagnostics = Mock()
    dirty = Mock()
    monkeypatch.setattr(main, "_prepare_startup", prepare)
    monkeypatch.setattr(main, "DatabaseService", database)
    monkeypatch.setattr(main, "Dashboard", dashboard)
    monkeypatch.setattr(main, "run_diagnostics", diagnostics)
    monkeypatch.setattr(main.CrashStateStore, "mark_dirty", dirty)
    monkeypatch.setattr(
        main.SingleInstanceGuard, "acquire",
        Mock(side_effect=InstanceAlreadyRunning("held")),
    )

    assert main.main() == main.DUPLICATE_INSTANCE_EXIT_CODE == 4
    prepare.assert_not_called()
    database.assert_not_called()
    dirty.assert_not_called()
    diagnostics.assert_not_called()
    dashboard.assert_not_called()
    app.exec.assert_not_called()
    title, message = main.QMessageBox.critical.call_args.args[1:]
    assert title == "Bonus Reload Automation"
    assert "already running" in message
    assert "Close the existing instance" in message
    assert not (tmp_path / "processed.db").exists()


def test_unexpected_lock_failure_fails_closed(monkeypatch):
    main = _import_main(monkeypatch)

    app = _patch_qt(monkeypatch, main)
    prepare = Mock()
    monkeypatch.setattr(main, "_prepare_startup", prepare)
    monkeypatch.setattr(
        main.SingleInstanceGuard, "acquire",
        Mock(side_effect=SingleInstanceError("simulated OS failure")),
    )
    assert main.main() == main.SINGLE_INSTANCE_FAILURE_EXIT_CODE == 5
    prepare.assert_not_called()
    app.exec.assert_not_called()
    message = main.QMessageBox.critical.call_args.args[2]
    assert "Startup stopped for safety" in message
    assert "simulated OS failure" in message


def test_main_releases_guard_after_owned_runner_on_return_and_exception(monkeypatch):
    main = _import_main(monkeypatch)

    _patch_qt(monkeypatch, main)
    guard = Mock()
    monkeypatch.setattr(main.SingleInstanceGuard, "acquire", Mock(return_value=guard))
    monkeypatch.setattr(main, "_run_owned", Mock(return_value=17))
    assert main.main() == 17
    guard.release.assert_called_once_with()

    guard.reset_mock()
    main._run_owned.side_effect = RuntimeError("unwind")
    with pytest.raises(RuntimeError, match="unwind"):
        main.main()
    guard.release.assert_called_once_with()


@pytest.mark.parametrize("exit_code", [1, 2, 3])
def test_all_owned_startup_error_returns_release_guard(monkeypatch, exit_code):
    """Covers runtime preparation, database open, and layout-init return paths."""
    main = _import_main(monkeypatch)

    _patch_qt(monkeypatch, main)
    guard = Mock()
    monkeypatch.setattr(main.SingleInstanceGuard, "acquire", Mock(return_value=guard))
    monkeypatch.setattr(main, "_run_owned", Mock(return_value=exit_code))
    assert main.main() == exit_code
    guard.release.assert_called_once_with()


def test_production_identity_is_stable_and_not_path_or_environment_derived(monkeypatch, tmp_path):
    original = WINDOWS_MUTEX_NAME
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BONUS_RELOAD_DATA_DIR", str(tmp_path / "elsewhere"))
    from core.single_instance import WINDOWS_MUTEX_NAME as after

    assert PRODUCT_KEY == "BonusReloadBotAutoAdjustV1"
    assert after == original == r"Local\BonusReloadBotAutoAdjustV1.SingleInstance"
    assert str(tmp_path) not in after
    assert os.environ["BONUS_RELOAD_DATA_DIR"] not in after


def _configure_integrated_lifecycle(monkeypatch, *, failure=None):
    """Configure actual main/_run_owned execution with lightweight ordered fakes."""
    from pathlib import Path
    from types import SimpleNamespace

    main = _import_main(monkeypatch)
    events = []

    class Guard:
        owns_lock = True

        def release(self):
            assert self.owns_lock
            events.append("guard-release")
            self.owns_lock = False

    guard = Guard()

    class Database:
        def __init__(self, path):
            events.append("db-open")

        def total_count(self):
            return 0

        def close(self):
            assert guard.owns_lock
            events.append("db-close")

    class CrashStore:
        def load(self):
            return SimpleNamespace(clean_exit=True, saved_at=None)

        def mark_dirty(self, **kwargs):
            events.append("crash-dirty")

        def mark_clean_exit(self):
            events.append("crash-clean")

    runtime_paths = SimpleNamespace(
        data_dir=Path("/data"), config_path=Path("/data/config.json"),
        selectors_path=Path("/data/selectors.json"),
        crash_state_path=Path("/data/crash_state.json"), logs_dir=Path("/data/logs"),
        screenshots_dir=Path("/data/screenshots"),
    )
    runtime = SimpleNamespace(
        credentials_path=Path("/data/credentials.json"),
        database_path=Path("/data/processed.db"),
        browser_profile_path=Path("/data/browser-profile"),
    )
    logger = Mock(file_handler_ok=True, file_handler_error=None)
    monkeypatch.setattr(main, "_prepare_startup", Mock(
        side_effect=lambda: (events.append("runtime-prepare") or (
            runtime_paths, {"hardening": {"auto_startup_maintenance": False}}, {}, runtime
        ))
    ))
    monkeypatch.setattr(main, "_install_uncaught_exception_handler", Mock())
    monkeypatch.setattr(main, "AppLogger", Mock())
    main.AppLogger.get.return_value = logger
    monkeypatch.setattr(main, "CrashStateStore", Mock(return_value=CrashStore()))
    monkeypatch.setattr(main, "DatabaseService", Database)
    monkeypatch.setattr(main, "mark_initialized", Mock())
    diagnostic = Mock(all_ok=True)
    diagnostic.summary.return_value = ""
    monkeypatch.setattr(main, "run_diagnostics", Mock(return_value=diagnostic))
    monkeypatch.setattr(main.SingleInstanceGuard, "acquire", Mock(
        side_effect=lambda: (events.append("lock-acquired") or guard)
    ))
    monkeypatch.setattr(main.QMessageBox, "critical", Mock())

    app = Mock()
    monkeypatch.setattr(main, "QApplication", Mock(return_value=app))

    def dashboard(*args, **kwargs):
        if failure == "dashboard":
            events.append("dashboard-error")
            raise RuntimeError("dashboard exploded")
        events.append("dashboard")
        window = Mock()
        window.show.side_effect = lambda: events.append("window-show")
        return window

    monkeypatch.setattr(main, "Dashboard", dashboard)

    def app_exec():
        events.append("app-exec")
        if failure == "app-exec":
            raise RuntimeError("event loop exploded")
        return 23

    app.exec.side_effect = app_exec
    return main, app, guard, events


def test_integrated_normal_shutdown_orders_db_clean_and_guard_release(monkeypatch):
    main, _app, guard, events = _configure_integrated_lifecycle(monkeypatch)

    assert main.main() == 23
    assert events == [
        "lock-acquired", "runtime-prepare", "crash-dirty", "db-open",
        "dashboard", "window-show", "app-exec", "db-close", "crash-clean",
        "guard-release",
    ]
    assert events.index("db-close") < events.index("crash-clean") < events.index("guard-release")
    assert not guard.owns_lock


def test_integrated_dashboard_exception_closes_db_before_releasing_guard(monkeypatch):
    main, _app, guard, events = _configure_integrated_lifecycle(
        monkeypatch, failure="dashboard",
    )

    with pytest.raises(RuntimeError, match="dashboard exploded"):
        main.main()
    assert events[-4:] == ["db-open", "dashboard-error", "db-close", "guard-release"]
    assert "crash-clean" not in events
    assert not guard.owns_lock


def test_integrated_app_exec_exception_closes_db_and_leaves_crash_dirty(monkeypatch):
    main, _app, guard, events = _configure_integrated_lifecycle(
        monkeypatch, failure="app-exec",
    )

    with pytest.raises(RuntimeError, match="event loop exploded"):
        main.main()
    assert events[-4:] == ["window-show", "app-exec", "db-close", "guard-release"]
    assert "crash-clean" not in events
    assert not guard.owns_lock


def test_integrated_mark_initialized_failure_closes_db_before_lock_release(monkeypatch):
    main, _app, guard, events = _configure_integrated_lifecycle(monkeypatch)
    main.mark_initialized.side_effect = main.RuntimeLayoutError("layout failed")

    assert main.main() == 3
    assert events[-2:] == ["db-close", "guard-release"]
    assert "dashboard" not in events
    assert "crash-clean" not in events
    main.QMessageBox.critical.assert_called_once()
    assert not guard.owns_lock
