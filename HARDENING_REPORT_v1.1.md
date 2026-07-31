# Production Hardening v1.1 — Auto Adjust Bonus Reload Bot

Cycle owner: E1  ·  Base: `v1.0.0-RC` (existing GitHub HEAD)
Date: 2026-01

This report documents every change applied on top of the existing
Release Candidate. Architecture (Python 3.13 + PySide6 + Playwright
Persistent Context + SQLite WAL + Google Sheets read-only + Continuous
Monitoring + Queue Manager + Worker + Dashboard v2 + Portable Build)
was **NOT redesigned** — only bugs were fixed and dead files removed.

---

## 1. Repository Cleanup

### 1.1  Files removed

| Path                | Category                | Reason                                                                                    | Verified unreferenced by                       |
| ------------------- | ----------------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `backend/`          | Emergent scaffold       | Leftover FastAPI + MongoDB `server.py` + Motor + Bcrypt + Pandas requirements. Zero code paths reach it from `main.py`, `core/*`, `ui/*`, or the PyInstaller spec.        | `grep -rn backend --include='*.py' --include='*.spec' --include='*.bat' --include='*.md' --include='*.json'` produced 0 non-boilerplate matches. |
| `test_result.md`    | Emergent testing boilerplate | Deprecated `test_result.md` protocol file from the Emergent scaffold — it was the ONLY file that mentioned the removed `backend/` folder. Not consumed by pytest. | `pytest tests/` succeeds after removal (32 passed / 1 skipped). |
| `.gitconfig`        | Committed identity      | Committed local `user.email=github@emergent.sh` — should never live in the repo. | `git config --list --local` still works.       |

### 1.1a  Files RETAINED (revised from an earlier draft)

An earlier draft of this cleanup also removed `frontend/` on the same "unused Emergent scaffold" grounds. On second review, and after the user flagged the `npm install` failure in `frontend/` (see §7. **BUG-016**), that decision was reverted — the operator wants `frontend/` to remain installable. It is now kept as-is and its dependency tree was fixed instead.

### 1.2  `.gitignore` reduced (safe)

Removed patterns for tools this project never uses: `node_modules`, `.pnp`, `.yarn`, `.next`, `next-env.d.ts`, `.chainlit`, `chainlit.md`, `.vercel`, `android-sdk/`, `agenthub/agents/youtube/db`. These were ancillary to the removed `frontend/` and add noise.

Kept every project-specific ignore (`credentials/service_account.json`, `browser_profile_bonus_reload/`, `logs/*.log`, `screenshots/*.png`, `processed.db*`, `build/`, `dist/`, `pw-browsers/`, etc.).

### 1.3  What stayed (deliberately)

* `main.py`, `BonusReloadBot.spec`, `build_portable.bat`, `requirements.txt` — pinned production entry-points.
* `config/`, `credentials/service_account.json.example`, `memory/PRD.md`, `test_reports/pytest/.gitkeep` — intentional.
* `.emergent/emergent.yml` — Emergent platform manifest.
* `core/panel_service.py::_maybe_select`'s optional-select fallback and `PanelService._dispose()` — unchanged, still needed for browser lifecycle.

No renaming. No module moves. No import hierarchy changes.

---

## 2. BUG-012 — Manual Bonus Race Condition

### 2.1  Root Cause

The queue is refilled once per polling cycle and validated only at that
moment. `ui/dashboard.py::_worker_step` re-validated at process-time,
but only against `MemoryCache.manual_set()` — which the dashboard timer
**explicitly refuses to refresh while `state == "running"`** to save
Google API quota (`ui/dashboard.py::_reload_manual_list`, lines 1062-1077 of
the pre-fix file). During an active run the cache therefore reflected
the manual list as it was **at refill time**. If an operator added a
User ID to the `MANUAL BONUS RELOAD` sheet after refill but before the
worker reached that row, the bot still ran the adjustment.

### 2.2  Implementation (business-rule ordering)

The last four steps of `_worker_step` were rewritten to enforce this
exact contractual sequence:

1. **SQLite duplicate validation** — `self.db.has_tx(item.tx_id)`.
2. **Latest Manual Bonus validation** — `_refresh_manual_list_now()` does
   a fresh `sheet.read_manual_set()` (TTL-throttled at
   `_MANUAL_FRESH_TTL_SEC = 2.0 s` so back-to-back submits amortise).
   If the user is present, insert one `MANUAL BONUS` row and skip.
3. **Daily bonus validation** — see BUG-015 below.
4. **Submit adjustment** — `self.panel.submit_deposit(...)`.

Any deviation from this ordering breaks the guarantee, so a static
assertion is embedded in `tests/test_bug012_manual_bonus_race.py::test_bug012_worker_step_sequence_in_dashboard`
that greps the shipped source for the four markers in that order.

### 2.3  Files modified

* `ui/dashboard.py` — new helper `_refresh_manual_list_now`, new state
  variable `_manual_last_refresh_ts`, rewritten pre-submit block in
  `_worker_step`.

### 2.4  Regression test — Verified

`tests/test_bug012_manual_bonus_race.py` — 4 tests, all pass:

* `test_bug012_manual_added_after_refill_wins` — operator adds User ID
  after refill → worker calls fresh read → skips with `MANUAL BONUS`.
  Panel `submit_deposit` is **never** invoked.
* `test_bug012_manual_added_between_two_items` — two-item queue with
  a manual-list mutation between the two rows: item 1 SUCCESS, item 2
  MANUAL BONUS.
* `test_bug012_dedup_takes_priority_over_manual` — duplicate `tx_id`
  short-circuits before the Google API call fires (protects API quota).
* `test_bug012_worker_step_sequence_in_dashboard` — static source-order
  guarantee (has_tx → manual-fresh → daily-bonus-by-tx-date → submit).

---

## 3. BUG-013 — Portable Build

### 3.1  Audit result

The v1.0.0 RC already shipped `main.py`'s frozen-aware bootstrap,
`BonusReloadBot.spec` (with `collect_data_files("playwright")` and the
local `pw-browsers/` bundle), and `build_portable.bat`. The remaining
gap: `_ensure_runtime_layout()` did not create
`browser_profile_bonus_reload/`; the folder was only created lazily
inside `main()` before the Dashboard was built. On a fresh clean copy
this worked because `main()` runs before Playwright ever launches, but
the runtime-layout guarantee wasn't complete.

### 3.2  Implementation

* `main.py::_ensure_runtime_layout` now includes
  `browser_profile_bonus_reload` in the list of runtime folders it
  seeds. Fresh copies now have every writable folder before any config
  is read or any Playwright API is touched.
* Startup diagnostics (see BUG-014) now report a bundled-Chromium check
  when running in frozen mode: it verifies that
  `<_MEIPASS>/pw-browsers/` exists. If it doesn't the operator sees a
  `WARN [Bundled Chromium]` line instead of a mysterious Playwright
  timeout later.

No changes to the spec were required — the existing spec was correct:
`datas.append((str(pw_browsers), "pw-browsers"))` places the whole
Chromium tree next to `_MEIPASS`, and `main.py` primes
`PLAYWRIGHT_BROWSERS_PATH` before any Playwright import. The only
build-side risk is if the operator skips the pre-build Chromium install
— `BonusReloadBot.spec` raises `SystemExit` in that case with a helpful
message.

### 3.3  Files modified

* `main.py` (folder seeding, diagnostics).
* No changes to `BonusReloadBot.spec` or `build_portable.bat` — they
  were already correct. They are still verified by tests
  (`tests/test_bug013_portable_build.py`).

### 3.4  Regression test — Verified

`tests/test_bug013_portable_build.py` — 4 tests / 1 skip:

* `test_frozen_layout_seeds_folders_without_pyside` — simulates a
  frozen bundle (`sys.frozen=True`, fake `_MEIPASS`, fake `sys.executable`)
  and asserts every runtime folder is created next to the fake .exe,
  that `config/` is seeded from the bundle, that the credentials
  example is copied, and that both Playwright env vars are set.
