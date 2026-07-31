# Bonus Reload Automation — v1.0.0 RC

A production Windows desktop bot that reads pending deposit transactions
from a Google Spreadsheet, validates them against the configured bonus
rules, performs the *Manual Deposit Adjustment* on a web panel via a
persistent Playwright/Chromium browser, and records the outcome in a
local SQLite database. Runs on **one** Windows PC.

- Python **3.13+**
- PySide6 · Playwright (Chromium) · gspread · sqlite3
- No Selenium · No Redis · No OCR · No AI · No threads / no
  multi-processing · No background polling
- Modern dark UI (amber / gold accent). Continuous monitoring mode:
  operator presses **START once**, bot runs until **STOP**.

## Data-flow at a glance

```
Google Sheets (READ ONLY)
        │
        ▼
   read master
        │
        ▼
  validate + dedup ────► SQLite   (WRITE ONLY)
        │                   ▲
        ▼                   │
   READY queue              │
        │                   │
        ▼                   │
     worker ────► panel ─── ┘ (insert one row per outcome)
```

- Google Sheets is **only** the transaction feed. The bot **never**
  writes STATUS, BONUS or any other cell back to the sheet.
- Every processed transaction (SUCCESS / FAILED / LIMIT / INVALID /
  MANUAL BONUS) becomes one row in `processed.db` keyed by `TX_ID`.
- Duplicate protection is guaranteed both by pre-filtering at refill
  time and by the SQLite `PRIMARY KEY` constraint.

---

## Project layout

```
project/
│
├── main.py                       # PySide6 entry point
├── processed.db                  # SQLite - single source of truth (auto-created)
│
├── credentials/
│    └── service_account.json     # Google service account (add this yourself)
│    └── service_account.json.example
│
├── config/
│    ├── config.json              # All runtime knobs (Panel URL, limits, …)
│    └── selectors.json           # Every HTML selector (never hardcoded)
│
├── core/
│    ├── database.py              # SQLite persistence layer (WAL)
│    ├── sheet_service.py         # gspread client - READ ONLY
│    ├── validator.py             # Bonus rule engine
│    ├── queue_manager.py         # Adaptive queue + per-user cumulative sim
│    ├── memory_cache.py          # daily_bonus dict + manual set
│    ├── panel_service.py         # Playwright wrapper + lifecycle recovery
│    └── logger.py                # Daily log file + Qt live-log signal (500-line FIFO)
│
├── ui/
│    └── dashboard.py             # Dark UI, KPIs, Monitoring mode, DB dialog
│
├── logs/                         # YYYY-MM-DD.log
├── screenshots/                  # FAILED-transaction snapshots
├── requirements.txt
└── README.md
```

---

## Requirements

The project is pinned and production-verified on Windows:

```
PySide6==6.10.3
playwright==1.55.0
gspread==6.1.2
google-auth==2.34.0
google-auth-oauthlib==1.2.1
```

Python **3.13+**. These versions have been validated on the operator's
Windows machine; keep them unless a compatibility issue forces an update.

---

## Installation (Windows)

```bat
:: 1. Create a virtual environment
py -3.13 -m venv .venv
.venv\Scripts\activate

:: 2. Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

:: 3. Install the bundled Chromium build for Playwright
python -m playwright install chromium
```

That's it. No Chocolatey, no system-wide package installs.

---

## Google Service Account

1. In Google Cloud Console → *IAM & Admin → Service accounts*, create a
   service account (or reuse an existing one), enable the **Google Sheets
   API** and **Google Drive API** on the project, and download its JSON
   key.
2. Save the key file as:

   ```
   project\credentials\service_account.json
   ```

   The path is already wired via `config/config.json →
   "google_credentials": "credentials/service_account.json"`. Change it
   from **Settings** in the app if you keep the key elsewhere.
