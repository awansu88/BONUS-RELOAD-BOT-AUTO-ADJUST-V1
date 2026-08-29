# PHASE 1 — AUDIT RESULT

## Architecture Lock metadata

- Target release: **v1.3.0 — Full Manual Adjust Mode**.
- Production baseline: `FINAL-PHASE-01-AUG` at
  `d27877a2482a642a21b87011cf5ae9f1fec04f01` (the local branch name `work`
  is an environment alias for this exact snapshot).
- Audit date: 2026-08-29 (UTC).
- Scope: audit and design only. No Full Manual Adjust implementation and no
  AUTO behavior change is included in this phase.
- Governing principle: AUTO Bonus Reload v1.2.0 is a frozen production engine.
  Manual Adjust is an additive, isolated engine, not a genericization of AUTO.

## 1. Baseline

| Item | Audited value |
|---|---|
| Authoritative branch | `FINAL-PHASE-01-AUG` |
| Verified HEAD | `d27877a2482a642a21b87011cf5ae9f1fec04f01` |
| Local branch | `work` (approved environment detail) |
| Version | `v1.2.0` in `config/config.json` |
| Working tree before Phase 1 | clean |
| Runtime in audit container | CPython 3.14.4 |
| Application runtime | Python/PySide6 desktop UI; synchronous Playwright; SQLite; gspread |
| Python dependencies | PySide6 6.10.3, Playwright 1.55.0, gspread 6.1.2, google-auth 2.34.0, google-auth-oauthlib 1.2.1, psutil 6.1.0 |
| Packaging | PyInstaller specification and Windows portable build script |
| Tests | pytest tests under `tests/`; prior JUnit/report artifacts under `test_reports/` |

The source declares itself v1.2.0, contains the v1.2 hardening, crash-state,
maintenance, health and recovery suites, and is an exact SHA match for the
externally verified production baseline. It is therefore the approved v1.2.0
production implementation for this architecture lock.

## 2. Current AUTO Architecture

### 2.1 Exact production execution path

1. `main.py` establishes portable resource/application directories, primes the
   Playwright environment, loads `config/config.json` and
   `config/selectors.json`, resolves writable credential/database/profile
   paths, initializes logging, crash recovery, diagnostics and maintenance,
   opens `DatabaseService`, constructs `Dashboard`, then enters the Qt loop.
2. `Dashboard.__init__` constructs the AUTO-only service graph:
   `MemoryCache`, `SheetService`, `Validator`, eventually `QueueManager`, and
   `PanelService`. It owns the manual-list, worker, panel-alive, metrics and
   watchdog timers and the `idle | running | monitoring | stopping` state.
3. `Dashboard._on_connect` calls `SheetService.connect`, then loads the MANUAL
   BONUS RELOAD set and constructs `QueueManager` using the existing services.
4. `SheetService.connect` uses read-only Google/Drive scopes, opens the sheet
   by parsed spreadsheet ID, requires both configured tabs, and validates that
   configured MASTER columns have non-empty headers.
5. `SheetService.read_master_rows` performs one bulk `get_all_values()` call.
   It maps B to `user_id`, E to timestamp, F to `true_amount`, and I to
   `tx_id`. Rows without TX_ID are deliberately omitted because safe AUTO
   deduplication is impossible. `_safe_int` accepts comma/space formatting and
   produces an integer (or zero on malformed input).
6. `SheetService.read_manual_set` bulk-reads MANUAL BONUS RELOAD and builds an
   exact-case set from non-empty column-B user IDs. Dashboard refreshes that
   cache periodically and performs a TTL-throttled fresh pull at submission.
7. `QueueManager.refill` reads MASTER, calls
   `DatabaseService.filter_new_tx_ids`, removes TX_IDs already persisted,
   limits pending rows to `batch_size`, and gets the cached manual set.
