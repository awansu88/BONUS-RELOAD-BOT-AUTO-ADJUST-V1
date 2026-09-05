"""Persistent runtime layout and upgrade migration helpers.

Application and bundled-resource directories are deliberately read-only inputs.
All relative writable paths are rooted in ``data_dir``.  Migration is copy-only:
the pre-PATCH-06 application directory is never modified.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath, PureWindowsPath
from typing import Callable, Mapping, Optional
from urllib.parse import quote


LAYOUT_VERSION = 1
DATA_DIR_ENV = "BONUS_RELOAD_DATA_DIR"


class RuntimeLayoutError(RuntimeError):
    """A critical layout/migration error that must abort startup."""


@dataclass(frozen=True)
class RuntimePaths:
    app_dir: Path
    resource_dir: Path
    data_dir: Path
    config_dir: Path
    config_path: Path
    selectors_path: Path
    credentials_dir: Path
    logs_dir: Path
    screenshots_dir: Path
    crash_state_path: Path
    layout_state_path: Path


@dataclass(frozen=True)
class RuntimeLayoutState:
    initialized: bool = False
    layout_version: int = 0
    database_path: Optional[Path] = None
    initialized_at: Optional[str] = None


def resolve_runtime_paths(
    *, app_dir: Path, resource_dir: Path, frozen: bool,
    environ: Optional[Mapping[str, str]] = None,
) -> RuntimePaths:
    """Resolve paths without consulting the current working directory."""
    env = os.environ if environ is None else environ
    app = Path(app_dir).resolve()
    resource = Path(resource_dir).resolve()
    override = env.get(DATA_DIR_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise RuntimeLayoutError(
                f"{DATA_DIR_ENV} must be an absolute path; got {override!r}"
            )
        data = candidate.resolve()
    elif frozen:
        local = env.get("LOCALAPPDATA", "").strip()
        if not local or not Path(local).is_absolute():
            raise RuntimeLayoutError(
                "Frozen startup requires a valid absolute LOCALAPPDATA or "
                f"an absolute {DATA_DIR_ENV}; executable-directory fallback is disabled."
            )
        data = (Path(local) / "BonusReloadBot").resolve()
    else:
        data = app
    return RuntimePaths(
        app, resource, data, data / "config", data / "config" / "config.json",
        data / "config" / "selectors.json", data / "credentials",
        data / "logs", data / "screenshots", data / "runtime_state.json",
        data / "runtime_layout.json",
    )


def read_layout_state(paths: RuntimePaths) -> RuntimeLayoutState:
    """Read and validate the marker, failing closed for initialized layouts."""
    if not paths.layout_state_path.exists():
        return RuntimeLayoutState()
    try:
        state = json.loads(paths.layout_state_path.read_text(encoding="utf-8"))
        initialized = state.get("initialized") is True
        version = int(state.get("layout_version", 0))
        raw_database = state.get("database_path")
        database_path = None
        if initialized:
            if version < 1:
                raise ValueError("initialized marker has an invalid layout_version")
            if not isinstance(raw_database, str) or not raw_database.strip():
                raise ValueError("initialized marker has no authoritative database_path")
            candidate = Path(raw_database)
            if not candidate.is_absolute():
                raise ValueError("authoritative database_path must be absolute")
            database_path = candidate.resolve()
        return RuntimeLayoutState(
            initialized=initialized,
            layout_version=version,
            database_path=database_path,
            initialized_at=state.get("initialized_at"),
        )
    except Exception as exc:
        raise RuntimeLayoutError(
            f"Runtime layout marker is unreadable at {paths.layout_state_path}: {exc}"
        ) from exc


def _temp_for(target: Path) -> Path:
    return target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex}")


def _copy_file_atomic(source: Path, target: Path, component: str) -> None:
    if target.exists() or not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_for(target)
    try:
        shutil.copy2(source, temp)
        os.replace(temp, target)
    except Exception as exc:
        temp.unlink(missing_ok=True)
        raise RuntimeLayoutError(
            f"Failed to migrate {component} to {target} from legacy source {source}; "
            f"original data was left untouched: {exc}"
        ) from exc


def _copy_dir_atomic(source: Path, target: Path, component: str) -> None:
    if target.exists() or not source.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_for(target)
    try:
        shutil.copytree(source, temp)
        os.replace(temp, target)
    except Exception as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise RuntimeLayoutError(
            f"Failed to migrate {component} to {target} from legacy source {source}; "
            f"original data was left untouched: {exc}"
        ) from exc


def prepare_config(paths: RuntimePaths) -> None:
    """Migrate legacy config, or seed bundled templates on first install."""
    layout = read_layout_state(paths)
    if layout.initialized:
        missing = [p for p in (paths.config_path, paths.selectors_path) if not p.is_file()]
        if missing:
            raise RuntimeLayoutError(
                "Initialized persistent configuration is incomplete under "
                f"{paths.data_dir}; missing: {', '.join(map(str, missing))}. "
                "Bundled defaults were not restored."
            )
        return
    if not paths.config_dir.exists():
        legacy = paths.app_dir / "config"
        bundled = paths.resource_dir / "config"
        # In source mode target and legacy are identical; the existing tree wins.
        if legacy.is_dir():
            if legacy.resolve() != paths.config_dir.resolve():
                _copy_dir_atomic(legacy, paths.config_dir, "configuration")
        elif bundled.is_dir():
            _copy_dir_atomic(bundled, paths.config_dir, "bundled configuration")
        else:
            raise RuntimeLayoutError(
                f"No configuration templates found at {bundled}; DATA_DIR={paths.data_dir}"
            )
    missing = [p for p in (paths.config_path, paths.selectors_path) if not p.is_file()]
    if missing:
        raise RuntimeLayoutError(f"Required persistent configuration missing: {missing}")


def remap_runtime_path(value: str | Path, paths: RuntimePaths) -> tuple[Path, Optional[Path]]:
    """Return (new path, legacy source), preserving truly external absolutes."""
    raw = Path(value)
    if not raw.is_absolute():
        return (paths.data_dir / raw).resolve(), (paths.app_dir / raw).resolve()
    resolved = raw.resolve()
    try:
        suffix = resolved.relative_to(paths.app_dir)
    except ValueError:
        return resolved, None
    return (paths.data_dir / suffix).resolve(), resolved


def sqlite_readonly_uri(path: PurePath) -> str:
    """Return a properly escaped, platform-native SQLite read-only file URI."""
    if isinstance(path, PureWindowsPath):
        if not path.is_absolute():
            raise ValueError("Windows SQLite path must be absolute")
        return f"file:///{quote(path.as_posix(), safe='/:')}?mode=ro"
    absolute = path if path.is_absolute() else Path(path).resolve()
    return f"{Path(absolute).as_uri()}?mode=ro"


def sqlite_snapshot(source: Path, target: Path) -> None:
    """Create and integrity-check a WAL-aware SQLite backup, then promote it."""
    if target.exists() or not source.exists() or source.resolve() == target.resolve():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_for(target)
    sidecars = (Path(f"{temp}-wal"), Path(f"{temp}-shm"))
    try:
        with sqlite3.connect(sqlite_readonly_uri(source), uri=True) as src:
            with sqlite3.connect(str(temp)) as dst:
                src.backup(dst)
        with sqlite3.connect(sqlite_readonly_uri(temp), uri=True) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise RuntimeLayoutError(f"SQLite integrity_check failed: {result!r}")
        # Connections are closed, so any target-side transient WAL files can
        # be discarded.  They are never promoted or confused with source WAL.
        for sidecar in sidecars:
            sidecar.unlink(missing_ok=True)
        os.replace(temp, target)
    except Exception as exc:
        temp.unlink(missing_ok=True)
        for sidecar in sidecars:
            sidecar.unlink(missing_ok=True)
        raise RuntimeLayoutError(
            f"Failed to migrate SQLite database to {target} from legacy source {source}; "
            f"original data was left untouched and no replacement database was created: {exc}"
        ) from exc


@dataclass(frozen=True)
class ResolvedRuntime:
    database_path: Path
    credentials_path: Path
    browser_profile_path: Path


def prepare_runtime(
    paths: RuntimePaths, config: dict,
    *, sqlite_migrator: Callable[[Path, Path], None] = sqlite_snapshot,
) -> ResolvedRuntime:
    """Resolve configured paths, migrate legacy state, and enforce reset guards."""
    layout = read_layout_state(paths)
    initialized = layout.initialized
    sqlite_value = config.get("sqlite_path", "processed.db")
    credentials_value = config.get("google_credentials", "credentials/service_account.json")
    browser = config.setdefault("browser", {})
    profile_value = browser.get("user_data_dir", "browser_profile_bonus_reload")
    db, legacy_db = remap_runtime_path(sqlite_value, paths)
    credentials, legacy_credentials = remap_runtime_path(
        credentials_value, paths
    )
    profile, legacy_profile = remap_runtime_path(
        profile_value, paths
    )
    config["google_credentials"] = str(credentials)
    browser["user_data_dir"] = str(profile)

    if initialized:
        assert layout.database_path is not None  # validated by read_layout_state
        if db.resolve() != layout.database_path.resolve():
            raise RuntimeLayoutError(
                "Configured database does not match the initialized authoritative database. "
                f"Recorded: {layout.database_path}; requested: {db.resolve()}; "
                f"DATA_DIR={paths.data_dir}. Startup stopped without changing the marker."
            )
        if not db.is_file():
            raise RuntimeLayoutError(
                f"Initialized runtime database is missing at {db}; DATA_DIR={paths.data_dir}. "
                "Startup stopped to prevent a silent empty database reset."
            )
    elif legacy_db and legacy_db.exists() and legacy_db.resolve() != db.resolve() and not db.exists():
        sqlite_migrator(legacy_db, db)
    # Creating the directory is safe; only DatabaseService may create a new
    # database, and only after the initialized-layout guard above permits it.
    db.parent.mkdir(parents=True, exist_ok=True)

    if not initialized:
        if legacy_credentials and legacy_credentials.resolve() != credentials.resolve():
            _copy_file_atomic(legacy_credentials, credentials, "credentials")
        if legacy_profile and legacy_profile.resolve() != profile.resolve():
            _copy_dir_atomic(legacy_profile, profile, "browser profile")
        _copy_file_atomic(paths.app_dir / "runtime_state.json", paths.crash_state_path, "crash state")
        for name, target in (("logs", paths.logs_dir), ("screenshots", paths.screenshots_dir)):
            legacy = paths.app_dir / name
            if legacy.resolve() != target.resolve():
                _copy_dir_atomic(legacy, target, name)

    for directory in (paths.credentials_dir, paths.logs_dir, paths.screenshots_dir, profile):
        directory.mkdir(parents=True, exist_ok=True)
    example = paths.resource_dir / "credentials" / "service_account.json.example"
    _copy_file_atomic(example, paths.credentials_dir / example.name, "credentials example")

    # Older releases could persist app-local absolute paths.  Rewrite only
    # those values after successful migration so a later executable-folder
    # replacement cannot make the old install directory look "external".
    rewritten = False
    if Path(sqlite_value).is_absolute() and legacy_db is not None:
        config["sqlite_path"] = str(db); rewritten = True
    if Path(credentials_value).is_absolute() and legacy_credentials is not None:
        config["google_credentials"] = str(credentials); rewritten = True
    if Path(profile_value).is_absolute() and legacy_profile is not None:
        browser["user_data_dir"] = str(profile); rewritten = True
    if rewritten:
        temp = _temp_for(paths.config_path)
        try:
            temp.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
            os.replace(temp, paths.config_path)
        except Exception as exc:
            temp.unlink(missing_ok=True)
            raise RuntimeLayoutError(
                f"Failed to persist remapped runtime paths in {paths.config_path}; "
                f"original legacy data was left untouched: {exc}"
            ) from exc
    return ResolvedRuntime(db, credentials, profile)


def mark_initialized(paths: RuntimePaths, database_path: Path) -> None:
    """Atomically record successful DB initialization."""
    authoritative = Path(database_path).resolve()
    existing = read_layout_state(paths)
    if existing.initialized:
        assert existing.database_path is not None
        if existing.database_path.resolve() != authoritative:
            raise RuntimeLayoutError(
                "Refusing to replace the initialized authoritative database marker. "
                f"Recorded: {existing.database_path}; requested: {authoritative}; "
                f"DATA_DIR={paths.data_dir}."
            )
        return
    temp: Optional[Path] = None
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        temp = _temp_for(paths.layout_state_path)
        payload = {
            "layout_version": LAYOUT_VERSION,
            "initialized": True,
            "initialized_at": datetime.now(timezone.utc).isoformat(),
            "database_path": str(authoritative),
        }
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, paths.layout_state_path)
    except Exception as exc:
        raise RuntimeLayoutError(
            f"Failed to write runtime layout marker at {paths.layout_state_path}; "
            f"database was left intact at {authoritative}: {exc}"
        ) from exc
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                # Preserve the normalized startup-blocking error above even if
                # the filesystem also refuses best-effort temp cleanup.
                pass