3. Open the target Google Spreadsheet, click **Share**, and add the
   service account's `client_email` (from the JSON, looks like
   `bot@my-project.iam.gserviceaccount.com`) with **Editor** rights.
   Do this **once per new sheet** — the credential itself never changes,
   only the daily spreadsheet URL.

---

## Run

```bat
.venv\Scripts\activate
python main.py
```

### First-time setup

1. Click **SETTINGS** and fill:
   - **Panel URL** — the deposit panel entry point (must start with
     `http://` or `https://`). Saved to `config.json` and restored on
     next launch.
   - Optional: Daily Limit, Batch Size, Reload Interval, Polling Delay,
     **Monitoring Interval** (default 10 s), Remark, Credentials path.
2. Paste the Google Spreadsheet URL into the top field and click
   **CONNECT SHEET**. The URL is remembered.
3. Click **OPEN PANEL** — a Chromium window opens; log in manually. The
   browser profile lives in `browser_profile/` (persistent), so cookies
   survive both app restarts and Windows reboots — you only re-log in
   when the website itself expires the session.
4. Click **READY** to hand browser control to the bot.
5. *(Optional)* Click **REFRESH QUEUE** and **PREVIEW QUEUE** to inspect
   what the bot will do before running it.
6. Click **START — once**. The bot now runs continuously:
   - Processes every READY row.
   - When the queue empties, it enters **Monitoring** mode
     (Bot status → *Monitoring*, ETA area shows `Next Refresh 00:10`).
   - Every `monitoring_interval_sec` (default 10 s) it polls the sheet
     for new pending rows; if any appear, it processes them
     automatically.
   - Never asks the operator to press START again.
7. Click **STOP** to exit. The bot finishes the current transaction,
   updates the sheet, and returns to Idle. The browser stays open.

### Emergency Stop / Browser Recovery

- **STOP** while processing: waits for the current submit to finish,
  writes its outcome, halts the worker, keeps the browser open.
- **STOP** while monitoring: exits immediately.
- If the operator closes the Chromium window (X), Panel Status
  auto-flips to **Closed** within 2 s and START is disabled. Click
  **OPEN PANEL** again — a fresh persistent context is created; no app
  restart required.

---

## Bonus rules

Configured in `config/config.json → bonus_rules`:

```json
{
  "daily_limit": 10000,
  "tiers": [
    { "min_deposit": 100000, "bonus": 10000 },
    { "min_deposit":  50000, "bonus":  5000 }
  ]
}
```

Actual bonus granted:

```
bonus_to_give = MIN( tier_bonus, daily_limit - current_daily_bonus )
```

Preview and Worker use the **same** cumulative simulation — a user with
two pending rows `50k` then `122k` will preview `5000 · 5000` (not
`5000 · 10000`), and the worker grants exactly those values.

If the user is in the **MANUAL BONUS RELOAD** sheet (Column B), the
row is marked `MANUAL BONUS` and skipped.

---

## Sheet contract (MASTER) — READ ONLY

| Col | Field        | Bot behaviour                                              |
| --- | ------------ | ---------------------------------------------------------- |
| A   | NO           | Ignored                                                    |
| B   | USER ID      | **Required** header. Filled into panel `#username`         |
| C   | AMOUNT       | Ignored                                                    |
| D   | SHEET DATA   | **Required** header. Never written.                        |
| E   | TIME STAMP   | **Required** header. Stored in SQLite for audit.           |
| F   | TRUE AMOUNT  | **Required** header. Deposit used for validation.          |
| G   | STATUS       | Never written.                                             |
| H   | BONUS        | Never written.                                             |
| I   | TX_ID        | **Required** header. Primary key for SQLite dedup.         |

When **CONNECT SHEET** is pressed the bot verifies that columns
**B, D, E, F, I** all have non-empty headers in row 1. If any is
missing, a clear error is shown and START stays disabled.

The remark `BONUS RELOAD AUTO` is sent **only** to the panel textarea —
never written anywhere else.