8. For each batch row it parses the original transaction timestamp (falling
   back to today's date if unparseable), obtains persisted successful bonus for
   `(username, original transaction date)`, and simulates bonuses from earlier
   READY rows in the same refill for that same key.
9. `Validator.validate` applies, in order: MANUAL BONUS exclusion; positive
   deposit and non-empty user validation; configured tier selection; and the
   configured daily cap. With current configuration, deposits >=100,000 yield
   10,000, deposits >=50,000 yield 5,000, and the actual award is
   `min(tier_bonus, 10,000 - current_daily_bonus)`.
10. `QueueManager.refill` persists every non-READY `LIMIT`, `INVALID`, or
    `MANUAL BONUS` outcome using `DatabaseService.bulk_insert`; only READY
    objects enter `_ready`. Preview is rebuilt rather than appended.
11. Preview/refresh are operator actions. `Dashboard._on_start` requires both
    a queue and attached panel, refills if empty, starts the worker timer, and
    enters monitoring immediately if no READY row exists.
12. `Dashboard._worker_step` takes one READY item at a time and preserves this
    contractual final validation order: (1) SQLite TX_ID check; (2) fresh
    MANUAL BONUS set check; (3) original-transaction-date daily-cap
    revalidation; (4) panel submission.
13. `PanelService.open_panel` launches/reuses one persistent Chromium context
    using the configured profile; `attach` binds the latest live page;
    `submit_deposit` fills username, integer bonus, and AUTO remark, preserves
    optional payment/currency defaults, clicks submit, then requires the
    configured success alert/text. It returns `SubmitResult`, never writes DB.
14. On apparent panel success Dashboard inserts AUTO `SUCCESS`; on apparent
    failure it inserts AUTO `FAILED`, captures a screenshot, and continues
    unless the browser closed. `processed_transactions.tx_id` is the primary
    key and inserts use `INSERT OR IGNORE`.
15. When READY is exhausted, AUTO enters monitoring. At each configured
    interval it calls the same `QueueManager.refill`; new READY rows resume
    processing, while an empty result resets the countdown. It does not end a
    finite cycle.
16. Stop during processing is cooperative: finish the current synchronous
    transaction, then `_finalise_stop`. Stop during monitoring is immediate.
    Finalization stops worker/metrics timers, returns state/UI to idle, and
    performs any deferred manual-list refresh.

### 2.2 Verified behavior versus the supplied summary

- **Verified:** Sheets are read-only; MASTER B/F/I supply user/amount/TX_ID;
  SQLite TX_ID blocks replay; current tiers and cap are 5,000/10,000 and
  10,000; partial cap uses `min`; business totals use original transaction
  date; MANUAL BONUS exclusion exists; only READY is worked; non-READY is
  persisted; PanelService submits; AUTO refills/monitors.
- **Important qualification:** the MANUAL BONUS comparison is exact-case after
  trimming, not lowercased. This is an AUTO frozen behavior, not something to
  silently change while implementing Manual Adjust.
- **Important qualification:** rows with blank TX_ID never become AUTO preview
  INVALID rows; `read_master_rows` omits them before validation.
- **Important qualification:** a Playwright timeout/error after the submit
  click is stored as AUTO `FAILED`, even though the external result may be
  ambiguous. Manual Adjust must not inherit this financial-safety weakness.
- **Important qualification:** successful external submit followed by an AUTO
  DB insert exception marks the in-memory item processed/failed, but the TX_ID
  is not durably recorded. This existing AUTO behavior is frozen; Manual uses
  the safer state model below.

## 3. Frozen Contracts

The following methods and observable behaviors are regression-sensitive and
must not be changed as part of Manual Adjust:

- `main.py`: `_prime_playwright_env`, `_ensure_runtime_layout`, path anchoring,
  startup order, `DatabaseService`/`Dashboard` construction, crash-state and
  graceful shutdown behavior.
- `SheetService`: `extract_spreadsheet_id`, `_authorize`, `connect`,
  `_validate_headers`, `read_master_rows`, `read_manual_set`, `_safe_int`, and
  the current `MasterRow` contract. In particular, keep read-only scopes,
  configured column mapping, blank-TX omission and manual-set semantics.
- `Validator.__init__`, `_tier_bonus`, `validate`, `_to_int`: tier ordering,
  thresholds, partial-cap calculation, validation order and AUTO status values.
- `QueueManager`: constructor contract, `refill`, preview replacement,
  batch slicing, TX_ID prefiltering, original-date simulation, non-READY bulk
  persistence, READY-only queue, `next_ready`, `is_empty`, `ready_count`,
  `stats`, and `mark_processed`.
- `DatabaseService`: existing schema and indexes; WAL/synchronous settings;
  `_migrate_timestamp_date_column`; `has_tx`; `filter_new_tx_ids`; `insert`;
  `bulk_insert`; both daily bonus queries; maintenance/export/backup helpers;
  `tx_id` primary-key and `INSERT OR IGNORE` semantics.
- `PanelService`: browser lifecycle and single persistent profile,
  `open_panel`, `attach`, `submit_deposit` signature and AUTO fill/click/wait
  behavior, selector meaning, `SubmitResult`, screenshots and close/dispose.
- `Dashboard`: current service graph; connect/manual reload/panel attach;
  refresh and preview; `_on_start`, `_on_stop`, `_worker_step` including the
  four-step pre-submit order; monitoring enter/exit/tick; panel-loss handling;
  `_finalise_stop`; AUTO metrics/statistics; timer behavior and controls.
- `config/config.json`: all current AUTO keys/defaults, column numbers, rules,
  intervals, remark, browser profile and allowed result values.
- `config/selectors.json`: existing selectors, defaults, success text and
  timeout interpretation.

### AUTO regression gate for every later phase

- [ ] 50,000 deposit still resolves to 5,000 bonus.
- [ ] 100,000 and larger deposit still resolves to 10,000 bonus.
- [ ] Deposit below 50,000 remains AUTO INVALID.
- [ ] Daily cap remains 10,000.
- [ ] Partial remaining-cap behavior remains unchanged.
- [ ] Duplicate TX_ID remains blocked during refill and immediately pre-submit.
- [ ] Blank TX_ID remains omitted by the AUTO sheet reader.
- [ ] MANUAL BONUS RELOAD exclusion and its pre-submit fresh-check order remain
      unchanged, including existing case sensitivity.
- [ ] Daily-limit decisions remain keyed by original transaction date;
      unparseable timestamp fallback remains today.
- [ ] In-refill simulated bonus accumulation remains per username/date.
- [ ] Only READY enters the AUTO worker; non-READY outcomes remain persisted.
- [ ] Preview remains rebuilt on each AUTO refill.
- [ ] AUTO batch-size slicing remains unchanged.
- [ ] AUTO monitoring and refill cadence remain unchanged.
- [ ] AUTO Stop remains immediate in monitoring and cooperative after the
      current transaction while running.
- [ ] `processed_transactions` schema, primary key, results and queries remain
      compatible; legacy `timestamp_date` migration/backfill remains compatible.
- [ ] AUTO `submit_deposit(user_id, bonus, remark)` and result handling remain
      unchanged.
- [ ] Persistent Chromium session/profile, selector and attach behavior remain
      unchanged.
- [ ] Existing AUTO dashboard labels, buttons, timers, KPIs, health/recovery,
      maintenance, crash-state and portable path behavior remain unchanged.
- [ ] The complete pre-existing test suite passes unchanged.

## 4. Full Manual Adjust Architecture

### 4.1 Decision and proposed files

Manual Adjust **must use a separate `ManualAdjustQueue`/controller**, not a mode
flag inside `QueueManager`. AUTO is an infinite polling transaction feed keyed
by TX_ID and coupled to Validator/date/cap/manual-exclusion rules. Manual is a
finite persisted user snapshot keyed by `(cycle_id, username_key)`, uses exact
amounts, and stops at end. Sharing the class would create unsafe conditional
branches in the frozen engine and invite monitoring/refill leakage.

Proposed new modules:

- `core/manual_adjust_models.py`: typed cycle/source-row/work-item/status
  models and normalization/positive-integer parsing.
- `core/manual_adjust_repository.py`: dedicated schema migration and atomic
  cycle, snapshot, claim, state-transition, reconciliation and aggregate APIs.
  It may use the same SQLite file/connection, but never the AUTO table/API.
- `core/manual_adjust_loader.py`: exactly one MASTER bulk read per explicit
  Load action; preserves every physical source row; reads only configured B/F
  (optional I as provenance); validates without `Validator`; first normalized
  user wins.
- `core/manual_adjust_queue.py`: finite queue over persisted eligible rows;
  never calls `SheetService`, `Validator`, AUTO `QueueManager.refill`, manual
  exclusion or AUTO database queries; no monitoring transition.
- `core/manual_adjust_controller.py`: preview confirmation, exclusive execution
  ownership, durable claim, panel call, state transition, continuation,
  completion, stop/resume and reconciliation policy.
- `ui/manual_adjust_view.py`: isolated preview, confirmation, execution and
  reconciliation UI. It receives explicit services and has its own state and
  timers rather than reusing AUTO dashboard fields.
- New tests under `tests/manual_adjust/` plus AUTO regression additions.

### 4.2 Event flow

1. Operator selects MANUAL only while AUTO is idle; the UI creates an exclusive
   mode session. AUTO worker/monitoring/manual-refresh timers remain stopped.
2. Operator clicks **Load snapshot**. One bulk sheet read is materialized into a
   newly allocated cycle in a single SQLite transaction. The immutable source
   provenance includes spreadsheet ID, tab, load timestamp and row number.
3. Loader computes `username = raw.strip()` and
   `username_key = username.casefold()` (which is at least as strong as required
   `lower()`), and parses F after removing comma and ordinary whitespace. Only
   a syntactically integral value >0 is valid. It does not accept truncating
   decimals and applies no AUTO rule.
4. Every source row is persisted. The first occurrence of each non-empty
   normalized key owns that key for the cycle. If it is valid it creates the
   sole executable transaction; if its amount is invalid it remains INVALID
   and no later occurrence may replace it. Later occurrences are recorded as
   `DUPLICATE` with a pointer to that first row. Empty user IDs have no key and
   are INVALID. Duplicate classification follows source order.
5. Preview is read only from that persisted snapshot and shows source rows,
   unique users, READY, duplicate, invalid and sum of READY exact amounts, plus
   USER ID/AMOUNT/STATUS rows. Sheet edits after load are irrelevant.
6. Confirmation repeats executable count and total, explicitly warns that real
   adjustments will be submitted, and requires Confirm & Start. The confirmed
   snapshot cannot be edited or reloaded in place.
7. For each row, controller atomically claims it in SQLite before any panel
   interaction, then uses a manual-specific submission API with the exact F
   integer and a dedicated manual remark. The API must expose whether the
   submit-click boundary was crossed.
8. Definite success becomes `SUCCESS`; a definite pre-click failure becomes
   `FAILED_NOT_SUBMITTED` and processing continues; any post-click uncertainty
   becomes `UNKNOWN` and is never automatically retried. Other users continue
   unless the browser/session is unavailable, in which case the controller
   safely stops the cycle without converting untouched PENDING rows.
9. When no PENDING/claimed operation remains, a cycle with no UNKNOWN becomes
   `COMPLETED` (failures may still exist); any UNKNOWN makes it
   `REVIEW_REQUIRED`. Worker becomes STOP/IDLE. No sheet reread and no refill.

TRUE AMOUNT is passed 1:1. Manual never invokes tier conversion, thresholds,
daily cap, current bonus queries, MANUAL BONUS exclusion, or `Validator`.

### 4.3 `cycle_id`

Use a locally generated UUIDv7 (or UUID4 if the runtime lacks UUIDv7), stored as
canonical lowercase text and created inside the database transaction that
inserts the cycle. Identity must be random/unique and independent of sheet
contents or time alone. Human display may add a timestamp prefix, but it is not
identity. A unique `snapshot_fingerprint` (hash of spreadsheet ID, tab and the
ordered raw B/F/I snapshot) is diagnostic only: it must not prevent an
intentional later cycle containing the same users.

## 5. Database Design

Use the existing SQLite file for atomic local durability/backup convenience,
but dedicated tables and repository methods. Do not add mode columns or rows to
`processed_transactions`. Enable foreign keys for the repository connection
and perform snapshot/transition writes in explicit transactions.

Three tables are required because preserving *all* duplicate/invalid source
rows conflicts with having exactly one executable row per username:

```sql
CREATE TABLE manual_adjust_cycles (
    cycle_id            TEXT PRIMARY KEY,
    status              TEXT NOT NULL CHECK (status IN
      ('LOADING','PREVIEW','RUNNING','STOPPED','REVIEW_REQUIRED','COMPLETED','CANCELLED')),
    spreadsheet_id      TEXT NOT NULL,
    sheet_name          TEXT NOT NULL,
    snapshot_fingerprint TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    loaded_at           TEXT,
    confirmed_at        TEXT,
    started_at          TEXT,
    stopped_at          TEXT,
    completed_at        TEXT,
    executor_id         TEXT,
    lease_heartbeat_at  TEXT,
    total_source_rows   INTEGER NOT NULL DEFAULT 0 CHECK(total_source_rows >= 0),
    total_unique_users  INTEGER NOT NULL DEFAULT 0 CHECK(total_unique_users >= 0),
    ready_count         INTEGER NOT NULL DEFAULT 0 CHECK(ready_count >= 0),
    duplicate_count     INTEGER NOT NULL DEFAULT 0 CHECK(duplicate_count >= 0),
    invalid_count       INTEGER NOT NULL DEFAULT 0 CHECK(invalid_count >= 0),
    total_amount        INTEGER NOT NULL DEFAULT 0 CHECK(total_amount >= 0),
    success_count       INTEGER NOT NULL DEFAULT 0 CHECK(success_count >= 0),
    failed_count        INTEGER NOT NULL DEFAULT 0 CHECK(failed_count >= 0),
    unknown_count       INTEGER NOT NULL DEFAULT 0 CHECK(unknown_count >= 0),
    CHECK (completed_at IS NULL OR status IN ('COMPLETED','CANCELLED'))
);

CREATE TABLE manual_adjust_source_rows (
    source_row_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id            TEXT NOT NULL REFERENCES manual_adjust_cycles(cycle_id),
    source_row          INTEGER NOT NULL CHECK(source_row >= 2),
    source_tx_id        TEXT,
    username_raw        TEXT,
    amount_raw          TEXT,
    username            TEXT,
    username_key        TEXT,
    parsed_amount       INTEGER,
    classification      TEXT NOT NULL CHECK(classification IN
      ('READY','DUPLICATE','INVALID')),
    reason              TEXT,
    winner_source_row_id INTEGER REFERENCES manual_adjust_source_rows(source_row_id),
    UNIQUE(cycle_id, source_row)
);

CREATE TABLE manual_adjust_transactions (
    transaction_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id            TEXT NOT NULL REFERENCES manual_adjust_cycles(cycle_id),
    source_row_id       INTEGER NOT NULL UNIQUE
                         REFERENCES manual_adjust_source_rows(source_row_id),
    username            TEXT NOT NULL,
    username_key        TEXT NOT NULL,
    adjust_amount       INTEGER NOT NULL CHECK(adjust_amount > 0),
    status              TEXT NOT NULL CHECK(status IN
      ('PENDING','SUBMITTING','SUCCESS','FAILED_NOT_SUBMITTED','UNKNOWN','CANCELLED')),
    attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    attempt_id          TEXT,
    claimed_at          TEXT,
    submit_clicked_at   TEXT,
    processed_at        TEXT,
    error_detail        TEXT,
    evidence_detail     TEXT,
    reconciled_at       TEXT,
    reconciled_by       TEXT,
    resolution_note     TEXT NOT NULL DEFAULT '',
    UNIQUE(cycle_id, username_key)
);

CREATE INDEX idx_manual_cycle_status
    ON manual_adjust_transactions(cycle_id, status, source_row_id);
CREATE INDEX idx_manual_source_class
    ON manual_adjust_source_rows(cycle_id, classification, source_row);
```

The unique executable constraint is the final per-cycle duplicate barrier.
Source rows retain duplicates/invalids for audit. Counts should normally be
derived in queries; cached cycle aggregates are updated in the same transaction
and verified before confirmation/completion. Store money as SQLite INTEGER and
reject values above the application's explicitly tested safe maximum (at most
signed 64-bit); never use float.

