# -*- mode: python ; coding: utf-8 -*-
"""
BonusReloadBot.spec — PyInstaller spec for the portable Windows build.

Two goals:
    1. Bundle the Playwright Chromium build so the .exe works on a
       Windows PC that has no Python / no Playwright / no Chromium
       installed anywhere.
    2. Keep every writable file (config/, credentials/, logs/,
       screenshots/, processed.db, browser_profile_bonus_reload/)
       *next to* the .exe rather than inside `_internal/`.

Prerequisite (see build_portable.bat):
    * `pw-browsers/` must sit next to this spec BEFORE running PyInstaller.
      Populate it with:
          set PLAYWRIGHT_BROWSERS_PATH=%CD%\\pw-browsers
          python -m playwright install chromium
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None
project_dir = Path(SPECPATH).resolve()

# ---------------------------------------------------------------- data files
datas = []

# Bundle every file needed by the Playwright driver + resources.
# `collect_data_files("playwright")` picks up the Node driver and JS resources.
datas += collect_data_files("playwright")

# Bundle the Chromium build we prepared under pw-browsers/.
pw_browsers = project_dir / "pw-browsers"
if not pw_browsers.exists():
    raise SystemExit(
        "pw-browsers/ is missing — run:\n"
        '    set PLAYWRIGHT_BROWSERS_PATH=%CD%\\pw-browsers\n'
        "    python -m playwright install chromium\n"
        "before building."
    )
datas.append((str(pw_browsers), "pw-browsers"))

# Bundle default config templates + selectors so we can seed them on first run.
datas.append((str(project_dir / "config"), "config"))
datas.append(
    (str(project_dir / "credentials" / "service_account.json.example"),
     "credentials")
)

# ---------------------------------------------------------------- binaries
binaries = []
# Grab any DLLs greenlet/pyside6 might pull in.
binaries += collect_dynamic_libs("PySide6")

# ---------------------------------------------------------------- hidden imports
hiddenimports = [
    "core.database",
    "core.sheet_service",
    "core.queue_manager",
    "core.memory_cache",
    "core.validator",
    "core.panel_service",
    "core.logger",
    "ui.dashboard",
    "gspread",
    "google.auth",
    "google.oauth2.service_account",
    "playwright",
    "playwright.sync_api",
    "playwright._impl",
]


# =============================================================================
a = Analysis(
    ["main.py"],
    pathex=[str(project_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest", "sqlite3.tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Bonus Reload Bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX corrupts some PySide6 DLLs; keep off
    console=False,       # windowed app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Bonus Reload Bot",
)
