"""
BUG-013 regression tests — Portable build layout.

We cannot execute a Windows .exe on Linux, but we CAN prove the source-
side wiring is correct:

  * `main.py` sets PLAYWRIGHT_BROWSERS_PATH before any Playwright import.
  * persistent writable folders are prepared under LOCALAPPDATA, never next
    to the fake executable.
  * `BonusReloadBot.spec` bundles `pw-browsers/`, `config/`,
    `service_account.json.example`.

The actual "run without Python / Playwright / Chromium" verification MUST
be performed on the Windows target machine — see the Windows Verification
Checklist in HARDENING_REPORT_v1.1.md.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def frozen_bundle(tmp_path, monkeypatch):
    """Build a fake `_MEIPASS` bundle + a fake .exe directory that mirror
    the PyInstaller onedir output layout."""
    resource = tmp_path / "_internal"
    app = tmp_path / "app"
    resource.mkdir()
    app.mkdir()

    # Bundle content
    (resource / "pw-browsers").mkdir()
    (resource / "pw-browsers" / "chromium.marker").write_text("ok")
    (resource / "config").mkdir()
    (resource / "config" / "config.json").write_text('{"panel_url": ""}')
    (resource / "config" / "selectors.json").write_text('{}')
    (resource / "credentials").mkdir()
    (resource / "credentials" / "service_account.json.example").write_text("{}")

    fake_exe = app / "Bonus Reload Bot.exe"
    fake_exe.write_text("stub")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(resource), raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    # Clean any env var that would mask the test.
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", raising=False)
    monkeypatch.delenv("BONUS_RELOAD_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    yield app, resource, tmp_path / "local" / "BonusReloadBot"


def test_frozen_layout_seeds_folders(frozen_bundle):
    app, resource, data = frozen_bundle
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    boot_end = src.index("# Now the app can import Playwright/Qt safely.")
    ns: dict = {"__name__": "main_isolated"}
    exec(compile(src[:boot_end], str(ROOT / "main.py"), "exec"), ns)

    # Playwright bootstrap ran at import-time; writable layout is explicit.
    assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == resource / "pw-browsers"
    assert os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"

    ns["prepare_config"](ns["RUNTIME_PATHS"])
    config = json.loads(ns["RUNTIME_PATHS"].config_path.read_text())
    ns["prepare_runtime"](ns["RUNTIME_PATHS"], config)
    for sub in ("logs", "screenshots", "credentials", "browser_profile_bonus_reload"):
        assert (data / sub).is_dir()
        assert not (app / sub).exists()
    assert (data / "config" / "config.json").exists()
    assert (data / "credentials" / "service_account.json.example").exists()



def test_frozen_layout_seeds_folders_without_pyside(frozen_bundle, monkeypatch):
    """PySide6-free variant that just exercises the runtime-layout logic
    from `main._ensure_runtime_layout` + `_prime_playwright_env`."""
    app, resource, data = frozen_bundle

    # Extract the two bootstrap helpers with a tiny AST-free re-import.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "main_isolated", ROOT / "main.py"
    )
    # We can't actually execute the module because it will try to import
    # PySide6 near the bottom; instead we exec the bootstrap portion only.
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    boot_end = src.index("# Now the app can import Playwright/Qt safely.")
    boot_src = src[:boot_end]

    ns: dict = {"__name__": "main_isolated"}
    exec(compile(boot_src, str(ROOT / "main.py"), "exec"), ns)

    # Import bootstrap only primes bundled Chromium and resolves stable paths;
    # it never writes production state beside the executable.
    assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == resource / "pw-browsers"
    assert os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"
    ns["prepare_config"](ns["RUNTIME_PATHS"])
    ns["prepare_runtime"](ns["RUNTIME_PATHS"], {"browser": {}})
    assert data.is_dir()
    assert not (app / "config").exists()


def test_spec_bundles_required_assets():
    spec = (ROOT / "BonusReloadBot.spec").read_text(encoding="utf-8")
    # Playwright driver + JS resources
    assert 'collect_data_files("playwright")' in spec
    # Chromium
    assert 'pw-browsers' in spec
    # Config templates + credentials example
    assert 'project_dir / "config"' in spec
    assert 'service_account.json.example' in spec
    # One-folder mode (portable), windowed
    assert "console=False" in spec
    assert "COLLECT(" in spec


def test_build_bat_installs_chromium_into_local_pw_browsers():
    bat = (ROOT / "build_portable.bat").read_text(encoding="utf-8")
    assert "PLAYWRIGHT_BROWSERS_PATH=%CD%\\pw-browsers" in bat
    assert "python -m playwright install chromium" in bat
    assert "pyinstaller" in bat.lower()
    assert "%%LOCALAPPDATA%%\\BonusReloadBot" in bat
    assert 'mkdir "%OUT%\\logs"' not in bat
    assert "processed.db" in bat  # explicit warning not to deploy production state