Schema installation must be additive and versioned in a dedicated migration
record (for example `_meta.manual_adjust_schema_version`). `CREATE TABLE/INDEX
IF NOT EXISTS` must not modify or backfill `processed_transactions`.

## 6. Manual Cycle State Machine

### 6.1 Cycle states

- `LOADING -> PREVIEW`: one read and atomic snapshot succeeded.
- `LOADING -> CANCELLED`: read/validation persistence failed; no execution.
- `PREVIEW -> RUNNING`: operator confirmed immutable counts/total and panel is
  attached; executor ownership acquired.
- `PREVIEW -> CANCELLED`: operator cancels.
- `RUNNING -> STOPPED`: operator requests stop after current safe boundary, or
  panel becomes unavailable before the next click.
- `RUNNING -> COMPLETED`: no PENDING/SUBMITTING/UNKNOWN remains; failures are
  terminal and do not prevent completion.
- `RUNNING|STOPPED -> REVIEW_REQUIRED`: stale SUBMITTING or any UNKNOWN exists.
- `STOPPED -> RUNNING`: resume after ownership checks, but only PENDING and
  explicitly operator-approved `FAILED_NOT_SUBMITTED` rows can execute.
- `REVIEW_REQUIRED -> STOPPED|COMPLETED`: every UNKNOWN is manually reconciled
  with durable evidence; never an automatic transition to retry.
