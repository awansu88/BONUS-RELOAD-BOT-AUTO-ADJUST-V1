# Bonus Reload Auto-Adjustment Bot — PRD

## Original problem statement
Deliver **Production Hardening v1.2.0** on top of the production-stable
`hardening-v1.1.0-final` branch of the Windows/PySide6 Bonus Reload
Automation bot (repo: napakappa/BOT-RELOAD-AUTO-ADJUSTMENT).

Explicit constraints from the operator:
- Do **not** modify production engine (queue manager, validator,
  sheet service, panel service business logic, dashboard worker step).
- Wrap frozen components rather than replacing them.
- Never introduce nested Git repositories.
- The workspace root **must** be the same repo tracked by Save to GitHub.
- No new business logic, no refactoring, no library replacements.

## Architecture
Single-PC PySide6 desktop application:
- Python 3.13 + PySide6 6.10.3 + Playwright 1.55 (Chromium)
- Read-only Google Sheets via gspread, write-only SQLite (WAL) as the
  single source of truth.
- Portable Windows build via PyInstaller (`BonusReloadBot.spec` +
  `build_portable.bat`) — no Python or Playwright required on the
  target PC.
- v1.2 hardening infrastructure lives under `core/` and `ui/` alongside
  the frozen engine — it wraps but never rewrites production code.

## Users
Single operator on a Windows PC. Long-running (weeks) production usage.
Needs minimum intervention.

## Core requirements (static)
- Zero-touch monitoring — bot restarts itself where possible.
- Single browser context, single SQLite writer, no threads.
- Bulletproof deduplication (SQLite PRIMARY KEY on tx_id).
- Bonus rule keyed by the **original transaction date** (never
  processed_at).

## What's been implemented

### v1.0.0 → v1.1.0 (previous sessions, frozen)
- Continuous monitoring mode.
- Adaptive polling.
- Manual bonus race-condition safe (BUG-012).
- Portable Windows build + bundled Chromium (BUG-013).
- Startup diagnostics, logger fallback (BUG-014).
- Daily bonus keyed by transaction date (BUG-015).

### v1.2.0 — Production Hardening (this session, 2026-01-31)
**Phase B — Stability**
- B-1  Google Sheets retry ladder (5, 10, 20, 40, 60 s) wrapping every
  Sheet-touching entry point.
- B-2  Playwright auto-recovery — watchdog reopens the persistent
  Chromium profile when the browser dies while worker is active.
- B-3  Health Watchdog QTimer sampling memory / threads / handles /
  browser count / QTimer count / SQLite / Google.
- B-4  Memory stability — log buffer FIFO already capped; health
  history capped at 60 samples; snapshots do not accumulate.
- B-5  Resource-leak detection with configurable thresholds; warnings
  logged only on state changes.
- B-6  Global `sys.excepthook` captures uncaught exceptions with full
  timestamp / module / stack trace; `core.recovery.safe_run` used
  everywhere non-critical.
- B-7  Graceful shutdown — `closeEvent` stops every QTimer, checkpoints
  WAL, closes browser, marks clean_exit in `runtime_state.json`.
- B-8  Crash recovery — `runtime_state.json` restores spreadsheet URL /
  window geometry / last panel URL on next launch.

**Phase C — Maintenance**
- C-1  Unified **Maintenance Center** dialog (top-bar MAINTENANCE
  button) with Database / Logs / Screenshots / Backups / Diagnostics /
  Health tabs.
- C-2  SQLite maintenance with retention presets (3 / 7 / 15 / 30 /
  Custom / None) + integrity check + ANALYZE + PRAGMA optimize +
  optional VACUUM (only when worker idle).
- C-3  Automatic startup maintenance — WAL checkpoint + PRAGMA
  optimize (never VACUUM, never interrupts monitoring).
- C-4  Log maintenance — TimedRotatingFileHandler (30-file cap) plus
  operator-driven "delete older than N days".
- C-5  Screenshot maintenance — configurable retention with manual
  cleanup.
- C-6  Health diagnostics — PASS / WARNING / FAILED score, live
  metrics, exportable diagnostic report (versions + config validation
  + health snapshot).
- C-7  Configuration validation — checks config keys, selectors,
  bonus rules, credentials, panel URL, DB / profile writability with
  clear operator-facing hints.

### Files modified (additive-only where possible)
| File | Change |
|---|---|
| `main.py` | Added crash-state store bootstrap, `sys.excepthook`, startup auto-maintenance, `Dashboard()` new-kwargs wiring. |
| `ui/dashboard.py` | Added optional kwargs, health monitor, watchdog QTimer, maintenance service, MAINTENANCE button, retry-ladder around `_on_connect`, auto-recovery for panel, graceful `closeEvent`, crash-state restore. Worker step unchanged. |
| `core/database.py` | Appended safe helpers: `checkpoint_wal`, `integrity_check`, `optimize`, `count_older_than`, `is_open`. |
| `config/config.json` | Added `hardening` block (backwards-compatible). |
| `BonusReloadBot.spec` | Added v1.2 modules + `psutil` to `hiddenimports`. |
| `requirements.txt` | Appended `psutil==6.1.0`. |

### Files added
- `core/recovery.py`, `core/health.py`, `core/maintenance.py`,
  `core/config_validator.py`, `core/crash_state.py`
- `ui/maintenance_center.py`
- 5 new test modules under `tests/` (46 new tests)

### Test results
- **79/79 pytest tests pass** in ~0.8 s
- All 33 pre-existing bug tests still pass (zero regression on the
  frozen engine).
- `npm install` succeeds without `--force` / `--legacy-peer-deps`.
- `npm run build` succeeds (`build/static/js/main.4772e0a8.js`
  98.47 kB gzipped).

## Prioritized backlog

### P0 — operator-facing verification (Windows only, cannot run here)
- Run `build_portable.bat` on a Windows 11 PC with Python 3.13; confirm
  `dist\Bonus Reload Bot\Bonus Reload Bot.exe` launches and finds the
  bundled Chromium.
- Copy the folder to a clean Windows PC (no Python, no Playwright);
  confirm one full continuous run (monitoring → new row → adjustment).

### P1 — future stability improvements (not in scope for v1.2)
- Extend the retry ladder to Playwright submit_deposit calls (currently
  only wraps Sheets).
- Persist last processed_at / last watchdog snapshot on graceful
  shutdown for even faster resume UX.
- Optional Windows notification (Toast) when Health score flips to
  FAILED.

### P2 — nice-to-have
- Add a "Diagnostic Report" button on the dashboard toolbar that runs
  the same report as the Maintenance Center for a single click.
- Support a system-tray icon so the operator can minimise the app
  during long monitoring stretches.

## Next tasks (from the operator)
1. Review the `git status` output on the workspace and click
   **Save to GitHub** to push the six-file modification + six-file
   addition set.
2. Fresh-clone the branch on a Windows 11 PC and run:
   ```
   git clone
   npm install
   npm run build
   build_portable.bat
   ```
3. Smoke test the portable `Bonus Reload Bot.exe` on a clean PC.

## Test credentials
None required — everything is offline pytest / static analysis.
