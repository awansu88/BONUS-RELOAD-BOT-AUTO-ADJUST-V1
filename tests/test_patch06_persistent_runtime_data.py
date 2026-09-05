"""PATCH-06 persistent runtime/upgrade safety regression tests."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path, PureWindowsPath

import pytest

from core.runtime_paths import (
    RuntimeLayoutError, mark_initialized, prepare_config, prepare_runtime,
    read_layout_state, remap_runtime_path, resolve_runtime_paths,
    sqlite_readonly_uri, sqlite_snapshot,
)
import core.runtime_paths as runtime_paths_module


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


def test_initialized_layout_same_database_is_idempotent(tmp_path):
    p = paths(tmp_path); seed_bundle(p); prepare_config(p)
    runtime = prepare_runtime(p, {})
    sqlite3.connect(runtime.database_path).close()
    mark_initialized(p, runtime.database_path)
    original = p.layout_state_path.read_bytes()
    first = read_layout_state(p)

    selected = prepare_runtime(p, {"sqlite_path": str(runtime.database_path)})
    mark_initialized(p, selected.database_path)

    assert selected.database_path == runtime.database_path
    assert p.layout_state_path.read_bytes() == original
    assert read_layout_state(p).initialized_at == first.initialized_at


def test_initialized_layout_rejects_existing_alternate_database(tmp_path):
    p = paths(tmp_path); seed_bundle(p); prepare_config(p)
    authoritative = p.data_dir / "processed.db"
    alternate = p.data_dir / "alternate.db"
    sqlite3.connect(authoritative).close()
    with sqlite3.connect(alternate) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('untouched')")
    mark_initialized(p, authoritative)
    marker = p.layout_state_path.read_bytes()

    with pytest.raises(RuntimeLayoutError, match="does not match") as error:
        prepare_runtime(p, {"sqlite_path": "alternate.db"})

    assert str(authoritative.resolve()) in str(error.value)
    assert str(alternate.resolve()) in str(error.value)
    assert p.layout_state_path.read_bytes() == marker
    with sqlite3.connect(alternate) as db:
        assert db.execute("SELECT value FROM sentinel").fetchone() == ("untouched",)


@pytest.mark.parametrize("database_path", [None, "", "relative.db", 42])
def test_initialized_marker_rejects_invalid_authoritative_database_path(
    tmp_path, database_path,
):
    p = paths(tmp_path); p.data_dir.mkdir(parents=True)
    p.layout_state_path.write_text(json.dumps({
        "layout_version": 1, "initialized": True, "database_path": database_path,
    }))
    with pytest.raises(RuntimeLayoutError, match="database_path"):
        read_layout_state(p)
    assert not (p.data_dir / "processed.db").exists()


def test_mark_initialized_rejects_different_database_without_rewrite(tmp_path):
    p = paths(tmp_path)
    original, alternate = p.data_dir / "original.db", p.data_dir / "alternate.db"
    original.parent.mkdir(parents=True); original.touch(); alternate.touch()
    mark_initialized(p, original)
    marker = p.layout_state_path.read_bytes()
    with pytest.raises(RuntimeLayoutError, match="Refusing to replace"):
        mark_initialized(p, alternate)
    assert p.layout_state_path.read_bytes() == marker


def test_initialized_layout_never_reimports_stale_legacy_runtime_state(tmp_path):
    p = paths(tmp_path); seed_bundle(p); prepare_config(p)
    (p.app_dir / "credentials").mkdir()
    (p.app_dir / "credentials/key.json").write_text("stale-secret")
    (p.app_dir / "profile").mkdir(); (p.app_dir / "profile/cookie").write_text("stale")
    (p.app_dir / "runtime_state.json").write_text('{"clean_exit":false}')
    (p.app_dir / "logs").mkdir(); (p.app_dir / "logs/legacy.log").write_text("stale")
    (p.app_dir / "screenshots").mkdir()
    (p.app_dir / "screenshots/legacy.png").write_bytes(b"stale")
    config = {"google_credentials": "credentials/key.json",
              "browser": {"user_data_dir": "profile"}}
    runtime = prepare_runtime(p, config)
    sqlite3.connect(runtime.database_path).close(); mark_initialized(p, runtime.database_path)

    runtime.credentials_path.unlink()
    for child in runtime.browser_profile_path.iterdir(): child.unlink()
    runtime.browser_profile_path.rmdir()
    p.crash_state_path.unlink()
    for directory in (p.logs_dir, p.screenshots_dir):
        for child in directory.iterdir(): child.unlink()
        directory.rmdir()

    selected = prepare_runtime(p, {"google_credentials": "credentials/key.json",
                                   "browser": {"user_data_dir": "profile"}})
    assert not selected.credentials_path.exists()
    assert selected.browser_profile_path.is_dir()
    assert not (selected.browser_profile_path / "cookie").exists()
    assert not p.crash_state_path.exists()
    assert p.logs_dir.is_dir() and not (p.logs_dir / "legacy.log").exists()
    assert p.screenshots_dir.is_dir() and not (p.screenshots_dir / "legacy.png").exists()


def test_marker_write_failure_is_atomic_and_database_survives(tmp_path, monkeypatch):
    p = paths(tmp_path); p.data_dir.mkdir(parents=True)
    database = p.data_dir / "processed.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('safe')")

    def fail_replace(source, target):
        raise PermissionError("simulated marker promotion denial")

    monkeypatch.setattr(runtime_paths_module.os, "replace", fail_replace)
    with pytest.raises(RuntimeLayoutError, match="database was left intact"):
        mark_initialized(p, database)
    assert not p.layout_state_path.exists()
    assert not list(p.data_dir.glob("runtime_layout.json.tmp-*"))
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT value FROM sentinel").fetchone() == ("safe",)

    existing = b'{"layout_version": 0, "initialized": false}\n'
    p.layout_state_path.write_bytes(existing)
    with pytest.raises(RuntimeLayoutError, match="database was left intact"):
        mark_initialized(p, database)
    assert p.layout_state_path.read_bytes() == existing
    assert not list(p.data_dir.glob("runtime_layout.json.tmp-*"))


def test_sqlite_readonly_uri_escapes_windows_path():
    uri = sqlite_readonly_uri(PureWindowsPath(r"C:\Program Files\Bonus #1\data?.db"))
    assert uri.startswith("file:///C:/Program%20Files/")
    assert "%23" in uri and "%3F" in uri and uri.endswith("?mode=ro")


def test_external_credentials_and_profile_are_preserved(tmp_path):
    p = paths(tmp_path); seed_bundle(p); prepare_config(p)
    external_credentials = tmp_path / "external" / "key.json"
    external_profile = tmp_path / "external-profile"
    external_credentials.parent.mkdir(); external_credentials.write_text("external")
    external_profile.mkdir(); (external_profile / "cookie").write_text("external")
    runtime = prepare_runtime(p, {
        "google_credentials": str(external_credentials),
        "browser": {"user_data_dir": str(external_profile)},
    })
    assert runtime.credentials_path == external_credentials.resolve()
    assert runtime.browser_profile_path == external_profile.resolve()
    assert not (p.credentials_dir / "key.json").exists()
    assert not (p.data_dir / "external-profile").exists()


@pytest.mark.parametrize("environment", [
    {},
    {"BONUS_RELOAD_DATA_DIR": "relative-data"},
])
def test_invalid_frozen_runtime_environment_is_deferred_to_startup_boundary(
    tmp_path, monkeypatch, environment,
):
    resource, app = tmp_path / "resource", tmp_path / "app"
    resource.mkdir(); app.mkdir()
    fake_exe = app / "Bonus Reload Bot.exe"; fake_exe.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(resource), raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    source = Path("main.py").resolve().read_text(encoding="utf-8")
    boot = source[:source.index("# Now the app can import Playwright/Qt safely.")]
    namespace = {"__name__": "main_invalid_runtime_test"}

    # Import-safe bootstrap does not resolve DATA_DIR or write runtime state.
    exec(compile(boot, str(Path("main.py").resolve()), "exec"), namespace)
    with pytest.raises(RuntimeLayoutError):
        namespace["_prepare_startup"](environment)
    assert not (app / "processed.db").exists()
    assert not (app / "config").exists()


def test_valid_frozen_runtime_environment_reaches_persistent_preparation(tmp_path):
    p = paths(tmp_path); seed_bundle(p)
    prepare_config(p)
    runtime = prepare_runtime(p, {})
    assert runtime.database_path.parent == p.data_dir
    assert runtime.browser_profile_path.parent == p.data_dir
