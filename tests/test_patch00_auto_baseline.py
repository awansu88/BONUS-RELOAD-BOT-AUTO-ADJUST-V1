"""PATCH-00 characterization lock for the production AUTO pipeline.

All external boundaries are fakes: no Google workbook or remote adjustment panel
is contacted, and every database is created under pytest's temporary directory.
"""

from __future__ import annotations

import csv
import ast
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.database import DatabaseService
from core.memory_cache import MemoryCache
from core.panel_service import PanelService
from core.queue_manager import QueueManager
from core.sheet_service import MasterRow
from core.validator import Validator

ROOT = Path(__file__).resolve().parents[1]


def dashboard_method(name, globals_=None):
    """Load one shipped Dashboard method without importing Qt in headless CI."""
    tree = ast.parse((ROOT / "ui" / "dashboard.py").read_text(encoding="utf-8"))
    dashboard = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "Dashboard")
    method = next(node for node in dashboard.body if isinstance(node, ast.FunctionDef)
                  and node.name == name)
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace = dict(globals_ or {})
    exec(compile(module, "ui/dashboard.py", "exec"), namespace)
    return namespace[name]


RULES = {
    "daily_limit": 10_000,
    "tiers": [
        {"min_deposit": 100_000, "bonus": 10_000},
        {"min_deposit": 50_000, "bonus": 5_000},
    ],
}


class Sheet:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def read_master_rows(self):
        return list(self.rows)


def row(tx, user, amount, timestamp="2025-08-01 10:00", index=1):
    return MasterRow(index, tx, user, amount, "MASTER", timestamp)


def queue(tmp_path, rows, *, batch_size=100, manual=()):
    db = DatabaseService(str(tmp_path / "processed.db"))
    cache = MemoryCache()
    cache.set_manual(set(manual))
    manager = QueueManager(Sheet(rows), cache, Validator(RULES), db, batch_size)
    return db, manager


@pytest.mark.parametrize(
    ("amount", "bonus"),
    [(50_000, 5_000), (99_999, 5_000), (100_000, 10_000), (150_000, 10_000)],
)
def test_auto_tier_boundaries_are_ready(tmp_path, amount, bonus):
    db, manager = queue(tmp_path, [row("T", "alice", amount)])
    stats = manager.refill()
    assert (stats.ready, manager.next_ready().status, manager.next_ready().bonus) == (
        1, "READY", bonus
    )
    db.close()


def test_daily_quota_reduces_second_award_and_is_independent_by_date(tmp_path):
    db, manager = queue(
        tmp_path,
        [
            row("new-same-day", "alice", 100_000, "2025-08-01 11:00"),
            row("new-next-day", "alice", 100_000, "2025-08-02 11:00"),
        ],
    )
    db.insert("old", "alice", 50_000, 5_000, "SUCCESS", "MASTER", "2025-08-01 09:00")
    manager.refill()
    items = manager.preview_items()
    assert [(item.status, item.bonus) for item in items] == [
        ("READY", 5_000), ("READY", 10_000)
    ]
    db.close()


def test_two_5000_awards_reach_cap_and_third_is_limit(tmp_path):
    db, manager = queue(
        tmp_path,
        [row("one", "alice", 50_000), row("two", "alice", 50_000),
         row("three", "alice", 50_000)],
    )
    stats = manager.refill()
    assert [(item.status, item.bonus) for item in manager.preview_items()] == [
        ("READY", 5_000), ("READY", 5_000), ("LIMIT", 0)
    ]
    assert (stats.ready, stats.limit) == (2, 1)
    assert sum(item.bonus for item in manager.preview_items()) == 10_000
    db.close()


def test_existing_10000_is_limit_and_manual_user_never_enters_ready_queue(tmp_path):
    db, manager = queue(
        tmp_path, [row("limited", "alice", 50_000), row("manual", "bob", 100_000)],
        manual={"bob"},
    )
    db.insert("old", "alice", 100_000, 10_000, "SUCCESS", "MASTER", "2025-08-01")
    manager.refill()
    assert [(x.tx_id, x.status) for x in manager.preview_items()] == [
        ("limited", "LIMIT"), ("manual", "MANUAL BONUS")
    ]
    assert manager.next_ready() is None
    db.close()