- `COMPLETED` and `CANCELLED` are terminal. A later run creates another cycle.

### 6.2 Transaction states

- `PENDING -> SUBMITTING`: atomic durable claim immediately before panel work;
  assigns attempt ID, increments attempt count, commits before form submission.
- `SUBMITTING -> SUCCESS`: explicit site success observed and result committed.
- `SUBMITTING -> FAILED_NOT_SUBMITTED`: failure is proven to have occurred
  before submit click (browser unavailable, field validation/navigation failure).
- `SUBMITTING -> UNKNOWN`: submit click occurred but success/non-acceptance
  cannot be proved, or the process/lease disappears while SUBMITTING.
- `FAILED_NOT_SUBMITTED -> SUBMITTING`: only explicit operator retry; never a
  blanket automatic retry policy.
- `UNKNOWN -> SUCCESS`: operator reconciliation proves panel accepted it.
- `UNKNOWN -> FAILED_NOT_SUBMITTED`: only authoritative panel audit proves no
  submission; requires operator identity, timestamp, evidence and note before a
  separately confirmed retry.
- `PENDING -> CANCELLED`: operator intentionally excludes an untouched row
  before execution; optional, audited, and never permitted after claim.
- `SUCCESS` is immutable. No state transitions back to PENDING.

`SubmitResult(False)` alone is not enough to select FAILED versus UNKNOWN. The
manual submission contract needs `click_crossed`/phase plus evidence. Any
exception where phase cannot be durably established resolves to UNKNOWN.