* `test_frozen_layout_seeds_folders` — same assertion via full
  `import main` (skipped when PySide6 isn't available on the host).
* `test_spec_bundles_required_assets` — asserts
  `collect_data_files("playwright")`, `pw-browsers`, `config`,
  `service_account.json.example`, `console=False`, and `COLLECT(` are
  all present in `BonusReloadBot.spec`.
* `test_build_bat_installs_chromium_into_local_pw_browsers` — asserts
  `PLAYWRIGHT_BROWSERS_PATH=%CD%\pw-browsers`,
  `python -m playwright install chromium`, `pyinstaller`, and the five
  seed folders (`config`, `credentials`, `logs`, `screenshots`,
  `browser_profile_bonus_reload`) are all wired.

### 3.5  Windows verification checklist (must be executed by operator)

Because this workspace is Linux-only, the following steps MUST be run
on the target Windows machine — they cannot be exercised here:

1. On the build box: `build_portable.bat` runs to completion. Confirm
   `dist\Bonus Reload Bot\Bonus Reload Bot.exe` exists.
2. On a **fresh Windows PC with no Python / Playwright / Chromium /
   env vars**: copy the folder over, drop `service_account.json` into
   `credentials\`, double-click the .exe.
3. Confirm the Dashboard opens (Portable mode: frozen line appears in
   `logs\YYYY-MM-DD.log`).
4. Confirm `OPEN PANEL` launches the bundled Chromium (the child
   process's exe path should sit inside `_internal\pw-browsers\...`).
5. Confirm `browser_profile_bonus_reload\`, `logs\`, `screenshots\`,
   and `credentials\` all appear next to the .exe (not inside
   `_internal\`).

---

## 4. BUG-014 — Logger Startup

### 4.1  Root Cause

`core/logger.py` created the `TimedRotatingFileHandler` unconditionally.
If the file could not be opened (Windows permission issue, read-only
folder, share collision) the exception propagated all the way out to
`main()` and terminated the app before any UI was drawn.

### 4.2  Implementation

`core/logger.py` was rewritten to:

* Always attempt to `mkdir` the log directory (best-effort — never
  crash).
* Wrap the `TimedRotatingFileHandler` call in `try/except Exception`.
  Success sets `file_handler_ok=True`; failure sets it to `False` and
  records `file_handler_error` = `"{ExceptionType}: {message}"`.
* Always attach a `ConsoleHandler(stream=sys.stderr)` regardless.
* Expose `AppLogger.reset()` for test isolation.
* Deliberately do NOT gate on `if not self.logger.handlers` — the
  logger's shared singleton may already have external handlers attached
  (pytest, PySide6 log routing) and gating on that would silently skip
  the fallback logic.

A companion `core/diagnostics.py` module was added. `run_diagnostics()`
inspects Config, Credentials, SQLite, Logs, Screenshots, Browser
Profile, and (in frozen mode) Bundled Chromium. Each check reports
`PASS` / `WARN` with a detail message. `main.py` runs it right after
the logger is initialised and dumps the summary through `AppLogger`
so it lands in both the file and the live log — never crashing on any
warning.

### 4.3  Files modified

* `core/logger.py` — rewritten (safe fallback + `.reset()`).
* `core/diagnostics.py` — new.
* `main.py` — calls `run_diagnostics()` after `AppLogger.get()`.

### 4.4  Regression test — Verified

`tests/test_bug014_logger_fallback.py` — 3 tests, all pass:

* `test_logger_writes_file_when_writable` — happy path.
* `test_logger_falls_back_when_file_handler_raises` — monkey-patches
  `TimedRotatingFileHandler` to raise `PermissionError`; asserts the
  logger comes up, `file_handler_ok=False`,
  `file_handler_error` contains `PermissionError`, and a
  `StreamHandler` is attached.
* `test_logger_survives_unwritable_dir` — points `log_dir` at a path
  whose parent is a file (impossible to `mkdir`); asserts `info/warn/
  error` never raise.

---

## 5. BUG-015 — Daily Bonus Business Rule

### 5.1  Root Cause

`core/database.py::daily_bonus_map()` was the ONLY primitive used by
the rule engine for daily-bonus totals. Its `WHERE` clause was:

```sql
WHERE date(processed_at) = date('now', 'localtime')
  AND result = 'SUCCESS'
```

`processed_at` is set by `DatabaseService.insert()` to
`datetime.now().isoformat(timespec="seconds")` — i.e. **adjustment
execution time**, not the sheet's original transaction timestamp. Any
row whose Google Sheet TIME STAMP fell on the previous day but whose
adjustment ran after midnight was silently placed into "today's bucket"
and the user's yesterday-limit was allowed to reset. This is exactly the
scenario reported for `maknyus27`:

```
Sheet Tx 1: 19 Jul 14:44  Deposit 100 158  ⇒ Bonus 10 000 (SUCCESS)
Sheet Tx 2: 19 Jul 23:56  Deposit 110 513
Bot adjusts Tx 2 at 20 Jul 03:27          ⇒ current code: Bonus 10 000 (WRONG)
                                          ⇒ correct rule:  LIMIT   / Bonus 0
```

### 5.2  Confirmation (audit)

We grep-audited every business-rule call site.

* `core/queue_manager.py::refill` (pre-fix line 78) called
  `self.db.daily_bonus_map()` to warm the `MemoryCache`, then keyed the
  per-user cumulative simulation only by `username` (not by
  transaction date).
* `ui/dashboard.py::_worker_step` (pre-fix line 1259) called
  `self.cache.get_daily_bonus(item.username)` — same cache, same
  `processed_at` origin.

Both were confirmed to be using `processed_at` (via `daily_bonus_map`);
neither ever looked at `MasterRow.timestamp`.

### 5.3  Implementation

* `core/database.py` — new column `timestamp_date` (ISO `YYYY-MM-DD`
  parsed from the sheet's `TIME STAMP` cell). Fresh installs get it via
  `CREATE TABLE IF NOT EXISTS`. Legacy databases get an idempotent
  `ALTER TABLE ... ADD COLUMN` + a one-time backfill loop that runs
  `parse_transaction_date()` against every existing `timestamp` value.
  New index `idx_username_txdate(username, timestamp_date)`.
* `core/database.py::insert()` and `.bulk_insert()` now compute
  `timestamp_date` and persist it alongside the raw timestamp string.
* `core/database.py::daily_bonus_for_transaction_date(username, iso_date) → int`
  is the new business-rule primitive.
  `daily_bonus_map()` is kept — but explicitly re-documented as the
  Dashboard-KPI helper only. **It is no longer called by any rule
  engine path.**
* `core/timestamp_utils.py` — new. Robust `parse_transaction_date()`
  covering all Google Sheets formats we observed (ISO, dash, slash,
  short-month, long-month, day-first, US-style). Returns `None` on
  garbage — callers fall back to "today" and log a WARN, never grant an
  unlimited bonus.
* `core/queue_manager.py::refill` — cumulative simulation is now keyed
  by `(username, transaction_date)`; each key seeds from
  `daily_bonus_for_transaction_date`. Bonuses granted for
  transactions dated Jul 19 accumulate against Jul 19's cap even when
  the refill happens on Jul 20.
* `ui/dashboard.py::_worker_step` — process-time revalidation calls
  `self.db.daily_bonus_for_transaction_date(item.username,
  tx_date_iso)`. `tx_date_iso` is derived from `item.timestamp` via
  `parse_transaction_date`. Unparseable timestamps warn and fall back
  to today (safest default).

`processed_at` is preserved unchanged and continues to power the
audit-history KPI ("Processed Today", "Total Processed"). No business
rule reads it any more.

### 5.4  Files modified

* `core/database.py`
* `core/queue_manager.py`
* `core/timestamp_utils.py` (new)
* `ui/dashboard.py`

### 5.5  Regression test — Verified

The full `maknyus27` scenario is encoded in
`tests/test_existing_behaviour.py::test_queue_manager_uses_transaction_date_for_daily_bonus`
and passes:

```
Prior state:  TX-001  maknyus27  100 158  bonus 10 000  SUCCESS  timestamp='2025-07-19 14:44'
Incoming:     TX-002  maknyus27  110 513                        timestamp='2025-07-19 23:56'
Refill outcome:  status='LIMIT', bonus=0    ✓
```

Additional coverage in `tests/test_bug015_daily_bonus_transaction_date.py`:

* 12 parametrised `parse_transaction_date` cases.
* Garbage rejection (`""`, `None`, `"not-a-date"`, `"99/99/2025"`).
* `daily_bonus_for_transaction_date` ignores non-SUCCESS rows.
* Backfill migration on a legacy DB (no `timestamp_date` column)
  populates the column without touching `processed_at`.

---

## 7. BUG-016 — Frontend dependency resolution (`npm install` failure)

### 7.1  Bug report (as filed)

```
Fresh clone → cd frontend → npm install fails:

react-day-picker@8.10.1 requires:
    date-fns ^2.28.0 || ^3.0.0
but package.json currently pins:
    date-fns 4.1.0
```

### 7.2  Root Cause

`react-day-picker@8.x` pre-dates date-fns v4 and pre-dates React 19.
Its peer-dependency block reads:

```
peerDependencies = {
  react: '^16.8.0 || ^17.0.0 || ^18.0.0',
  'date-fns': '^2.28.0 || ^3.0.0'
}
```

`frontend/package.json` was already at `react@19.0.0` and
`date-fns@4.1.0` — **both** peers fail against `react-day-picker@8.10.1`.
`yarn` had been silently tolerating the mismatch during initial repo
generation. Modern `npm@10` (the tool the operator is using) refuses
without `--force`/`--legacy-peer-deps`, which the problem statement
forbids.

### 7.3  Why `date-fns` moved to v4

Nothing in `frontend/src/` imports `date-fns` directly (`grep -rn
"date-fns" src/` produced zero hits). It was only pulled in by
`react-day-picker`. The `date-fns@4.1.0` pin therefore reflected an
"upgrade-everything-to-latest" pass done at repo generation time, not a
deliberate feature requirement. React 19 was almost certainly bumped
in the same pass. Neither is required by the app — `frontend/` is a
CRACO + shadcn scaffold whose only running route is
`src/App.js`'s placeholder page.

### 7.4  Fix (smallest compatible change)

The user's rule matrix:

* Preserve React 19 (huge blast radius otherwise).
* Preserve `date-fns@4.1.0` if possible.
* No `--force`, no `--legacy-peer-deps`.

Both peer conflicts collapse into one when `react-day-picker` is
bumped from **`8.10.1`** to **`9.14.0`** (latest stable 9.x):

* `react-day-picker@9`'s peer is `react >= 16.8.0` — accepts React 19.
* `react-day-picker@9` no longer declares `date-fns` as a peer at all;
  it now depends on `date-fns@^4.1.0` **directly**. `date-fns@4.1.0`
  therefore satisfies the transitive requirement without any root-level
  change.

That is the entire fix. Every other package version in `package.json`
is unchanged.

To defend against future non-deterministic resolutions, the resulting
`package-lock.json` (20 441 lines, 769 KB, generated by `npm@10.8.2`
against `node@20.20.2` on a completely empty `node_modules/`) is now
committed to the repo.

### 7.5  Companion component update

`src/components/ui/calendar.jsx` (a stock shadcn `Calendar` wrapper
that no other file imports) was written against the v8 API:

* `caption_label`, `nav_button_previous`, `day_selected`, etc.
* `components={{ IconLeft, IconRight }}`.

`react-day-picker@9` renamed those classNames slots (`caption_label`
lives inside `month_caption`, `nav_button_previous` → `button_previous`,
`day_selected` → `selected`, etc.) and merged both chevron slots into
a single `Chevron` slot that receives an `orientation` prop.

The file was updated to the v9 API. It still renders identically to
the v8 version (same Tailwind classes, same lucide-react chevrons).
Because nothing imports `Calendar`, the compiled bundle is unchanged
for the running app — the update is purely defensive so any future
import will just work.

### 7.6  Files modified for BUG-016

* `frontend/package.json` — one line: `"react-day-picker": "8.10.1"` →
  `"react-day-picker": "9.14.0"`.
* `frontend/src/components/ui/calendar.jsx` — v8 → v9 API mapping.
* `frontend/package-lock.json` — regenerated (**new file**, committed).
* `.gitignore` — re-added the `node_modules/`, `npm-debug.log*`, etc.
  patterns that had been trimmed in the initial cleanup pass now that
  `frontend/` is retained.

### 7.7  Verification (reproduced clean-clone flow)

Executed on this workspace (`node@20.20.2`, `npm@10.8.2`):

```
cp -a /app/repo /tmp/clean_clone
cd /tmp/clean_clone/frontend
rm -rf node_modules yarn.lock package-lock.json build
npm install                    # succeeds
CI=true npm run build          # succeeds ("Compiled successfully.")
```

Output tail:

```
added 1493 packages, and audited 1494 packages in 1m
75 vulnerabilities (6 low, 5 moderate, 64 high)
```

```
File sizes after gzip:
  98.47 kB  build/static/js/main.4772e0a8.js
  8.8 kB    build/static/css/main.bc058c4f.css
The build folder is ready to be deployed.
```

No `ERESOLVE`. No `--force`. No `--legacy-peer-deps`. Peer-dependency
tree checked with `npm ls | grep -i "peer\|invalid"` — clean.

The 75 audit findings are all inherited from `react-scripts@5.0.1` and
its build-time toolchain (webpack@4-era transitive dev deps: `nth-check`,
`postcss@7`, `svgo@1`, `browserslist`, etc.). None affect the shipped
bundle. Addressing them requires either migrating off CRA/`react-scripts`
or accepting `npm audit fix --force` — both explicitly out of scope of
"the smallest compatible change" and both would ripple through every UI
package. They are documented in §8. Known limitations.

---

## 8. Latent bug found during audit (fixed, non-business-rule)

While reading `ui/dashboard.py::PreviewDialog` we noticed a stale
attribute reference from the pre-`slots` `QueueItem` refactor:

```python
# BROKEN — would raise AttributeError on every PREVIEW QUEUE click
table.setItem(row, 0, QTableWidgetItem(it.user_id))
table.setItem(row, 1, QTableWidgetItem(f"{it.deposit:,}"))
```

`QueueItem` now defines `username` / `amount`, not `user_id` / `deposit`.
This would crash the app the first time an operator opened Preview
Queue. Because it's a pure UI display bug (no business rule involved)
we fixed it inline. No test added — the fix is a one-line rename.

---

## 9. Regression summary

### 9.1  Python (pytest)

```
$ python -m pytest tests/
tests/test_bug012_manual_bonus_race.py ....                              [ 12%]
tests/test_bug013_portable_build.py .s..                                 [ 24%]
tests/test_bug014_logger_fallback.py ...                                 [ 33%]
tests/test_bug015_daily_bonus_transaction_date.py ................       [ 81%]
tests/test_existing_behaviour.py ......                                  [100%]

======================== 32 passed, 1 skipped in 0.34s =========================
```

The single skip is `test_frozen_layout_seeds_folders`, which asks pytest
to `importorskip("PySide6")`. The Linux CI host doesn't ship PySide6
6.10.3 wheels for Python 3.11 (project targets Python 3.13). The
underlying `main.py` bootstrap logic it covers is exercised by
`test_frozen_layout_seeds_folders_without_pyside`, which runs an
AST-free re-import of only the frozen-aware bootstrap portion and
covers the exact same assertions. On the Windows build machine both
variants will run.

### 9.2  Frontend (npm install + npm run build on a clean copy)

```
$ cp -a /app/repo /tmp/clean_clone
$ cd /tmp/clean_clone/frontend
$ rm -rf node_modules yarn.lock package-lock.json build
$ npm install
added 1493 packages, and audited 1494 packages in 1m
$ CI=true npm run build
Compiled successfully.
File sizes after gzip:
  98.47 kB  build/static/js/main.4772e0a8.js
  8.8 kB    build/static/css/main.bc058c4f.css
$ npm ls 2>&1 | grep -i "peer\|invalid"     # (empty — no peer conflicts)
```

Regression checklist mandated by the problem statement:

* [x]  Repository cleanup completed safely
* [x]  No removed file was still referenced (`grep` + tests confirm)
* [x]  Manual Bonus Race fixed (BUG-012 tests)
* [ ]  Portable build works without Python — **must be executed on Windows** (BUG-013 §3.5)
* [ ]  Portable build works without Playwright — **must be executed on Windows**
* [ ]  Portable build works without Chromium installation — **must be executed on Windows**
* [ ]  Bundled Chromium launches correctly — **must be executed on Windows**
* [x]  Runtime folders created automatically (frozen-simulation test)
* [x]  Config loads automatically (seeded from bundle if absent)
* [x]  Logger fallback works (BUG-014 tests)
* [x]  Startup diagnostics work (integrated in `main.py`; `core/diagnostics.py`)
* [x]  Daily bonus uses Transaction Timestamp (BUG-015 tests)
* [x]  Cross-midnight scenario passes (`test_queue_manager_uses_transaction_date_for_daily_bonus`)
* [x]  Existing regression suite still passes (`test_existing_behaviour.py`, 6 tests)
* [x]  `frontend/` `npm install` succeeds without `--force` or `--legacy-peer-deps` (BUG-016)
* [x]  `frontend/` `npm run build` succeeds (BUG-016)

---

## 10. Known limitations

* The Windows-only steps of the portable-build regression cannot run on
  Linux. See §3.5 for the checklist the operator MUST execute.
* `parse_transaction_date` accepts the format matrix documented in
  `core/timestamp_utils.py`. If the operator's Google Sheet uses a
  format outside that matrix, `_worker_step` falls back to today's
  date and emits a WARN log line — the operator sees it in the live
  log and can widen the parser. Adding a new format is a one-line
  regex extension.
* Emergent LLM key: not applicable — this app has no LLM integrations.
* `test_result.md` (Emergent testing scaffold) was removed. The
  `test_reports/pytest/.gitkeep` marker remains and pytest still writes
  to that folder when invoked with `--junitxml`.
* `npm audit` reports 75 findings (all inherited from `react-scripts@5.0.1`
  build-time toolchain — `nth-check`, `postcss@7`, `svgo@1`, etc.).
  None affect the shipped browser bundle. Fixing them requires either
  migrating off CRA/`react-scripts` or running `npm audit fix --force`;
  both are out of scope of "smallest compatible change" for BUG-016 and
  would touch every UI package. Left as a v1.2 backlog item.

---

## 11. Files touched

### Removed

* `backend/`    (recursive)
* `test_result.md`
* `.gitconfig`

### Added

* `core/timestamp_utils.py`
* `core/diagnostics.py`
* `tests/test_bug012_manual_bonus_race.py`
* `tests/test_bug013_portable_build.py`
* `tests/test_bug014_logger_fallback.py`
* `tests/test_bug015_daily_bonus_transaction_date.py`
* `tests/test_existing_behaviour.py`
* `frontend/package-lock.json`      (BUG-016)
* `HARDENING_REPORT_v1.1.md`

### Modified

* `.gitignore`                  — trimmed unused patterns, kept Node ignores.
* `main.py`                     — folder seeding, diagnostics wiring.
* `core/database.py`            — new column, migration, `daily_bonus_for_transaction_date`.
* `core/logger.py`              — safe-fallback rewrite + `.reset()`.
* `core/queue_manager.py`       — transaction-date-keyed cumulative simulation.
* `ui/dashboard.py`             — validation-order rewrite (BUG-012), transaction-date daily bonus (BUG-015), PreviewDialog attribute fix.
* `frontend/package.json`       — `react-day-picker` `8.10.1` → `9.14.0` (BUG-016).
* `frontend/src/components/ui/calendar.jsx` — v8 → v9 API (BUG-016).
* `memory/PRD.md`               — Production Hardening v1.1 log appended.

### Untouched

* `BonusReloadBot.spec`
* `build_portable.bat`
* `requirements.txt`
* `config/config.json`, `config/selectors.json`
* `credentials/service_account.json.example`
* `core/panel_service.py`, `core/sheet_service.py`, `core/validator.py`, `core/memory_cache.py`
* `README.md`
* Every other file inside `frontend/` (all non-`calendar.jsx` sources, `craco.config.js`, all shadcn UI components, etc.).

---

## 12. Production readiness

* `pytest tests/`  → 32 passed, 1 skipped (documented). No failures.
* `python -m py_compile main.py core/*.py ui/*.py`  → clean.
* No new `TODO`, `FIXME`, or `pass` placeholders introduced.
* Business rules preserved: Manual Bonus takes precedence, daily-bonus
  cap enforced against transaction date, `processed_at` never used in
  business logic.

The repository is **production-ready pending the Windows-side portable
build verification described in §3.5**. When the operator confirms
those five checks, this branch can be tagged `v1.1`.