MANUAL BONUS RELOAD sheet: USER IDs live in **Column B only** (Column A
is a row number and is ignored). Values are trimmed. Refreshed every
`manual_reload_interval_sec` (default **90 s**).

## SQLite database — WRITE ONLY

Path: `sqlite_path` in `config.json` (default `processed.db`). Auto-created
on first run with:

```sql
CREATE TABLE processed_transactions (
    tx_id        TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    amount       INTEGER,
    bonus        INTEGER,
    result       TEXT NOT NULL,           -- SUCCESS | FAILED | LIMIT | INVALID | MANUAL BONUS
    processed_at TEXT NOT NULL,
    sheet_name   TEXT,
    timestamp    TEXT
);
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
```

- Duplicate protection: pre-filter at refill (`SELECT tx_id IN (...)`)
  **and** the PRIMARY KEY constraint on `INSERT OR IGNORE`.
- Today's daily-bonus totals come from one query:
  `SELECT username, SUM(bonus) FROM ... WHERE date(processed_at)=date('now') AND result='SUCCESS' GROUP BY username`.
- Only ever queried by primary key or aggregated with the two indexes
  that the schema declares. No full-table scans on the hot path.

### Database dialog (top-bar **DATABASE** button)

- Processed Today
- Total Processed
- Database Size
- Status (Connected / Error)

Three maintenance actions:

- **Export Database** — writes a CSV dump of the whole table.
- **Backup Database** — copies the `.db` file to
  `processed_YYYY-MM-DD_HH-MM-SS.db` after a WAL checkpoint.
- **Maintenance** — asks for confirmation, then runs `VACUUM; ANALYZE;`.
  Disabled while the worker is running. Never runs automatically.

---

## Adaptive polling + Continuous monitoring

- **Batch of 100** rows read per Google Sheets call.
- Sheet is *not* touched again until the queue drains OR the operator
  clicks REFRESH QUEUE.
- After the queue drains the bot enters **Monitoring** and polls every
  `monitoring_interval_sec` (default 10 s). Between polls the worker
  simply sleeps — Google API traffic stays minimal.
- Manual-list refresh (every 30 s) is *postponed* while a queue is
  actively being processed to save quota; deferred refreshes fire as
  soon as the queue drains.

## Queue synchronisation

Refill is race-condition safe:

1. Read pending rows.
2. Validate all → categorise READY / INVALID / MANUAL BONUS / LIMIT.
3. `sheet.batch_update_statuses(...)` writes every non-READY status in
   a single `batch_update` call.
4. Only **after Google confirms** the write (gspread's `batch_update`
   is synchronous — it returns after the API accepts the change) does
   `QueueManager` commit the READY items to the internal worker queue.
5. If the write fails, the READY queue is *not* populated — the next
   refill retries.

The worker therefore never processes a row before the sheet reflects
its correct status.

---

## Logging & exports

- Daily log file: `logs/YYYY-MM-DD.log`
- Live log on the right side of the dashboard, capped at **500 lines
  FIFO**. Memory usage stays constant during long runs.
- Format kept minimal:

  ```
  Queue Loaded
    READY        : 87
    MANUAL BONUS : 5 (Skipped)
    INVALID      : 3 (Skipped)
    LIMIT        : 5 (Skipped)
  steven       Deposit 100,000  Bonus 10,000  SUCCESS
  capitsandal  Deposit  50,000  Bonus  5,000  SUCCESS
  ```

- **EXPORT .TXT / .CSV** buttons dump the current session buffer.

---

## Dashboard KPIs

Always visible in the top bar: **Last sync: YYYY-MM-DD HH:MM:SS** +
version badge (`v1.0.0`).

Left pane (scrollable):

- **Status** — Google Sheets / Panel / Bot indicators + last sync.
- **Actions** — CONNECT SHEET · OPEN PANEL · READY · REFRESH QUEUE ·
  PREVIEW QUEUE · START · STOP.
