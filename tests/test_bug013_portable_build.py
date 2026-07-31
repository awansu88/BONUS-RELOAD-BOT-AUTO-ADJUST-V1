"""
BUG-013 regression tests — Portable build layout.

We cannot execute a Windows .exe on Linux, but we CAN prove the source-
side wiring is correct:

  * `main.py` sets PLAYWRIGHT_BROWSERS_PATH before any Playwright import.
  * `_ensure_runtime_layout()` creates every required folder next to the
    fake .exe (frozen mode) and seeds `config/` from the bundle when
    missing.
  * `BonusReloadBot.spec` bundles `pw-browsers/`, `config/`,
    `service_account.json.example`.

The actual "run without Python / Playwright / Chromium" verification MUST
be performed on the Windows target machine — see the Windows Verification
Checklist in HARDENING_REPORT_v1.1.md.
"""

from __future__ import annotations

import importlib
import os
import shutil
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
    yield app, resource


def test_frozen_layout_seeds_folders(frozen_bundle):
    app, resource = frozen_bundle
    # `main` imports PySide6 for the GUI shell. Skip the full-import
    # variant when PySide6 isn't available on the CI host; the folder-
    # seeding logic itself is exercised by the smaller `_ensure_runtime_layout`
    # test below.
    pytest.importorskip("PySide6")

    # Import main fresh in this environment.
    sys.modules.pop("main", None)
    main = importlib.import_module("main")  # noqa: F841

    # The bootstrap ran at import-time.
    assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == resource / "pw-browsers"
    assert os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"

    for sub in ("logs", "screenshots", "credentials", "browser_profile_bonus_reload"):
        assert (app / sub).is_dir(), f"missing runtime folder: {sub}"

    # config/ was seeded from the bundle
    assert (app / "config" / "config.json").exists()
    # placeholder credentials copied
    assert (app / "credentials" / "service_account.json.example").exists()

    # Clean up singleton logger the bootstrap may have created.
    from core.logger import AppLogger
    AppLogger.reset()


def test_frozen_layout_seeds_folders_without_pyside(frozen_bundle, monkeypatch):
    """PySide6-free variant that just exercises the runtime-layout logic
    from `main._ensure_runtime_layout` + `_prime_playwright_env`."""
    app, resource = frozen_bundle

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

    # After the bootstrap block executed, environment + folders must exist.
    assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == resource / "pw-browsers"
    assert os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"
    for sub in ("logs", "screenshots", "credentials", "browser_profile_bonus_reload"):
        assert (app / sub).is_dir(), f"missing runtime folder: {sub}"
    assert (app / "config" / "config.json").exists()
    assert (app / "credentials" / "service_account.json.example").exists()


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
    # Seeds runtime folders next to the .exe (never bundled inside _internal).
    for sub in ("config", "credentials", "logs", "screenshots",
                "browser_profile_bonus_reload"):
        assert sub in bat, f"build_portable.bat should seed {sub}/"