## 7. Duplicate Protection

1. **Load:** iterate fixed source order, normalize using
   `strip().casefold()`, and put the first non-empty normalized user in a local
   seen map before amount validation. Later matching rows are persisted
   DUPLICATE pointing to the first occurrence; amounts are neither merged nor
   replaced. Thus an invalid first amount cannot be bypassed by a later row for
   the same user. A blank user has no comparison key and is simply INVALID.
2. **Snapshot transaction:** insert all source audit rows and executable rows
   atomically. A constraint conflict never silently replaces the winner. It is
   handled as duplicate/audit evidence or aborts the snapshot if inconsistent.
3. **Queue creation:** query only persisted `manual_adjust_transactions` in
   `PENDING` state ordered by winner source row. Never recreate work from the
   sheet or from duplicate source rows.
4. **Database:** `UNIQUE(cycle_id, username_key)` is authoritative and survives
   process loss. No `INSERT OR REPLACE`; first insert wins.
5. **Immediately pre-submit:** in one `BEGIN IMMEDIATE` transaction, verify
   cycle ownership/state, transaction PENDING (or explicitly retried terminal
   failure), absence of another nonterminal operation for the same key, and
   atomically change it to SUBMITTING. Only the row returned by this claim may
   reach the panel.
6. **Cross cycle:** cycle ID is part of the unique key, so the same normalized
   username is intentionally allowed once in a different cycle.

RAM is only an optimization. Recovery always reconstructs from constraints and
persisted statuses, guaranteeing crash-surviving duplicate protection.

## 8. Crash / Unknown Submission Safety

### 8.1 The irreducible boundary

SQLite and the remote website cannot participate in one atomic transaction.
Therefore local code alone cannot prove the result if power is lost between the
remote acceptance and the local SUCCESS commit. The safe containment policy is
**never automatically retry an attempt that might have crossed submit**.

Before touching the panel, commit SUBMITTING with a unique attempt ID. The
manual panel path records its phase in memory and, where possible, commits
`submit_clicked_at` immediately before clicking. This timestamp improves the
audit trail but cannot eliminate the micro-window between click and DB write.
Thus *every stale SUBMITTING*, with or without `submit_clicked_at`, becomes
UNKNOWN at recovery, never PENDING/FAILED.

If the site provides an authoritative transaction history, search it using the
username, exact amount, dedicated manual remark, attempt/cycle correlation text
(if the panel accepts it), and time window. If the site supports an idempotency
key, use `attempt_id`; that is the only way to make automatic retry safe. The
audited panel currently exposes no proven idempotency or history API, so Phase 2
must assume neither until integration testing proves it.

### 8.2 DB write failure after website success

- Stop issuing submissions immediately because durable safety is degraded.
- Keep the row SUBMITTING if no write is possible; on restart it becomes
  UNKNOWN. Also write best-effort append-only emergency log/screenshot, but
  never treat those as the authoritative state transition.
- Put the cycle in REVIEW_REQUIRED when SQLite becomes writable (or infer it
  from stale SUBMITTING at restart).
- Do not retry that user. Operator must reconcile against panel history/support
  and record evidence. If panel acceptance is proved, mark SUCCESS. If and only
  if authoritative evidence proves non-submission, resolve as
  FAILED_NOT_SUBMITTED and separately confirm a retry.

### 8.3 Restart and resume

At startup, acquire a single-instance/executor lock and inspect nonterminal
cycles. A cycle whose lease is stale changes to REVIEW_REQUIRED if any row is
SUBMITTING; those rows become UNKNOWN in one transaction. Untouched PENDING
rows remain safe, SUCCESS remains immutable, and definite failures remain
terminal.

Incomplete cycles are **selectively resumable**, never blindly resumable:

