# PRD - Bonus Reload Automation (Phase 1: Core Engine)

## Original Problem Statement
Build a production-ready **Windows desktop application** for automating
"Manual Deposit Adjustment" (Bonus Reload) on a web panel.
Stack: Python 3.12+, PySide6, Playwright (Chromium), gspread. No Selenium,
Redis, SQLite, RabbitMQ, OCR, AI, threads or multi-processing. Runs on
ONE Windows PC only. Google Sheets is the sole database.

## Architecture
```
project/
├── main.py                    # PySide6 entry point
├── credentials/
│    └── service_account.json  # Google service account (user-provided)
├── config/
│    ├── config.json           # Bot behaviour, rules, sheet names, panel URL
│    └── selectors.json        # All HTML selectors (never hardcoded in Python)
├── core/
│    ├── sheet_service.py      # gspread client, ID extraction, read/write
│    ├── validator.py          # Bonus tier + daily-limit rule engine
│    ├── queue_manager.py      # Batch queue (100), adaptive refill
│    ├── memory_cache.py       # daily_bonus dict + manual set
│    ├── panel_service.py      # Playwright wrapper (sync API, single browser)
│    └── logger.py             # Daily file logger + Qt signal emitter
├── ui/
│    └── dashboard.py          # Dark UI, amber accent, QTimer-driven worker
├── logs/                      # YYYY-MM-DD.log
├── screenshots/               # failed-transaction snapshots
├── requirements.txt
└── README.md
```

Threading model: single Qt event loop; a `QTimer` pumps one transaction per
tick. Playwright uses its sync API against a persistent Chromium context so
cookies (i.e. the manual login) survive between sessions and even between
app restarts.

## User personas
* **Bot Operator** — the only human interacting with the app. Pastes the
  daily Google Spreadsheet URL, opens the panel, logs in manually, and
  supervises the run.

## Core requirements (static)
* Google Sheets used only as DB. URL pasted at runtime, ID extracted.
* MASTER sheet: NO / USER ID / AMOUNT(ignored) / SHEET DATA / TIME STAMP /
  TRUE AMOUNT / STATUS / BONUS.
* MANUAL BONUS RELOAD sheet: user IDs to skip (status set to `MANUAL BONUS`).
* Bonus rule:
  * `daily_limit = 10000`
  * Deposit `>= 50000` → tier bonus `5000`
  * Deposit `>= 100000` → tier bonus `10000`
  * `bonus_to_give = min(tier_bonus, 10000 - current_daily_bonus)`
* Allowed statuses: `PROCESSED · INVALID · LIMIT · MANUAL BONUS · FAILED`.
  Empty = pending.
* Remark filled with `BONUS RELOAD AUTO` (configurable).
* Batch size 100. Adaptive polling: sheet is only re-read when the queue is
  empty.
* Manual list refreshed every 30 s (configurable). Daily bonus reloaded only
  on reconnect or when the date changes.
* Single persistent Chromium context; no page refresh; operator logs in
  manually. No credentials are ever stored by the app.
* Success detection: `<div class="alert alert-success">Deposit telah
  disubmit</div>`. Bot proceeds immediately to the next transaction.
* STOP finishes the current transaction, updates the sheet, halts the worker,
  keeps the browser open, returns to Idle.
* Recovery: on restart, only rows with empty STATUS are picked up — no
  duplicates.
* Everything configurable via `config/config.json`; every selector in
  `config/selectors.json`.
* PyInstaller-compatible layout.

## What's been implemented (Phase 1 — 2026-01)
* ✅ Full project skeleton exactly as spec'd.
* ✅ `SheetService`: URL → ID parser, connect + validate (MASTER + MANUAL
  BONUS RELOAD present, read + write probe), bulk MASTER read, manual set
  read from **Column B only** (Column A ignored, values trimmed), per-row
  batched update (STATUS / BONUS / SHEET DATA).
* ✅ `Validator`: tier + daily-limit engine, INVALID / LIMIT / READY /
  MANUAL BONUS classification. 12/12 spec-derived unit tests pass.
* ✅ `MemoryCache`: daily bonus dict, manual set, stale-by-date detection,
  in-place bonus increment.
* ✅ `QueueManager`: 100-row batch, preview items with validation results,
  adaptive refill, per-status counters.
