"""PATCH-06 persistent runtime/upgrade safety regression tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.runtime_paths import (
    RuntimeLayoutError, mark_initialized, prepare_config, prepare_runtime,
    remap_runtime_path, resolve_runtime_paths, sqlite_snapshot,
)


def paths(tmp_path: Path, *, frozen: bool = True, env=None):
    app, resource = tmp_path / "app", tmp_path / "resource"
    app.mkdir(); resource.mkdir()
    environment = {"LOCALAPPDATA": str(tmp_path / "local")} if env is None else env
    return resolve_runtime_paths(
        app_dir=app, resource_dir=resource, frozen=frozen, environ=environment
    )


def seed_bundle(p) -> None:
    p.resource_dir.joinpath("config").mkdir()
    p.resource_dir.joinpath("config/config.json").write_text(
        json.dumps({"panel_url": "bundled", "spreadsheet_url": "bundle"})
    )
    p.resource_dir.joinpath("config/selectors.json").write_text('{"login": "#default"}')
    p.resource_dir.joinpath("credentials").mkdir()
    p.resource_dir.joinpath("credentials/service_account.json.example").write_text("example")


def test_path_resolution_matrix(tmp_path, monkeypatch):
    p = paths(tmp_path)
    assert p.data_dir == (tmp_path / "local" / "BonusReloadBot").resolve()
    assert p.data_dir != p.app_dir and p.resource_dir != p.data_dir
    override = (tmp_path / "override").resolve()
    q = resolve_runtime_paths(app_dir=p.app_dir, resource_dir=p.resource_dir,
                              frozen=True, environ={"BONUS_RELOAD_DATA_DIR": str(override)})
    assert q.data_dir == override
    with pytest.raises(RuntimeLayoutError, match="absolute"):
        resolve_runtime_paths(app_dir=p.app_dir, resource_dir=p.resource_dir,
                              frozen=True, environ={"BONUS_RELOAD_DATA_DIR": "relative"})
    with pytest.raises(RuntimeLayoutError, match="LOCALAPPDATA"):
        resolve_runtime_paths(app_dir=p.app_dir, resource_dir=p.resource_dir,
                              frozen=True, environ={})
    (tmp_path / "local").mkdir()
    monkeypatch.chdir(tmp_path / "local")
    source = resolve_runtime_paths(app_dir=p.app_dir, resource_dir=p.resource_dir,
                                   frozen=False, environ={})
    assert source.data_dir == p.app_dir


def test_first_install_and_marker(tmp_path):
    p = paths(tmp_path); seed_bundle(p)
    prepare_config(p)
    runtime = prepare_runtime(p, {"browser": {}})
    assert json.loads(p.config_path.read_text())["panel_url"] == "bundled"
    assert p.selectors_path.exists()
    assert (p.credentials_dir / "service_account.json.example").read_text() == "example"
    assert not (p.credentials_dir / "service_account.json").exists()
    assert all(x.is_dir() for x in (p.logs_dir, p.screenshots_dir, runtime.browser_profile_path))
    assert not runtime.database_path.exists()  # migration decision precedes DB creation
    sqlite3.connect(runtime.database_path).close()
    mark_initialized(p, runtime.database_path)
    assert json.loads(p.layout_state_path.read_text())["initialized"] is True
    assert not (p.app_dir / "logs").exists()


def test_config_upgrade_precedence_and_missing_guard(tmp_path):
    p = paths(tmp_path); seed_bundle(p)
    legacy = p.app_dir / "config"; legacy.mkdir()
    legacy.joinpath("config.json").write_text('{"panel_url":"custom","spreadsheet_url":"sheet"}')
    legacy.joinpath("selectors.json").write_text('{"login":"#legacy"}')
    prepare_config(p); prepare_config(p)
    assert json.loads(p.config_path.read_text())["panel_url"] == "custom"
    assert json.loads(p.config_path.read_text())["spreadsheet_url"] == "sheet"
    assert json.loads(p.selectors_path.read_text())["login"] == "#legacy"
    sqlite3.connect(p.data_dir / "processed.db").close(); mark_initialized(p, p.data_dir / "processed.db")
    p.selectors_path.unlink()
    with pytest.raises(RuntimeLayoutError, match="incomplete"):
        prepare_config(p)
    assert not p.selectors_path.exists()


def test_sqlite_wal_snapshot_preserves_rows_and_source(tmp_path):
    p = paths(tmp_path); source = p.app_dir / "processed.db"
    con = sqlite3.connect(source)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE state(tx TEXT, status TEXT, reserved_bonus INTEGER, attempt_count INTEGER)")
    con.execute("INSERT INTO state VALUES ('TX1','UNKNOWN',75,2)"); con.commit()
    before = source.read_bytes()
    target = p.data_dir / "processed.db"
    sqlite_snapshot(source, target)
    with sqlite3.connect(target) as migrated:
        assert migrated.execute("SELECT * FROM state").fetchone() == ("TX1", "UNKNOWN", 75, 2)
        assert migrated.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert source.exists() and source.read_bytes() == before
    # No source sidecar was copied; the destination may create its own WAL.
    assert not list(target.parent.glob("processed.db.tmp-*-wal"))
    con.close()


def test_database_precedence_idempotence_and_no_silent_reset(tmp_path):
    p = paths(tmp_path); seed_bundle(p); prepare_config(p)
    legacy = p.app_dir / "processed.db"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE t(value TEXT)"); db.execute("INSERT INTO t VALUES ('legacy')")
    r = prepare_runtime(p, {})
    prepare_runtime(p, {})
    assert sqlite3.connect(r.database_path).execute("SELECT value FROM t").fetchone()[0] == "legacy"
    mark_initialized(p, r.database_path)
    r.database_path.unlink()
    with pytest.raises(RuntimeLayoutError, match="silent empty database reset"):
        prepare_runtime(p, {})
    assert not r.database_path.exists()  # stale legacy is not re-imported


def test_snapshot_failure_is_atomic_and_leaves_legacy(tmp_path):
    p = paths(tmp_path); source = p.app_dir / "processed.db"; source.write_text("not sqlite")
    target = p.data_dir / "processed.db"
    with pytest.raises(RuntimeLayoutError, match="original data was left untouched"):
        sqlite_snapshot(source, target)
    assert source.read_text() == "not sqlite" and not target.exists()
    assert not list(target.parent.glob("processed.db.tmp-*"))


def test_custom_path_remapping_and_external_preservation(tmp_path):
    p = paths(tmp_path)
    target, legacy = remap_runtime_path("data/accounting.db", p)
    assert target == p.data_dir / "data/accounting.db"
    assert legacy == p.app_dir / "data/accounting.db"
    inside, old = remap_runtime_path(p.app_dir / "nested/db.sqlite", p)
    assert inside == p.data_dir / "nested/db.sqlite" and old == p.app_dir / "nested/db.sqlite"
    external = tmp_path / "app2" / "external.db"
    assert remap_runtime_path(external, p) == (external.resolve(), None)


def test_credential_profile_crash_and_history_migration(tmp_path):
    p = paths(tmp_path); seed_bundle(p); prepare_config(p)
    (p.app_dir / "credentials").mkdir(); (p.app_dir / "credentials/key.json").write_text("SECRET")
    (p.app_dir / "profile").mkdir(); (p.app_dir / "profile/cookie").write_text("session")
    (p.app_dir / "runtime_state.json").write_text('{"clean_exit":false}')
    (p.app_dir / "logs").mkdir(); (p.app_dir / "logs/old.log").write_text("old")
    (p.app_dir / "screenshots").mkdir(); (p.app_dir / "screenshots/a.png").write_bytes(b"png")
    runtime = prepare_runtime(p, {
        "google_credentials": "credentials/key.json", "browser": {"user_data_dir": "profile"}
    })
    assert runtime.credentials_path.read_text() == "SECRET"
    assert (runtime.browser_profile_path / "cookie").read_text() == "session"
    assert json.loads(p.crash_state_path.read_text())["clean_exit"] is False
    assert (p.logs_dir / "old.log").exists() and (p.screenshots_dir / "a.png").exists()
    runtime.credentials_path.write_text("NEW"); (runtime.browser_profile_path / "cookie").write_text("new")
    prepare_runtime(p, {"google_credentials": "credentials/key.json",
                        "browser": {"user_data_dir": "profile"}})
    assert runtime.credentials_path.read_text() == "NEW"
    assert (runtime.browser_profile_path / "cookie").read_text() == "new"