@pytest.mark.parametrize("existing_result", ["SUCCESS", "LIMIT", "INVALID", "MANUAL BONUS", "FAILED"])
def test_any_existing_tx_outcome_is_deduplicated_including_legacy_failed(
    tmp_path, existing_result
):
    db, manager = queue(tmp_path, [row("same", "alice", 100_000)])
    db.insert("same", "alice", 100_000, 0, existing_result, "MASTER", "2025-08-01")
    stats = manager.refill()
    assert stats.already_in_db == 1
    assert manager.preview_items() == []
    assert manager.next_ready() is None
    db.close()


def test_refill_honours_batch_size_order_and_rebuilds_preview(tmp_path):
    sheet = Sheet([row("A", "a", 50_000), row("B", "b", 50_000), row("C", "c", 50_000)])
    db = DatabaseService(str(tmp_path / "processed.db"))
    manager = QueueManager(sheet, MemoryCache(), Validator(RULES), db, batch_size=2)
    manager.refill()
    assert [manager.next_ready().tx_id, manager.ready_count()] == ["A", 2]
    manager.mark_processed(manager.next_ready(), True)
    assert manager.next_ready().tx_id == "B"
    sheet.rows = [row("D", "d", 50_000)]
    manager.refill()
    assert [x.tx_id for x in manager.preview_items()] == ["D"]
    db.close()


class Locator:
    def __init__(self, selector, events):
        self.selector, self.events, self.first = selector, events, self

    def wait_for(self, **kwargs): self.events.append(("visible", self.selector))
    def click(self): self.events.append(("field-click", self.selector))
    def fill(self, value): self.events.append(("fill", self.selector, value))
    def count(self): return 1
    def evaluate(self, _script): return ""
    def select_option(self, **choice): self.events.append(("select", self.selector, choice))
    def inner_text(self, **kwargs): return "Deposit telah disubmit"


class Page:
    def __init__(self): self.events = []
    def is_closed(self): return False
    def locator(self, selector): return Locator(selector, self.events)
    def wait_for_selector(self, selector, **kwargs): self.events.append(("wait", selector))
    def click(self, selector): self.events.append(("submit", selector))


def panel_with(page):
    selectors = {
        "panel": {"username": "#user", "amount": "#amount", "remark": "#remark",
                  "payment_dropdown": "#payment", "currency_dropdown": "#currency",
                  "submit": "#submit", "success_alert": "#success"},
        "defaults": {"payment": "Bank Transfer", "currency": "Indonesia Rupiah"},
        "success_text": "Deposit telah disubmit", "timeouts": {},
    }
    panel = PanelService({}, selectors)
    panel._page = page
    panel._context = SimpleNamespace(pages=[page])
    panel._attached = True
    return panel


def test_normal_auto_panel_sequence_and_frozen_defaults():
    page = Page()
    result = panel_with(page).submit_deposit("alice", 5_000, "BONUS RELOAD AUTO")
    assert result.ok
    significant = [event for event in page.events if event[0] in {"fill", "select", "submit", "wait"}]
    assert significant == [
        ("wait", "#user"),
        ("fill", "#user", ""), ("fill", "#user", "alice"),
        ("fill", "#amount", ""), ("fill", "#amount", "5000"),
        ("fill", "#remark", ""), ("fill", "#remark", "BONUS RELOAD AUTO"),
        ("select", "#payment", {"label": "Bank Transfer"}),
        ("select", "#currency", {"label": "Indonesia Rupiah"}),
        ("submit", "#submit"), ("wait", "#success"),
    ]


