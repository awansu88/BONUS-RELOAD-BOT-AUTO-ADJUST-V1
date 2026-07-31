"""
v1.2.0 Production Hardening — Crash-state store tests (B-7, B-8).

The store persists a tiny JSON checkpoint next to the executable so
the operator's URL and window geometry survive both clean and unclean
exits. It must never raise on corrupt / missing state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.crash_state import CrashState, CrashStateStore


def test_load_returns_default_when_file_absent(tmp_path):
    store = CrashStateStore(tmp_path / "state.json")
    state = store.load()
    assert isinstance(state, CrashState)
    assert state.clean_exit is False
    assert state.spreadsheet_url == ""


def test_load_returns_default_when_file_corrupt(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = CrashStateStore(path)
    state = store.load()
    assert isinstance(state, CrashState)
    # Corrupt content must NOT raise.
    assert state.spreadsheet_url == ""


def test_save_and_load_round_trip(tmp_path):
    store = CrashStateStore(tmp_path / "state.json")
    s = CrashState(
        version="v1.2.0",
        clean_exit=True,
        spreadsheet_url="https://docs.google.com/spreadsheets/d/abc",
        monitoring_active=True,
        window_geometry="abcd",
        last_panel_url="https://panel.example.com",
    )
    assert store.save(s) is True
    loaded = store.load()
    assert loaded.version == "v1.2.0"
    assert loaded.clean_exit is True
    assert loaded.spreadsheet_url == "https://docs.google.com/spreadsheets/d/abc"
    assert loaded.monitoring_active is True
    assert loaded.window_geometry == "abcd"
    assert loaded.last_panel_url == "https://panel.example.com"
    assert loaded.saved_at   # populated by save()


def test_mark_dirty_forces_clean_exit_false(tmp_path):
    path = tmp_path / "state.json"
    # Start with a clean state on disk.
    initial = CrashState(clean_exit=True, spreadsheet_url="old")
    CrashStateStore(path).save(initial)

    store = CrashStateStore(path)
    result = store.mark_dirty(spreadsheet_url="new")
    assert result is not None
    reloaded = store.load()
    assert reloaded.clean_exit is False
    assert reloaded.spreadsheet_url == "new"


def test_mark_clean_exit_flips_flag(tmp_path):
    store = CrashStateStore(tmp_path / "state.json")
    store.save(CrashState(clean_exit=False, spreadsheet_url="x"))
    assert store.mark_clean_exit(monitoring_active=False) is True
    loaded = store.load()
    assert loaded.clean_exit is True
    assert loaded.monitoring_active is False
    assert loaded.spreadsheet_url == "x"  # preserved


def test_save_is_atomic(tmp_path):
    """Ensure no `.tmp` file is left behind after a successful save."""
    store = CrashStateStore(tmp_path / "state.json")
    store.save(CrashState(spreadsheet_url="url"))
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["state.json"]


def test_unknown_keys_in_saved_file_are_ignored(tmp_path):
    path = tmp_path / "state.json"
    payload = {
        "spreadsheet_url": "kept",
        "unknown_key": "ignored",
        "another_bad_key": 42,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = CrashStateStore(path).load()
    assert loaded.spreadsheet_url == "kept"