* ✅ `PanelService`: **persistent Chromium** (`launch_persistent_context`
  with `user_data_dir=browser_profile/`) — cookies + login survive app
  restarts, Windows reboots, and even manual `python main.py` re-runs; the
  operator only re-logs in when the website session expires. `open_panel`
  rejects empty/invalid URL, `attach` after manual login, `submit_deposit`
  fills form, submits, waits for the success alert and verifies text. No
  page refresh. Screenshot on failure.
* ✅ `AppLogger`: `logs/YYYY-MM-DD.log` daily rotation, Qt-signal listener
  for the live log, session-wide export buffer.
* ✅ `Dashboard` (adjusted):
  * Modern dark UI, amber/gold accent.
  * Persistent **top bar**: app title, `Last sync: YYYY-MM-DD HH:MM:SS`,
    amber-boxed **v1.0.0** badge, SETTINGS.
  * Spreadsheet URL and Panel URL are auto-saved to `config.json` and
    pre-filled on next launch.
  * Actions grid (2 × 3): OPEN PANEL · READY · **REFRESH QUEUE** ·
    PREVIEW QUEUE · START · STOP. REFRESH QUEUE re-reads the sheet on
    demand; PREVIEW QUEUE only shows the currently-loaded queue (and
    triggers a one-time refill if nothing is loaded yet).
  * Settings dialog persists Panel URL, Daily Limit, Batch Size, Reload
    Interval, Polling Delay, Remark, Credentials path.
  * Preview Queue dialog with color-coded status.
  * Export TXT / CSV.
* ✅ Single-QTimer worker loop — no threads, no processes.
* ✅ Manual-list refresh (every 30 s) is **deferred while the worker is
  running** to save Google API quota; the pending refresh fires as soon
  as the queue drains or the operator stops.
* ✅ STOP is graceful (finishes current transaction, updates sheet).
* ✅ Recovery: only empty-STATUS rows are read.
* ✅ Validated headlessly (offscreen render + core-logic unit checks +
  Column-B trimming unit check).

## Bug Fix & Performance pass (2026-01)

Applied without any architectural change:

* **BUG-001 · Browser Lifecycle Recovery** — `PanelService` now exposes
  `is_alive()` and an internal `_dispose()`. `open_panel()` detects a dead
  context/page (X closed by operator), disposes references, and launches a
  brand-new persistent context so no restart is ever required.
  `submit_deposit()` short-circuits with `"browser closed"` when the window
  vanishes mid-run. A 2 s `panel_timer` in the dashboard polls
  `is_alive()` — as soon as the operator closes the browser, Panel Status
  reverts to **Closed**, START is re-disabled, and the worker (if running)
  halts gracefully.
* **BUG-002 · Daily Bonus Partial Calculation** — the worker now
  **revalidates every READY item at process-time** against the current
  daily-bonus cache (not just at refill). Same-user duplicates within a
  100-row batch are correctly capped:
  50k → grant 5000 (cache 5000) → next 100k → grant 5000 (remaining) →
  next 100k → LIMIT / 0. Verified with a dedicated regression test.

* **BUG-002 (reopened) · Preview cumulative simulation** — the *Preview*
  queue now also accumulates per-user bonus while building. `refill()`
  keeps a local `simulated[user] = today + granted_so_far` snapshot and
  passes it into the validator for every next row. Result: Preview and
  Worker always output identical bonus values. Verified for
  `capitsandal` with 50k → 5000, 122k → 5000, 100k → LIMIT.
* **BUG-003 · Wrong Sheet Update** — `update_row()` writes **only** to
  columns G (STATUS) and H (BONUS). The `sheet_data` (column D) column
  is never touched. The remark "BONUS RELOAD AUTO" is still sent to the
  panel textarea only.
* **BUG-004 · Queue Filtering** — the worker queue holds **READY items
  only**. Non-READY items (INVALID / LIMIT / MANUAL BONUS) never enter the
  worker loop. Preview Queue continues to show all validation results
  (`preview_items()`).
* **BUG-005 · Worker Processing** — non-READY sheet updates are flushed
  in a single `batch_update` at refill time via a new
  `SheetService.batch_update_statuses()`. The worker tick therefore never
  spends CPU/API on skipped items.
* **Performance · Cleaner Live Log** — replaced per-item skip lines with a
  single `Queue Loaded` block:
  ```
  Queue Loaded
    READY        : 87
    MANUAL BONUS : 5 (Skipped)
    INVALID      : 3 (Skipped)
    LIMIT        : 5 (Skipped)
  ```
  Followed only by real transaction lines
  (`username  Deposit N  Bonus M  SUCCESS`).

