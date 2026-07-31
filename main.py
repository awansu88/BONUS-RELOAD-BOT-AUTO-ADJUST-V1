"""
Bonus Reload Automation - Entry point.

Portable-mode aware:

    * When frozen by PyInstaller (--onedir), `sys._MEIPASS` points at the
      `_internal/` folder that ships next to the .exe. Read-only bundled
      resources (Playwright Chromium + driver, config templates) live there.
    * The APP_DIR (writable side of the app — where the .exe sits) hosts
      config/, credentials/, browser_profile_bonus_reload/, logs/,
      screenshots/ and processed.db so the operator can edit them.
    * When running from source, both paths collapse to the project root.

Playwright is redirected to the bundled Chromium via
`PLAYWRIGHT_BROWSERS_PATH` before *any* Playwright import so the app
never touches `%LOCALAPPDATA%\\ms-playwright`.
"""

from __future__ import annotations

import json
import os
import shutil
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
    """Directory sitting next to the .exe (writable runtime state)."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


RESOURCE_DIR = _resource_dir()
APP_DIR = _app_dir()


def _prime_playwright_env() -> None:
    """Point Playwright at the bundled Chromium."""
    bundled = RESOURCE_DIR / "pw-browsers"
    if bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
    # Prevent Playwright from ever trying to download Chromium at runtime.
    os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")


def _ensure_runtime_layout() -> None:
    """Create missing runtime folders and seed config/ on first launch."""
    for sub in ("logs", "screenshots", "credentials", "browser_profile_bonus_reload"):
        (APP_DIR / sub).mkdir(parents=True, exist_ok=True)

    # If shipped from a frozen build, the config folder is bundled next to
    # the .exe. When missing (first run after a fresh copy that lost it),
    # restore it from the bundled template inside _internal/.
    runtime_config = APP_DIR / "config"
    if not runtime_config.exists():
        bundled_config = RESOURCE_DIR / "config"
        if bundled_config.exists():
            shutil.copytree(bundled_config, runtime_config)
        else:
            runtime_config.mkdir(parents=True, exist_ok=True)

    # Placeholder credentials example so the operator knows where to drop
    # their key file.
    cred_example = RESOURCE_DIR / "credentials" / "service_account.json.example"
    dest_example = APP_DIR / "credentials" / "service_account.json.example"
    if cred_example.exists() and not dest_example.exists():
        try:
            shutil.copy2(cred_example, dest_example)
        except Exception:
            pass


# Run these two BEFORE the Playwright/Qt imports.
_prime_playwright_env()
_ensure_runtime_layout()


# =============================================================================
# Now the app can import Playwright/Qt safely.
# =============================================================================
from PySide6.QtWidgets import QApplication, QMessageBox     # noqa: E402

from core.database import DatabaseService                    # noqa: E402
from core.diagnostics import run_diagnostics                 # noqa: E402
from core.logger import AppLogger                            # noqa: E402
from ui.dashboard import Dashboard                           # noqa: E402


CONFIG_PATH = APP_DIR / "config" / "config.json"
SELECTORS_PATH = APP_DIR / "config" / "selectors.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Bonus Reload Automation")

    try:
        config = _load_json(CONFIG_PATH)
        selectors = _load_json(SELECTORS_PATH)
    except Exception as exc:
        QMessageBox.critical(None, "Startup error", f"Could not load configuration:\n{exc}")
        return 1

    # Resolve paths relative to APP_DIR (never CWD).
    cred_path = Path(config.get("google_credentials", "credentials/service_account.json"))
    if not cred_path.is_absolute():
        cred_path = APP_DIR / cred_path
    config["google_credentials"] = str(cred_path)

    db_path = Path(config.get("sqlite_path", "processed.db"))
    if not db_path.is_absolute():
        db_path = APP_DIR / db_path

    # Rewrite user_data_dir to an absolute path anchored at APP_DIR so the
    # profile always lives next to the .exe, never inside _internal.
    browser_conf = config.setdefault("browser", {})
    profile_name = browser_conf.get("user_data_dir", "browser_profile_bonus_reload")
    profile_path = Path(profile_name)
    if not profile_path.is_absolute():
        profile_path = APP_DIR / profile_name
    profile_path.mkdir(parents=True, exist_ok=True)
    browser_conf["user_data_dir"] = str(profile_path)

    (APP_DIR / "logs").mkdir(exist_ok=True)
    AppLogger.get(log_dir=str(APP_DIR / "logs"))
    logger = AppLogger.get()
    logger.info(f"Application started ({config.get('version', 'v1.0.0')})")
    logger.info(
        f"Portable mode: {'frozen' if _is_frozen() else 'source'} "
        f"| app={APP_DIR} | res={RESOURCE_DIR}"
    )

    # -----------------------------------------------------------------
    # Startup diagnostics (BUG-014)
    # WARN-only: missing / read-only paths never stop the app.
    # -----------------------------------------------------------------
    try:
        diag = run_diagnostics(
            app_dir=APP_DIR,
            resource_dir=RESOURCE_DIR,
            config_path=CONFIG_PATH,
            selectors_path=SELECTORS_PATH,
            credentials_path=cred_path,
            sqlite_path=db_path,
            logs_dir=APP_DIR / "logs",
            screenshots_dir=APP_DIR / "screenshots",
            browser_profile_dir=profile_path,
            logger_file_handler_ok=logger.file_handler_ok,
            logger_file_handler_error=logger.file_handler_error,
        )
        for line in diag.summary().splitlines():
            (logger.info if diag.all_ok else logger.warn)(line)
    except Exception as exc:
        # Diagnostics themselves must NEVER crash the app.
        logger.warn(f"Startup diagnostics failed: {exc}")

    try:
        db = DatabaseService(str(db_path))
    except Exception as exc:
        QMessageBox.critical(None, "SQLite error", f"Could not open database:\n{db_path}\n\n{exc}")
        return 2

    AppLogger.get().info(f"SQLite ready: {db_path.name} ({db.total_count():,} rows)")

    window = Dashboard(
        config=config, selectors=selectors,
        config_path=CONFIG_PATH, db=db,
    )
    window.show()
    exit_code = app.exec()
    db.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