1. Show persisted snapshot and status totals without rereading Sheets.
2. Require operator selection of the same cycle and panel attachment.
3. Block resume while any UNKNOWN is unresolved (conservative cycle-wide
   barrier). This prevents an operator overlooking ambiguous money movement.
4. After reconciliation, resume only persisted PENDING rows. A
   FAILED_NOT_SUBMITTED retry requires a separate explicit confirmation.
5. Acquire executor ownership/heartbeat to prevent two app instances working
   one cycle. A database transaction/unique active-owner guard must reject a
   second executor.

Classification is therefore precise: SUCCESS means site success was observed
and committed (or later reconciled with evidence); definitely NOT submitted is
only a pre-click failure or authoritative negative reconciliation; everything
else is UNKNOWN and operator-review-only.

## 9. AUTO / MANUAL Isolation

- Manual loader/parser never imports or invokes `Validator`.
- Manual queue/controller never imports or invokes AUTO `QueueManager`.
- Manual persistence never inserts, updates, deletes or queries
  `processed_transactions`; AUTO daily queries retain their existing table-only
  SQL, so Manual amounts cannot affect caps or KPIs.
- Manual uses `adjust_amount` directly from parsed F and a separate remark; it
  has no bonus field and no tier/cap/manual-exclusion path.
- Manual execution is a persisted finite snapshot and has no refill,
  monitoring timer or automatic sheet read.
- AUTO and MANUAL may share a connected read-only SheetService, SQLite file and
  browser context only at service boundaries; their state, queues, timers,
  controls, statistics and tables remain separate.
- A top-level mutually exclusive mode coordinator permits switching only when
  the active mode is idle and no submission is in flight. Switching out of
  running/monitoring requires Stop and completion of the safe stop boundary.
- Entering MANUAL stops/disconnects AUTO worker and monitoring timers, does not
  reuse AUTO preview/current item/session counters, and never starts the AUTO
  manual-list refresh as part of Manual processing. Entering AUTO hides and
  disables Manual controls and cannot auto-resume a manual cycle.
- Startup must present nonterminal Manual cycles for review but must not start
  either mode automatically. Panel attachment is explicit for each run.

PanelService can be reused safely only additively. Keep `submit_deposit`
untouched for AUTO. Add a manual-specific `submit_adjustment` or wrapper that
uses the same low-level selectors/browser but supplies exact amount and exposes
pre-click/post-click phase/evidence. Do not route AUTO through the new method in
the same release. If safely factoring internals would alter AUTO, duplicate the
small manual flow instead and accept that maintenance cost.

## 10. Required Existing-File Changes

| File | Classification for implementation | Locked rationale |
|---|---|---|
| `main.py` | ADDITIVE ONLY | Construct/inject manual repository/controller or mode coordinator; retain startup/shutdown order and portable paths. |
| `core/sheet_service.py` | ADDITIVE ONLY | Add a raw one-shot MASTER B/F(/I) snapshot method that does not require TX_ID and does not use lossy `_safe_int`; existing reads untouched. A new loader may access the connected worksheet through a narrow new API. |
| `core/validator.py` | **NONE** | Manual must bypass all Bonus Reload rules. |
| `core/queue_manager.py` | **NONE** | Manual gets a separate finite queue. |
| `core/database.py` | Prefer **NONE** | New repository owns additive Manual schema/APIs using an injected connection or carefully coordinated connection. If connection access/migration hookup is unavoidable, expose only a narrow additive hook; no AUTO SQL changes. |
| `core/panel_service.py` | ADDITIVE ONLY | Add manual submission contract/phase reporting while preserving `submit_deposit` byte-for-byte where practical. |
| `ui/dashboard.py` | ADDITIVE ONLY | Add exclusive mode navigation/integration; keep AUTO handlers/state/timers unchanged and delegate Manual UI to a new view/controller. |
| `config/config.json` | ADDITIVE ONLY | Add separate manual remark/safety settings only; do not rename/change AUTO keys. Schema statuses should not be forced into AUTO `allowed_results`. |
| `config/selectors.json` | **NONE** initially | Reuse current fields only if the real Manual panel form is identical; add separate selector keys only when panel audit proves necessary. |
| `core/memory_cache.py` | **NONE** | Manual duplicate/state safety is database-backed. |
| `core/timestamp_utils.py` | **NONE** | Manual has no transaction-date bonus rule. |
| AUTO hardening/recovery/maintenance modules | **NONE** initially | Preserve production behavior; add independent manual recovery integration rather than changing generic safety components prematurely. |
| `core/manual_adjust_models.py` | **NEW FILE** | Manual-only types, statuses, normalization and exact parser. |
| `core/manual_adjust_repository.py` | **NEW FILE** | Isolated persistence, constraints and state transitions. |
| `core/manual_adjust_loader.py` | **NEW FILE** | One-shot immutable snapshot and first-wins classification. |
| `core/manual_adjust_queue.py` | **NEW FILE** | Finite persisted queue, no monitoring/refill. |
| `core/manual_adjust_controller.py` | **NEW FILE** | Execution ownership and financial-safety state machine. |
| `ui/manual_adjust_view.py` | **NEW FILE** | Preview/confirmation/progress/reconciliation UI. |
| `tests/manual_adjust/*` | **NEW FILES** | Feature, fault-injection and scale coverage. |

This is the least invasive strategy. In particular, both Validator and AUTO
QueueManager can remain completely untouched.

## 11. Tests Required