- **Current Processing** — big amber user id, deposit, granted bonus,
  live status pill (Idle / PROCESSING / SUCCESS / FAILED / MONITORING).
- **Progress** — `Processed X / Y` label, amber progress bar,
  `ETA hh:mm:ss` (or `Next Refresh mm:ss` while monitoring).
- **Queue Summary** — READY (green) · MANUAL BONUS (blue) ·
  INVALID (red) · LIMIT (amber).
- **Statistics** (2 × 4 grid):
  Queue Ready · Processed · Skipped · Failed · Today's Bonus ·
  Adj / Min · Avg Submit · Elapsed.

Right pane: Live Log.

---

## Build a stand-alone portable folder (Windows)

**Goal:** produce a folder that runs on any Windows PC with **no Python,
no Playwright, no Chromium** pre-installed. Operators only need to copy
the folder and drop their `service_account.json` in.

### One-command build

On a Windows machine that has Python 3.13:

```bat
build_portable.bat
```

That script:

1. Creates a `.venv/` and installs `requirements.txt` + `pyinstaller`.
2. Runs `python -m playwright install chromium` with
   `PLAYWRIGHT_BROWSERS_PATH=%CD%\pw-browsers` so the Chromium build
   lands **inside the project**, not in `%LOCALAPPDATA%`.
3. Runs `pyinstaller BonusReloadBot.spec` which bundles:
   * Playwright driver + Node.js runtime (`collect_data_files("playwright")`)
   * Local `pw-browsers/` (the Chromium we just installed)
   * `config/` templates + `service_account.json.example`
   * `PySide6` DLLs.
4. Seeds the writable folders next to the `.exe`:
   ```
   dist\Bonus Reload Bot\
       Bonus Reload Bot.exe
       _internal\...                     # bundled Chromium + driver + PySide6
       config\config.json                # editable
       config\selectors.json             # editable
       credentials\service_account.json.example
       logs\
       screenshots\
       browser_profile_bonus_reload\
   ```

### Deploy to a fresh Windows PC

1. Copy the whole `dist\Bonus Reload Bot\` folder to the target machine.
2. Drop the real `service_account.json` into `credentials\`.
3. Double-click `Bonus Reload Bot.exe`.

That's it — nothing else to install.

### How the portable runtime finds Chromium

`main.py` sets `PLAYWRIGHT_BROWSERS_PATH` to
`<_internal>\pw-browsers` **before** importing Playwright, and sets
`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` so the bot can never fall back to
downloading Chromium at runtime. On a completely clean Windows PC the
bot uses the bundled Chromium exclusively.

### Isolated, unique browser profile

`launch_persistent_context()` is always called with
`user_data_dir = <APP_DIR>\browser_profile_bonus_reload\`. The folder
name is unique to this bot, which lets multiple Playwright automations
coexist on the same PC without profile / session collisions
(`browser_profile_deposit`, `browser_profile_withdraw`, etc. for future
bots).

Cookies, session, login, cache and LocalStorage all live in that folder
next to the `.exe`, so they persist across restarts and Windows
reboots.

---

## Performance target

**30 – 60** successful adjustments per minute on a modern Windows PC:

- Single persistent Chromium context — no relaunch per transaction.
- No page refresh — panel keeps its state between submits.
- Bulk Google Sheets reads (`get_all_values()` per batch of 100).
- Batched writes (one `batch_update` per row for PROCESSED / FAILED,
  one bulk `batch_update` for all skipped rows in a refill).
- No background polling: the queue drives itself; monitoring wakes up
  only on the configured interval.
- No worker threads/processes — a single `QTimer` pumps one transaction
  per tick.

---

## Recovery

If the application crashes / is closed, restart it and click
**CONNECT SHEET**. Pending rows are re-read; anything already marked
`PROCESSED · INVALID · LIMIT · MANUAL BONUS · FAILED` is ignored, so
no transaction is ever duplicated.

---

## Version

`v1.0.0`
