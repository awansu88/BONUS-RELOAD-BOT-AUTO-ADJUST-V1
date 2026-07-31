"""
v1.2.0 Production Hardening — Maintenance service tests.

Categories: C-2 (SQLite retention/vacuum), C-3 (startup maintenance),
C-4 (log cleanup), C-5 (screenshot cleanup).

We exercise `MaintenanceService` against a real temporary SQLite file
so the tests catch any drift in DatabaseService's helper contract.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.database import DatabaseService
from core.maintenance import MaintenanceService


@pytest.fixture
def tmp_db(tmp_path: Path) -> DatabaseService:
    db_path = tmp_path / "processed.db"
    db = DatabaseService(str(db_path))
    yield db
    db.close()


@pytest.fixture
def tmp_service(tmp_db, tmp_path: Path) -> MaintenanceService:
    (tmp_path / "logs").mkdir()
    (tmp_path / "screenshots").mkdir()
    return MaintenanceService(
        db=tmp_db,
        logs_dir=tmp_path / "logs",
        screenshots_dir=tmp_path / "screenshots",
    )


# --------------------------------------------------------------------------- C-3
def test_startup_maintenance_runs_wal_and_optimize_without_vacuum(tmp_service):
    report = tmp_service.startup_maintenance()
    step_names = [s.name for s in report.steps]
    assert step_names == ["WAL checkpoint (passive)", "PRAGMA optimize"]
    assert all(s.ok for s in report.steps)
    # startup_maintenance must NEVER include VACUUM (C-3 explicit).
    assert "VACUUM" not in step_names


# --------------------------------------------------------------------------- C-2
def test_full_maintenance_retention_purge(tmp_db, tmp_service):
    # Seed one recent row and one 60-day-old row.
    tmp_db._conn.execute(
        "INSERT INTO processed_transactions "
        "(tx_id, username, amount, bonus, result, processed_at, sheet_name, timestamp, timestamp_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("recent", "u1", 100, 10, "SUCCESS",
         datetime.now().isoformat(timespec="seconds"),
         "MASTER", "", None),
    )
    old_iso = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
    tmp_db._conn.execute(
        "INSERT INTO processed_transactions "
        "(tx_id, username, amount, bonus, result, processed_at, sheet_name, timestamp, timestamp_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("old", "u2", 100, 10, "SUCCESS", old_iso, "MASTER", "", None),
    )
    assert tmp_db.total_count() == 2
    assert tmp_db.count_older_than(30) == 1

    report = tmp_service.full_maintenance(retention_days=30, run_vacuum=False)
    assert report.all_ok, report.summary()
    assert tmp_db.total_count() == 1  # old row purged, recent kept

    # Report must clearly show the purge step.
    purge_step = next(
        (s for s in report.steps if s.name.startswith("Delete rows")), None
    )
    assert purge_step is not None
    assert "deleted 1 rows" in purge_step.detail


def test_full_maintenance_without_retention_or_vacuum(tmp_db, tmp_service):
    report = tmp_service.full_maintenance(retention_days=None, run_vacuum=False)
    names = [s.name for s in report.steps]
    assert not any(n.startswith("Delete rows") for n in names)
    assert not any(n == "VACUUM" for n in names)
    # Core steps still run.
    assert "Integrity check" in names
    assert "ANALYZE" in names


def test_full_maintenance_with_vacuum(tmp_db, tmp_service):
    report = tmp_service.full_maintenance(retention_days=None, run_vacuum=True)
    assert any(s.name == "VACUUM" for s in report.steps)


def test_integrity_check_returns_ok_on_fresh_db(tmp_service):
    report = tmp_service.integrity_check()
    assert report.all_ok
    assert "ok" in report.steps[0].detail.lower()


# --------------------------------------------------------------------------- C-4
def test_cleanup_logs_removes_files_older_than_days(tmp_service, tmp_path):
    logs_dir = tmp_path / "logs"
    # 2 recent, 2 old (mtime 40 days ago)
    recent1 = logs_dir / "2026-01-01.log"; recent1.write_text("r1")
    recent2 = logs_dir / "2026-01-02.log"; recent2.write_text("r2")
    old1 = logs_dir / "2025-11-01.log"; old1.write_text("o1")
    old2 = logs_dir / "2025-11-02.log"; old2.write_text("o2")
    old_ts = (datetime.now() - timedelta(days=40)).timestamp()
    os.utime(old1, (old_ts, old_ts))
    os.utime(old2, (old_ts, old_ts))

    report = tmp_service.cleanup_logs(older_than_days=30, keep_files=999)
    assert report.all_ok
    remaining = tmp_service.list_logs()
    assert recent1 in remaining and recent2 in remaining
    assert old1 not in remaining and old2 not in remaining


def test_cleanup_logs_fifo_when_over_limit(tmp_service, tmp_path):
    logs_dir = tmp_path / "logs"
    for name in ("2026-01-01.log", "2026-01-02.log", "2026-01-03.log", "2026-01-04.log"):
        (logs_dir / name).write_text("x")
    report = tmp_service.cleanup_logs(keep_files=2)
    assert report.all_ok
    remaining = sorted(p.name for p in tmp_service.list_logs())
    assert remaining == ["2026-01-03.log", "2026-01-04.log"]


# --------------------------------------------------------------------------- C-5
def test_cleanup_screenshots_removes_old_files_only(tmp_service, tmp_path):
    sc_dir = tmp_path / "screenshots"
    fresh = sc_dir / "fresh.png"; fresh.write_bytes(b"\x89PNG")
    stale = sc_dir / "stale.png"; stale.write_bytes(b"\x89PNG")
    stale_ts = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(stale, (stale_ts, stale_ts))

    report = tmp_service.cleanup_screenshots(older_than_days=14)
    assert report.all_ok
    remaining = [p.name for p in tmp_service.list_screenshots()]
    assert "fresh.png" in remaining
    assert "stale.png" not in remaining


def test_maintenance_report_summary_lines(tmp_service):
    report = tmp_service.startup_maintenance()
    summary = report.summary()
    assert "Maintenance report" in summary
    assert "[OK] WAL checkpoint (passive)" in summary


# --------------------------------------------------------------------------- DB helpers
def test_database_new_helpers_are_no_ops_on_success(tmp_db):
    tmp_db.checkpoint_wal("PASSIVE")
    tmp_db.optimize()
    assert tmp_db.is_open() is True
    assert tmp_db.integrity_check().strip().lower().startswith("ok")


def test_database_count_older_than(tmp_db):
    now = datetime.now()
    old = (now - timedelta(days=45)).isoformat(timespec="seconds")
    tmp_db._conn.execute(
        "INSERT INTO processed_transactions "
        "(tx_id, username, amount, bonus, result, processed_at, sheet_name, timestamp, timestamp_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a", "u", 1, 1, "SUCCESS", old, "MASTER", "", None),
    )
    assert tmp_db.count_older_than(30) == 1
    assert tmp_db.count_older_than(60) == 0