def test_auto_worker_success_persists_after_real_panel_call(tmp_path):
    db, manager = queue(tmp_path, [row("success", "alice", 50_000)])
    manager.refill()
    submits = []
    panel = SimpleNamespace(
        is_alive=lambda: True,
        submit_deposit=lambda **values: (
            submits.append(values) or SimpleNamespace(ok=True, detail="")
        ),
    )
    label = lambda: SimpleNamespace(setText=lambda *_: None, setStyleSheet=lambda *_: None)
    mode = SimpleNamespace(MANUAL=object())
    fake = SimpleNamespace(
        manual_state=SimpleNamespace(mode=object()), worker_timer=SimpleNamespace(stop=lambda: None),
        queue=manager, panel=panel, db=db, cache=MemoryCache(), validator=Validator(RULES),
        logger=SimpleNamespace(info=lambda *_: None, error=lambda *_: None, warn=lambda *_: None),
        state="running", stop_requested=False, config={"remark": "BONUS RELOAD AUTO"},
        current_item=None, cur_user=label(), cur_deposit=label(), cur_bonus=label(), cur_status=label(),
        _submit_duration_sum=0.0, _submit_duration_count=0, _processed_count=0,
        _bonus_paid_total=0, _refresh_manual_list_now=lambda: None,
        _refresh_metrics=lambda: None, _refresh_stats=lambda: None,
        _on_panel_lost=lambda: None, _finalise_stop=lambda *_: None,
        _enter_monitoring=lambda **_: None, _exit_monitoring=lambda: None,
        _tick_monitoring=lambda: None, _save_screenshot=lambda *_: None,
    )
    worker_step = dashboard_method("_worker_step", {"OperatingMode": mode, "time": __import__("time")})
    worker_step(fake)
    assert submits == [{"user_id": "alice", "bonus": 5_000, "remark": "BONUS RELOAD AUTO"}]
    assert db._conn.execute(
        "SELECT bonus, result FROM processed_transactions WHERE tx_id='success'"
    ).fetchone() == (5_000, "SUCCESS")
    assert fake._processed_count == 1 and manager.stats().processed == 1
    db.close()


@pytest.mark.parametrize("closed,context_raises,expected", [(True, False, False), (False, True, False), (False, False, True)])
def test_browser_close_detection_is_current_fail_closed(closed, context_raises, expected):
    class BrowserPage:
        def is_closed(self): return closed
    class Context:
        @property
        def pages(self):
            if context_raises: raise RuntimeError("context died")
            return [BrowserPage()]
    panel = PanelService({}, {})
    panel._page, panel._context = BrowserPage(), Context()
    assert panel.is_alive() is expected


def test_stop_running_waits_for_boundary_but_monitoring_stops_immediately():
    log = SimpleNamespace(info=lambda *_: None)
    running = SimpleNamespace(
        state="running", stop_requested=False, logger=log,
        _set_dot=lambda *_: None, dot_bot=None,
        txt_bot=SimpleNamespace(setText=lambda *_: None), finalised=[],
        _finalise_stop=lambda note="Worker stopped": running.finalised.append(note),
    )
    on_stop = dashboard_method("_on_stop")
    on_stop(running)
    assert running.state == "stopping" and running.stop_requested and running.finalised == []

    monitoring = SimpleNamespace(state="monitoring", stop_requested=False,
        finalised=[], _finalise_stop=lambda note="": monitoring.finalised.append(note))
    on_stop(monitoring)
    assert monitoring.stop_requested and monitoring.finalised == ["STOP requested during monitoring"]


def test_database_wal_export_backup_and_legacy_migration(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE processed_transactions (
          tx_id TEXT PRIMARY KEY, username TEXT NOT NULL, amount INTEGER, bonus INTEGER,
          result TEXT NOT NULL, processed_at TEXT NOT NULL, sheet_name TEXT, timestamp TEXT);
        INSERT INTO processed_transactions VALUES
          ('old','alice',50000,5000,'SUCCESS','2025-08-02T01:00','MASTER','2025-08-01 09:00');
    """)
    conn.close()
    db = DatabaseService(str(path))
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.daily_bonus_for_transaction_date("alice", "2025-08-01") == 5_000
    export, backup = tmp_path / "rows.csv", tmp_path / "backup.db"
    assert db.export_csv(str(export)) == 1
    db.backup(str(backup))
    with export.open(newline="", encoding="utf-8") as handle:
        assert list(csv.reader(handle))[1][-1] == "2025-08-01"
    copied = sqlite3.connect(backup)
    assert copied.execute("SELECT result FROM processed_transactions").fetchone() == ("SUCCESS",)
    copied.close()
    db.close()
