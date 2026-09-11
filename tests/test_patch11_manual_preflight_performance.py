"""PATCH-11 regressions for Manual Bonus payload and freshness accounting."""

from __future__ import annotations

import ast
import time
from pathlib import Path

from core.memory_cache import MemoryCache
from core.sheet_service import SheetService


def _dashboard_methods():
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    dashboard = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "Dashboard")
    wanted = {"_install_manual_snapshot", "_refresh_manual_list_now"}
    methods = [node for node in dashboard.body
               if isinstance(node, ast.FunctionDef) and node.name in wanted]
    for method in methods:
        method.decorator_list = []
    namespace = {"time": time}
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
                 "ui/dashboard.py", "exec"), namespace)
    return namespace


_METHODS = _dashboard_methods()


def _sheet_config():
    return {
        "columns": {},
        "required_headers": {},
        "sheet_names": {"master": "MASTER", "manual_bonus_reload": "MANUAL"},
    }


class ColumnOnlyWorksheet:
    def __init__(self, values):
        self.values = values
        self.column_reads = []

    def col_values(self, column):
        self.column_reads.append(column)
        return self.values

    def get_all_values(self):
        raise AssertionError("Manual Bonus must not fetch the full worksheet")


def test_manual_set_uses_one_column_b_read_and_cleans_user_ids():
    worksheet = ColumnOnlyWorksheet(["USER ID", " alice ", "", "  ", "bob"])
    service = SheetService("unused", _sheet_config())
    service._manual = worksheet

    assert service.read_manual_set() == {"alice", "bob"}
    assert worksheet.column_reads == [2]


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warn(self, message):
        self.warnings.append(message)


class FakeSheet:
    is_connected = True

    def __init__(self, clock, snapshots=None, error=None):
        self.clock = clock
        self.snapshots = list(snapshots or [])
        self.error = error
        self.reads = 0

    def read_manual_set(self):
        self.reads += 1
        if self.error:
            raise self.error
        self.clock["now"] += 6.0
        return set(self.snapshots.pop(0))


class DashboardStandIn:
    _MANUAL_FRESH_TTL_SEC = 2.0
    _install_manual_snapshot = _METHODS["_install_manual_snapshot"]
    _refresh_manual_list_now = _METHODS["_refresh_manual_list_now"]

    def __init__(self, sheet):
        self.sheet = sheet
        self.cache = MemoryCache()
        self.logger = FakeLogger()
        self._manual_last_refresh_ts = 100.0


def test_slow_read_freshness_starts_at_completion_and_prevents_duplicate(monkeypatch):
    clock = {"now": 103.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    sheet = FakeSheet(clock, snapshots=[{"alice"}, {"bob"}])
    dashboard = DashboardStandIn(sheet)

    dashboard._refresh_manual_list_now()
    assert dashboard.cache.manual_set() == {"alice"}
    assert dashboard._manual_last_refresh_ts == 109.0

    clock["now"] = 110.9
    dashboard._refresh_manual_list_now()
    assert sheet.reads == 1

    clock["now"] = 111.1
    dashboard._refresh_manual_list_now()
    assert sheet.reads == 2
    assert dashboard._manual_last_refresh_ts == 117.1


def test_failed_refresh_preserves_snapshot_and_freshness(monkeypatch):
    clock = {"now": 103.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    sheet = FakeSheet(clock, error=RuntimeError("Google unavailable"))
    dashboard = DashboardStandIn(sheet)
    dashboard.cache.set_manual({"cached-user"})

    dashboard._refresh_manual_list_now()

    assert dashboard.cache.manual_set() == {"cached-user"}
    assert dashboard._manual_last_refresh_ts == 100.0
    assert sheet.reads == 1
    assert dashboard.logger.warnings