No real-money production enablement is allowed until all tests below pass in a
staging panel and the complete frozen AUTO suite passes.

### 11.1 Loader and validation

- Valid user/exact positive integer; comma-formatted and whitespace-formatted
  integer; leading/trailing username whitespace; Unicode/case normalization.
- Blank user, blank amount, malformed text, decimal/fraction, zero, negative,
  overflow and excessively large amount.
- Explicit proof that 1, 49,999, 50,000, 100,000 and values above daily cap are
  accepted unchanged; a user in MANUAL BONUS is accepted unchanged.
- Blank TX_ID remains valid Manual input; optional TX_ID is provenance only.
- Snapshot read exactly once; source order and row numbers preserved; sheet
  mutation after load does not affect preview or work.

### 11.2 Duplicate and database constraints

- Exact, case-only, leading/trailing whitespace, mixed-case and far-apart
  duplicates; first occurrence wins; amounts never sum/replace.
- Invalid-amount first row followed by a valid same user leaves the first row
  INVALID and classifies the later row DUPLICATE (first occurrence wins);
  valid first row followed by invalid/malformed same-user row preserves the
  first winner and classifies the later row DUPLICATE.
- 100+ duplicates produce one executable row and complete audit rows.
- Direct concurrent/hostile duplicate inserts fail at UNIQUE constraint.
- Same user once in two different cycles is allowed.
- Snapshot transaction rollback leaves no partial executable cycle.
- Migration on fresh and existing v1.2 DB; repeat migration idempotence; AUTO
  table schema/data/query plans/results unchanged.

### 11.3 Preview, confirmation and lifecycle

- Counts and total reconcile against persisted rows, including duplicates and
  invalids; total sums READY exact amounts only; integer formatting correct.
- Confirmation displays exact executable count/total; cancel performs no
  submission; snapshot immutable after confirmation.
- End of list marks COMPLETED/IDLE and performs no read/refill; new sheet rows
  wait for a new intentional cycle.
- Stop before first row, between rows and during an operation respects safe
  boundaries. Separate cycles and state/timers cannot bleed across mode switch.

### 11.4 Execution and continuation

- Exact amount and dedicated remark reach form; no tier/cap/Validator call.
- Success; definite pre-click timeout; panel closed before click; failed field
  fill; post-click timeout; unexpected alert; site rejection proven/not proven;
  continuation after a terminal failure; halt on lost durability.
- Immediate pre-submit claim blocks duplicate/concurrent executor; two app
  instances cannot operate one cycle.
- Browser/profile/selectors work in source and portable build.

### 11.5 Crash and fault injection

Kill the process deterministically: before PENDING claim; after SUBMITTING
commit; before click; at click; immediately after website success; before
SUCCESS commit; after SUCCESS commit. Also inject SQLite busy/full/I/O/corrupt
errors at every transition and UI freeze/forced close.

Expected invariants: untouched stays PENDING; committed SUCCESS never retries;
every stale SUBMITTING becomes UNKNOWN; no UNKNOWN automatically retries;
review evidence is mandatory; resume uses persisted snapshot; DB failure after
remote success stops new submissions.

### 11.6 Scale and endurance

- Deterministic 100, 500 and 1,000-row snapshots, including 100+ duplicates,
  with exact totals and no duplicate submit calls.
- Restart/reconcile/resume at scale; bounded UI memory; acceptable snapshot and
  queue latency; SQLite WAL/backup/integrity behavior.

### 11.7 AUTO regression

Automate every checkbox in Section 3, retain all current tests, add contract
spies proving Manual never calls Validator/AUTO queue/processed_transactions,
and conduct staging smoke tests for connect, preview, start, submit, monitoring,
stop, browser recovery, maintenance and portable restart.

## 12. Risks

### HIGH

- **Remote/local atomicity gap:** a click can succeed while local SUCCESS is
  absent. Contained by durable SUBMITTING, UNKNOWN quarantine and operator
  reconciliation; eliminated only if the panel supports idempotency/history.
- **Panel false-negative:** current `submit_deposit` cannot distinguish pre-
  from post-click failures. Manual cannot reuse its coarse result contract.
- **Concurrent execution:** two application instances could double-adjust
  without an executor lease/DB claim. Single-cycle ownership is mandatory.
- **Mode leakage:** reusing Dashboard worker/monitoring state could reread the
  sheet or apply Validator. Separate controller/queue/timer is mandatory.

### MEDIUM

- SQLite disk-full/lock/corruption during execution; stop submissions on lost
  durability, preserve UNKNOWN, provide backup/integrity/runbook.
- Username normalization expectations (Unicode/casefold) may differ from panel
  account identity. Confirm with real account rules while retaining at least
  trim/lower equivalence.
- `_safe_int` is lossy (`float`, malformed -> zero); Manual requires a new
  strict parser and raw values.
- Existing connect requires MANUAL BONUS RELOAD tab although Manual does not
  use it. Preserve AUTO contract; decide whether Manual connection can use a
  new additive capability check without weakening AUTO.
- Cycle aggregate drift if cached counters are not transactional; reconcile
  against row queries at preview/completion.

### LOW

- Additional schema increases backup/export/maintenance surface; dedicated
  migrations and integration tests contain it.
- Shared selectors/profile may evolve; separate configuration keys can be
  introduced additively if staging proves differences.

## 13. Recommended Implementation Sequence

1. Freeze baseline with automated AUTO characterization tests for Section 3,
   including source-order assertions around Dashboard final validation.