## Production hardening pass (2026-01)

### Improvement #1 · Continuous Monitoring Mode
* Operator now presses **START only once**. When the queue empties the
  worker enters **Monitoring** state instead of stopping:
  * Bot status label → *Monitoring*
  * Current-Processing status pill → blue **MONITORING**
  * Progress label → *Waiting for New Transactions*
  * ETA area → countdown `Next Refresh mm:ss`
* Every `monitoring_interval_sec` (default **10 s**, editable in
  Settings) the bot polls the sheet for new pending rows. Between polls
  the worker sleeps — Google API traffic stays minimal.
* When a new READY row appears, the worker transparently exits
  Monitoring and resumes processing without operator interaction.
* STOP during Monitoring finalises immediately; STOP during processing
  still waits for the current transaction (existing semantics).
* Implemented as three tiny helpers (`_enter_monitoring`,
  `_exit_monitoring`, `_tick_monitoring`) plus a `state="monitoring"`
  branch at the top of `_worker_step`. Single-QTimer architecture
  unchanged.

### Improvement #2 · Queue Synchronisation (race-condition proof)
* `QueueManager.refill()` now:
  1. Reads pending rows.
  2. Validates all → categorises READY / INVALID / MANUAL BONUS / LIMIT.
  3. Calls `sheet.batch_update_statuses(skips)` — gspread's
     `batch_update` is synchronous, so control returns only after Google
     confirms the change.
  4. **Only then** commits `self._ready = ready` and `self._last_preview`.
* If the batch update raises, the READY queue is *not* populated at all.
  The exception propagates to the caller; the next refill retries.
* Worker therefore can never begin processing a READY row before the
  sheet reflects the correct status for every skipped row.
* Regression test asserts both paths: `RuntimeError` from
  `batch_update_statuses` leaves `ready_count() == 0`; success populates
  the ready queue *after* the confirmation call.

### Requirements pinned for production
```
PySide6==6.10.3
playwright==1.55.0
gspread==6.1.2
google-auth==2.34.0
google-auth-oauthlib==1.2.1
```
Target: Python **3.13+**. Verified on the operator's Windows machine.

### README rewritten
Complete production doc: install, Playwright, service account placement,
sheet sharing, first-time setup, continuous-monitoring workflow,
Emergency Stop / browser recovery, PyInstaller build.

## Phase 2 · Dashboard Enhancement (2026-01)

UI-only changes; engine untouched.

* Left pane is now inside a `QScrollArea` and contains, in order:
  Status → Actions → **Current Processing** → **Progress** →
  **Queue Summary** → **Statistics**.
* **Current Processing card** — big amber user id, small deposit + granted
  bonus, right-aligned status pill (`Idle` / `PROCESSING` / `SUCCESS` /
  `FAILED`, color-coded).
* **Progress card** — `Processed X / Y` label with amber `QProgressBar`
  and right-aligned `ETA hh:mm:ss` computed from the running average
  submit time × remaining READY.
* **Queue Summary card** — four color-coded chips: READY (green),
  MANUAL BONUS (blue), INVALID (red), LIMIT (amber). Values are pulled
  from the latest `QueueStats`.
* **Statistics card** — 8 KPIs in a 2 × 4 grid, each in its own
  `SubCard`:
  1. Queue Ready
  2. Processed (session cumulative)
  3. Skipped
  4. Failed
  5. Today's Bonus Paid (sum of granted bonuses)
  6. Adjustments / Minute (session-wide, `processed / elapsed_min`)
  7. Average Submit Time (avg of every panel submit round-trip)
  8. Elapsed Time (`hh:mm:ss` since START)
* A dedicated 1-second `metrics_timer` recomputes elapsed / rate /
  avg-submit / bonus-paid labels. It only runs while the worker is
  running (started on `_on_start`, stopped in `_finalise_stop`) so idle
  CPU cost is zero.
* Live log unchanged: `Queue Loaded` summary block + one line per real
  transaction (`user  Deposit N  Bonus M  SUCCESS`). No skip spam.
* All new widgets have stable `data-testid` object names
  (`current-user`, `current-deposit`, `current-bonus`, `current-status`,
  `progress-bar`, `progress-label`, `eta-label`, `qs-ready`, `qs-manual`,
  `qs-invalid`, `qs-limit`, `stat-bonus-paid`, `stat-rate`,
  `stat-avg-submit`, `stat-elapsed`).

