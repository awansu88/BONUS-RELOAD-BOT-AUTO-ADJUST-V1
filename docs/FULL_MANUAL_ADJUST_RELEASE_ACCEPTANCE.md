# Full Manual Adjust v1.3.0 — Release Acceptance

## Release boundary and frozen baseline

Phase 5 starts from the externally verified Phase 4 commit
`670fa17f42500ac74249e7122957a308fe932bbc`, checkpointed by
`v1.3.0-manual-execution`. AUTO Bonus Reload v1.2.0 remains frozen. Manual is
an additive finite snapshot engine with its own repository, queue, controller,
state, and view; it shares only the existing panel/browser infrastructure.

Rollback checkpoints must never be moved or rewritten:

* `v1.3.0-manual-arch-lock`
* `v1.3.0-manual-core`
* `v1.3.0-manual-ui`
* `v1.3.0-manual-execution` → `670fa17f42500ac74249e7122957a308fe932bbc`

## Financial safety invariants

* Exactly one of AUTO or Manual may execute. Mode selection alone never grants
  submission authority.
* Only the literal JSON boolean `true` enables Manual START, RESUME, or RETRY.
  The shipped value is `false`; omission and malformed truthy values are safe.
* Manual sends the positive integer parsed from TRUE AMOUNT exactly 1:1. It
  performs no tier, percentage, cap, aggregation, or AUTO dedup calculation.
* User names normalize as `raw.strip()` / `.lower()` and the first normalized
  name wins. Duplicates remain evidence and never execute or aggregate.
* A preview is an immutable SQLite snapshot. Execution, retry, recovery, and
  reconciliation never reload Sheets.
* A claim and IN_PROGRESS attempt are durable before form mutation. UNKNOWN is
  never automatically retried. Reconciliation is exact-attempt, evidence-led,
  complete, and write-once.
* Shutdown never fabricates a definitely-not-submitted result. A persisted
  SUBMITTING operation remains available for conservative stale recovery.
* Integrity contradictions block submission and require operator review; no
  ambiguous financial history is automatically repaired.

## Automated acceptance record

The release candidate must pass the Manual suite, the suite excluding the
known platform-only portable test, compileall, and `git diff --check`. Record
the exact commands and counts in the Phase 5 PR. Run the complete suite too;
if `libGL.so.1` prevents importing PySide6 on Linux, report that limitation and
do not alter production dependencies merely to hide it. No automated test may
contact a production panel; all submission tests use fakes.

## Packaging audit and Windows build checklist

`BonusReloadBot.spec` collects `main.py` through normal static imports, so the
Manual `core.*` and `ui.*` modules require no hidden imports. The existing
portable build copies the additive `config/config.json`; writable folder names
remain `config`, `credentials`, `logs`, `screenshots`,
`browser_profile_bonus_reload`, and the application directory containing
`processed.db`.

On a clean supported Windows workstation:

1. Check out the reviewed merge commit and confirm `git status --short` is
   empty.
2. Keep `manual_adjust.execution_enabled` set to `false`.
3. Run `py -m pytest -q`.
4. Run `py -m compileall -q core ui tests`.
5. Run `build_portable.bat` from a Developer Command Prompt.
6. Inspect PyInstaller output for missing Manual modules; do not add hidden
   imports unless the actual collection log proves one is needed.
7. Start the portable executable from a writable non-repository directory and
   verify all runtime folders and `processed.db` are created/reused.
8. Restart the package and confirm AUTO history and Manual cycles persist.

## Operator smoke acceptance checklist

- [ ] Back up `processed.db`, config, and browser profile.
- [ ] With the default execution gate false, run the established AUTO smoke:
      connect, preview, panel attach, one approved AUTO test, stop, monitoring,
      and restart checks.
- [ ] Select Manual only after AUTO reaches idle; confirm AUTO timers remain
      dormant.
- [ ] Load one Manual preview; verify source counts, duplicates, invalid rows,
      exact total, provenance, and frozen fingerprint.
- [ ] Change the test Sheet after loading and confirm the preview is unchanged.
- [ ] Confirm START/RESUME are unavailable while the gate is false.
- [ ] Confirm panel open/attach, cycle navigation, and load are unavailable
      during Manual RUNNING.
- [ ] Exercise STOP between users and RESUME only from STOPPED.
- [ ] Stage a definite pre-click failure, explicitly select it, verify retry
      count/total, and confirm a new attempt preserves prior evidence.
- [ ] Exercise stale recovery; acknowledge that ambiguous SUBMITTING becomes
      UNKNOWN, then reconcile using operator identity, note, and authoritative
      evidence.
- [ ] Verify UNKNOWN cannot retry automatically and FINALIZE WITH FAILURES
      reports failed count and amount.
- [ ] Return to AUTO only after Manual is durably non-RUNNING.
- [ ] Restart the final package and repeat cycle/history inspection.

## Controlled Manual test plan — prepare only

Codex must not execute this plan. The operator supplies every account and
amount after review.

1. Use a dedicated test account and one Manual cycle containing only 1–3 known
   test user IDs.
2. Obtain explicit approval for a small TRUE AMOUNT; record the panel balance
   and state before testing.
3. Temporarily set `execution_enabled` to literal `true` and restart so config
   validation is visible.
4. Load exactly once. Verify executable user count, exact total, first-wins
   duplicate behavior, and the TRUE AMOUNT 1:1 warning before START.
5. Start exactly once and confirm the panel receives exactly the displayed
   integer amount.
6. Confirm SQLite records SUCCESS and its crossed-click attempt evidence.
7. Confirm AUTO `processed_transactions` is unchanged by Manual execution.
8. Test cooperative STOP/RESUME and safe mode switching with additional
   approved staging data if required.
9. Return Manual to a safe terminal state, set `execution_enabled` back to
   `false`, restart, and revalidate configuration.

## Final release criteria and remaining manual gates

Code may be marked **ready for review**, not released, after automated gates,
schema/config/integrity/shutdown tests, AUTO regression, packaging audit, and
this artifact pass. Final `v1.3.0` tag/release requires PR approval and merge,
successful Windows packaging, operator smoke acceptance, and the controlled
Manual test above. Release ownership stays with the operator. Never include
credentials, cookies, passwords, service-account content, or session tokens in
acceptance evidence.