2. Add manual status models, strict raw parser and normalization unit tests.
3. Add isolated repository/schema migration, constraints, transactional
   snapshot API and migration compatibility tests.
4. Add one-shot loader and immutable persisted preview; test validation,
   duplicates, totals, cross-cycle behavior and scale. No panel integration.
5. Add finite ManualAdjustQueue reading only persisted PENDING rows; prove zero
   Sheet/Validator/AUTO DB calls after load.
6. Define and test PanelService additive manual submission result with explicit
   click phase. Verify selectors, remark, exact amount, panel history and any
   idempotency support in staging before choosing reconciliation automation.
7. Add controller state machine, executor ownership, durable claims, UNKNOWN
   recovery and fault-injection tests. Default to operator review.
8. Add isolated Manual UI preview/confirmation/progress/reconciliation and
   mutually exclusive Dashboard mode coordinator.
9. Run crash matrix, two-instance/concurrency, 1,000-row and portable-build
   staging tests; write operator recovery/reconciliation runbook.
10. Run the entire AUTO regression gate plus manual suite. Perform staged dry
    runs, then small controlled real-panel acceptance with independent ledger
    verification and rollback/stop authority.
11. Only after review approval, update release metadata and create the
    `v1.3.0-manual-arch-lock` checkpoint if it does not exist. Do not create the
    final v1.3.0 tag during implementation and never overwrite tags.

## Architecture questions — explicit answers

1. **Exact AUTO files:** `main.py`, `config/config.json`,
   `config/selectors.json`, `ui/dashboard.py`, `core/sheet_service.py`,
   `core/memory_cache.py`, `core/queue_manager.py`, `core/validator.py`,
   `core/timestamp_utils.py`, `core/database.py`, `core/panel_service.py`, and
   the logger/recovery/health/maintenance/crash/diagnostic support modules
   initialized by main/dashboard.
2. **Regression-sensitive methods:** enumerated exactly in Section 3; most
   critical are Validator `_tier_bonus`/`validate`, QueueManager `refill` and
   queue primitives, DatabaseService dedup/insert/daily methods/migration,
   SheetService connect/reads, PanelService lifecycle/`submit_deposit`, and
   Dashboard connect/start/stop/worker/monitor/finalize handlers.
3. **Existing files completely untouched:** `core/validator.py`,
   `core/queue_manager.py`, `core/memory_cache.py`,
   `core/timestamp_utils.py`, and initially selectors and hardening modules.
4. **Additive integration:** `main.py`, `core/sheet_service.py`,
   `core/panel_service.py`, `ui/dashboard.py`, `config/config.json`; preferably
   no `core/database.py` change because the new repository owns its schema.
5. **Separate queue?** Yes: finite immutable username-cycle work has opposing
   identity, validation and lifecycle semantics from AUTO polling/TX_ID work.
6. **Cycle ID:** database-created UUIDv7/UUID4, not a sheet-derived hash or
   timestamp; fingerprint is provenance only.
7. **Snapshot location:** all rows and executable winners in dedicated SQLite
   Manual tables, atomically committed before preview.
8. **Duplicate layers:** normalized seen-map at load; queue from persisted
   winners only; UNIQUE cycle/key; atomic state/ownership claim immediately
   before submission.
9. **Crash survival:** durable rows, unique constraint and SUBMITTING claim are
   committed before panel work; recovery never rebuilds from RAM/sheet.
10. **No AUTO amount contamination:** no Manual rows in
    `processed_transactions`; no calls to AUTO daily queries/cache/Validator.
11. **Panel reuse:** yes, browser and selectors can be shared through a new
    additive method; existing AUTO `submit_deposit` remains unchanged.
12. **Remote success/DB failure:** stop submissions, retain/infer UNKNOWN,
    require authoritative operator reconciliation, never blindly retry.
13. **Half-cycle restart:** load persisted cycle; stale SUBMITTING -> UNKNOWN;
    keep SUCCESS and PENDING; show review rather than auto-start.
14. **Resumable?** Selectively: only after UNKNOWN resolution, and only PENDING
    plus separately confirmed definitely-not-submitted failures.
15. **Certainty classification:** committed/reconciled evidence = SUCCESS;
    proven pre-click/authoritative negative = definitely not submitted; any
    crossed-or-uncertain click boundary = UNKNOWN.
16. **Protective model:** cycle/source/executable tables, unique cycle/key,
    immutable SUCCESS, durable PENDING -> SUBMITTING claim, distinct
    FAILED_NOT_SUBMITTED and UNKNOWN, ownership/attempt/evidence fields.
17. **UI switch:** mutually exclusive coordinator; switching only at idle safe
    boundary; separate controllers/timers/models; no automatic resume/refill.
18. **Tests before money:** all loader, duplicate, cycle, execution,
    concurrency, crash/fault, reconciliation, scale, portable/staging and full
    AUTO regression suites in Section 11.

## 14. Files Changed in Phase 1

- `docs/FULL_MANUAL_ADJUST_ARCHITECTURE.md` — this documentation-only audit and
  Architecture Lock.
- No Python, UI, configuration, selector, dependency, database or production
  engine file was changed.

## 15. Final Verdict

**ARCHITECTURE READY**

Phase 1 is complete at the design level. Implementation must stop here pending
explicit approval. The high-risk unknown-submission boundary is safely
contained by a conservative state machine; it is not falsely claimed to be
atomically solvable without panel idempotency or authoritative reconciliation.