## Prioritised backlog (Phase 2+)
* P0 — Real end-to-end run against a live spreadsheet + panel (must happen
  on operator's Windows PC because Playwright + a real login are required).
* P1 — Statistics panel with adjustments-per-minute + total processed today.
* P1 — Auto-reconnect if Google API throws transient 5xx.
* P1 — Optional Windows toast on FAILED transactions.
* P2 — PyInstaller `.spec` file committed to the repo.
* P2 — Simple retry policy for FAILED (configurable, off by default).
* P2 — CSV import mode for offline dry-runs (no sheet, no panel).

## Next tasks
1. Operator installs deps + Chromium on Windows, drops the service-account
   JSON into `credentials/`, and runs `python main.py`.
2. First live smoke test against a staging spreadsheet + panel.
3. Package with PyInstaller once smoke test passes.

## v1.0.0 Release Candidate — SQLite persistence pivot (2026-01)

**Architecture change** (persistence layer only; Dashboard, Playwright,
Validator, Monitoring, Worker, UI untouched):

* Google Sheets is now **READ ONLY** — the bot never writes STATUS,
  BONUS or any other cell back to the sheet. `SheetService` no longer
  exposes `update_row`, `batch_update_statuses`, `_probe_write` or
  `build_daily_bonus_map`. Scope narrowed to `spreadsheets.readonly` +
  `drive.readonly`.
* SQLite is now the **WRITE ONLY** processing database (`processed.db`,
  auto-created, `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`). Schema:
  ```sql
  CREATE TABLE processed_transactions (
      tx_id TEXT PRIMARY KEY,
      username TEXT NOT NULL,
      amount INTEGER, bonus INTEGER,
      result TEXT NOT NULL,           -- SUCCESS | FAILED | LIMIT | INVALID | MANUAL BONUS
      processed_at TEXT NOT NULL,
      sheet_name TEXT, timestamp TEXT
  );
  ```
* **TX_ID (Column I)** is the sole dedup key. Every outcome writes one
  row. Duplicate protection is enforced twice:
  1. `QueueManager.refill()` pre-filters via
     `DatabaseService.filter_new_tx_ids(...)` before validation.
  2. `INSERT OR IGNORE` at write time relies on the SQLite PRIMARY KEY.
* **CONNECT SHEET** validates that MASTER has non-empty headers in
  columns **B, D, E, F, I**. If any is missing the error is shown, START
  stays disabled, and no queue is built.
* Daily-bonus totals come from a single grouped query on `processed.db`
  (today, SUCCESS only). No more MASTER scan for cache seed.
* QueueItem trimmed to `tx_id · username · amount · bonus · sheet_name`
  (+ small status/timestamp helpers), `slots=True`.
* Preview is fully rebuilt on every refill (no appending).
* Manual-bonus refresh interval default bumped from **30 s → 90 s**.
* Live log capped at **500 lines FIFO** (`AppLogger._buffer` +
  `QPlainTextEdit.setMaximumBlockCount(500)`). RAM stays constant
  during multi-hour runs.

### Database dialog (new)
Top-bar **DATABASE** button opens a dialog showing Processed Today,
Total Processed, Database Size, Status. Three action buttons:
* **Export Database** — CSV dump of the table.
* **Backup Database** — copies the `.db` file to
  `processed_YYYY-MM-DD_HH-MM-SS.db` after a WAL checkpoint.
* **Maintenance** — confirmation prompt, then `VACUUM; ANALYZE;`.
  Disabled while the worker is running. Never runs automatically.

### Regression tests (all headless, no live browser required)
* Sheet header validation — missing TX_ID rejected, complete header
  accepted.
* `read_master_rows` — parses tx_id, skips rows with empty tx_id.
* Sheet write methods truly removed.
* Full end-to-end via `DatabaseService` + `QueueManager`: 5-row batch ⇒
  1 READY + 1 LIMIT + 1 MANUAL + 2 INVALID; all 4 non-READY inserted in
  one batch; second refill finds 0 pending; duplicate INSERT rejected.
* `daily_bonus_map()` reflects today's SUCCESS rows only.
* Preview cleared and rebuilt on each refill.
* `VACUUM` + `last_vacuum` marker, `export_csv`, `backup`,
  `clear_older_than(30)` all pass.

### v1.0.0 RC Production Goal — met
* Google Sheets: read-only feed.
* SQLite: write-only source of truth.
* No dependency on sheet row numbers.
* TX_ID enforced dedup (double protection).
* Constant RAM during long runs (500-line log cap, `slots` on hot
  dataclasses, no growing buffers, single connection to SQLite in WAL
  mode).

## Production packaging revision — Bundled Chromium + isolated profile (2026-01)

Ship a true **portable Windows folder**. Target machines have no Python,
no Playwright, no Chromium.

### `main.py` — frozen-aware bootstrap
* `_is_frozen()`, `_resource_dir()` (= `sys._MEIPASS` when frozen),
  `_app_dir()` (= folder holding the .exe).
* `_prime_playwright_env()` runs **before** the Playwright import:
  * `PLAYWRIGHT_BROWSERS_PATH = <resource>/pw-browsers`
  * `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = 1`
* `_ensure_runtime_layout()` auto-creates `logs/`, `screenshots/`,
  `credentials/` next to the .exe and restores `config/` from the bundle
  on first launch (or if the operator deleted it).
* Every path in `config.json` (credentials, sqlite_path,
  `browser.user_data_dir`) is resolved **relative to APP_DIR**, never
  CWD, so the operator can launch the .exe from anywhere.

### Browser profile — unique + external
* `config.json → browser.user_data_dir` renamed
  `browser_profile` → **`browser_profile_bonus_reload`**.
* Path is absolutised at startup to `<APP_DIR>/browser_profile_bonus_reload/`
  and created if missing. Cookies, session, login, cache and LocalStorage
  persist there — never bundled inside `_internal/`.
* The unique name (`_bonus_reload`) reserves the space for sibling
  automations (`_deposit`, `_withdraw`, …) with zero collision risk.

### `BonusReloadBot.spec` (new PyInstaller spec)
* Bundles **Playwright driver + JS resources**
  (`collect_data_files("playwright")`).
* Bundles the whole `pw-browsers/` folder (populated by
  `python -m playwright install chromium` with
  `PLAYWRIGHT_BROWSERS_PATH=%CD%\pw-browsers`).
* Bundles default `config/` and `service_account.json.example`.
* Windowed .exe named `Bonus Reload Bot`, one-folder mode
  (`dist\Bonus Reload Bot\`).

### `build_portable.bat` (new one-shot builder)
Automates the whole pipeline on a build machine that has Python 3.13:
1. venv + pip install requirements + pyinstaller.
2. `python -m playwright install chromium` into `pw-browsers/`.
3. `pyinstaller BonusReloadBot.spec`.
4. Copies writable seed folders (config, credentials example, empty
   logs/, screenshots/, browser_profile_bonus_reload/) next to the .exe.

### Deploy checklist
1. Copy `dist\Bonus Reload Bot\` to any Windows PC.
2. Drop the real `service_account.json` into `credentials\`.
3. Double-click `Bonus Reload Bot.exe`.
No Python / Playwright / Chromium install on the target machine.

### Verified (headless)
* Source mode: `PLAYWRIGHT_BROWSERS_PATH` correctly stays *unset* when
  there's no `pw-browsers/` folder yet; `SKIP_BROWSER_DOWNLOAD=1` always
  set.
* Simulated frozen mode
  (`sys.frozen=True`, `sys._MEIPASS=/tmp/fake_bundle`,
  `sys.executable=/tmp/fake_exe/Bonus Reload Bot.exe`):
  * `PLAYWRIGHT_BROWSERS_PATH = /tmp/fake_bundle/pw-browsers` (bundled).
  * `APP_DIR = /tmp/fake_exe`, `RESOURCE_DIR = /tmp/fake_bundle`.
  * `logs/`, `screenshots/`, `credentials/` auto-created next to the fake
    .exe; `config/` seeded from the bundle.
  * `browser.user_data_dir` resolves to
    `/tmp/fake_exe/browser_profile_bonus_reload`.

## Production Hardening v1.1 (2026-01)

Continuation from the v1.0.0 RC. Architecture unchanged.
Full details: `HARDENING_REPORT_v1.1.md` at the repository root.

### Repository cleanup (Conservative)
Removed (all unreferenced by the desktop app):
* `frontend/`  — leftover React/CRACO scaffold.
* `backend/`   — leftover FastAPI + MongoDB scaffold.
* `test_result.md` — Emergent testing-boilerplate protocol file.
* `.gitconfig` — committed local git identity.
* `.gitignore` trimmed of frontend-only patterns.

### BUG-012 · Manual Bonus Race — FIXED
* Root cause: `_worker_step` re-validated against a cache the
  dashboard timer refused to refresh while `state == "running"`.
* Fix: contractual pre-submit sequence in `_worker_step` — SQLite dedup
  → **fresh** `sheet.read_manual_set()` (TTL 2 s) → daily-bonus
  revalidation → submit.
* Static assertion in `tests/test_bug012_manual_bonus_race.py`
  guarantees the source-code ordering never regresses.

### BUG-013 · Portable Build — HARDENED
* `main.py::_ensure_runtime_layout` now includes
  `browser_profile_bonus_reload/` alongside `logs/`, `screenshots/`,
  `credentials/`, so every writable folder exists before Playwright
  is touched.
* Startup diagnostics now emit a WARN if `<bundle>/pw-browsers/` is
  missing in frozen mode.
* Spec + `build_portable.bat` verified by pytest (asset bundling,
  Chromium install path, seed folders).
* Windows-side verification checklist documented in
  `HARDENING_REPORT_v1.1.md §3.5`.

### BUG-014 · Logger Startup — HARDENED
* `core/logger.py` rewritten: `mkdir` best-effort, `FileHandler`
  wrapped in `try/except`, `ConsoleHandler(stderr)` always attached,
  `file_handler_ok` / `file_handler_error` fields exposed for the
  diagnostics module.
* New `core/diagnostics.py` — `run_diagnostics()` checks Config /
  Credentials / SQLite / Logs / Screenshots / Browser Profile /
  Bundled Chromium and reports PASS/WARN. Never raises.

### BUG-015 · Daily Bonus by Transaction Timestamp — FIXED
* Root cause: `daily_bonus_map()` keyed by `processed_at` (execution
  time). The rule engine consumed it. Cross-midnight adjustments
  therefore reset the user's daily cap incorrectly.
* Fix: new SQLite column `timestamp_date` (backfilled on first open of
  legacy DBs), new primitive
  `DatabaseService.daily_bonus_for_transaction_date(username, iso)`,
  new `core/timestamp_utils.parse_transaction_date`. Both
  `QueueManager.refill` and `_worker_step` now key daily-bonus totals
  by the ORIGINAL sheet timestamp date. `processed_at` is untouched
  and continues to power audit-history KPIs only.
* Verified against the exact `maknyus27` regression scenario in
  `tests/test_existing_behaviour.py`.

### Latent bug fix (non-business-rule)
`ui/dashboard.py::PreviewDialog` used stale attribute names
(`it.user_id`, `it.deposit`) that would have raised `AttributeError`
on every PREVIEW QUEUE click. Renamed to `username` / `amount`.

### Regression status
`pytest tests/` → 32 passed, 1 skipped (PySide6 unavailable on Linux CI).

### Backlog (v1.2+)
* Widen `parse_transaction_date` if the operator's sheet exposes a new
  format (P2).
* Move the `_MANUAL_FRESH_TTL_SEC` value into `config.json` (P2).
* Optional: expose a `Startup Diagnostics` viewer in the Dashboard
  sidebar (P2).

### BUG-016 · Frontend `npm install` peer-dep conflict — FIXED
* Reported: `react-day-picker@8.10.1` requires `date-fns@^2.28.0 || ^3.0.0`
  but repo pinned `date-fns@4.1.0`; also peer-required `react ^16-18`
  vs pinned `react@19.0.0`. `npm install` failed with ERESOLVE.
* Root cause: `react-day-picker@8` predates React 19 and date-fns v4;
  yarn had been silently tolerating the mismatch. `frontend/src/`
  never imports `date-fns` directly — it's pulled in only by
  `react-day-picker`.
* Smallest compatible fix: bump `react-day-picker` `8.10.1` → `9.14.0`.
  v9 accepts `react >= 16.8.0` and depends on `date-fns@^4.1.0`
  directly (no peer any more). No other package touched.
* `frontend/src/components/ui/calendar.jsx` migrated from the v8
  classNames + `IconLeft`/`IconRight` API to the v9 classNames +
  `Chevron` API. Behavior unchanged.
* `frontend/package-lock.json` regenerated (`npm@10.8.2` /
  `node@20.20.2`) and committed for reproducibility.
* Verified: `npm install` + `CI=true npm run build` succeed on a fresh
  copy without `--force` or `--legacy-peer-deps`.
* Earlier draft removed `frontend/` entirely as "unused scaffold"; that
  decision was reverted for BUG-016 — the operator wants it installable.

### Also reverted from initial cleanup
* `frontend/` — kept (see BUG-016).
* `.gitignore` — Node/frontend ignores restored.
