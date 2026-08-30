from __future__ import annotations

import json
import sqlite3

import pytest

from core.config_validator import validate_configuration
from core.manual_adjust_controller import ManualAdjustController
from core.manual_adjust_loader import ManualAdjustLoader, snapshot_fingerprint
from core.manual_adjust_models import (AttemptResult, ClassifiedSourceRow,
                                       RawManualAdjustRow, SourceClassification)
from core.manual_adjust_repository import ManualAdjustRepository


class NeverPanel:
    is_attached = True

    def __init__(self):
        self.calls = []

    def is_alive(self):
        return True

    def submit_adjustment(self, *args):
        self.calls.append(args)
        raise AssertionError("submission must remain gated")


@pytest.fixture
def repo(tmp_path):
    value = ManualAdjustRepository(tmp_path / "manual.db")
    value.initialize_schema()
    yield value
    value.close()


def _row(number=2, username="UserABC", amount=1040243):
    return ClassifiedSourceRow(number, username, f"{amount:,}", "tx", username,
                               username.lower(), amount, SourceClassification.READY)


def _cycle(repo, rows=None):
    return repo.create_snapshot("sheet", "MASTER", "immutable-fingerprint", rows or [_row()])


@pytest.mark.parametrize("enabled", [None, "true", "yes", "1", 1, 0, [], {}])
def test_only_literal_true_can_cross_controller_execution_gate(repo, enabled):
    cid = _cycle(repo)
    config = {"manual_adjust": {"remark": "MANUAL", "lease_timeout_sec": 120,
                                "heartbeat_interval_sec": 10}}
    if enabled is not None:
        config["manual_adjust"]["execution_enabled"] = enabled
    panel = NeverPanel()
    with pytest.raises(RuntimeError, match="disabled"):
        ManualAdjustController(repo, panel, config).start(cid, confirmed=True)
    assert panel.calls == []
    assert repo.get_pending_transactions(cid)[0].attempt_count == 0


def test_manual_config_validation_is_actionable_and_does_not_mutate_auto(tmp_path):
    config = {
        "google_credentials": "credentials/service_account.json", "sqlite_path": "processed.db",
        "panel_url": "https://panel.invalid", "sheet_names": {},
        "columns": {k: 1 for k in ("user_id", "sheet_data", "time_stamp", "true_amount", "tx_id")},
        "required_headers": {k: k for k in ("user_id", "sheet_data", "time_stamp", "true_amount", "tx_id")},
        "bonus_rules": {"daily_limit": 10000, "tiers": [{"min_deposit": 50000, "bonus": 5000}]},
        "batch_size": 100, "monitoring_interval_sec": 10, "remark": "AUTO REMAINS",
        "browser": {}, "manual_adjust": {"execution_enabled": "true", "remark": " ",
        "lease_timeout_sec": 30, "heartbeat_interval_sec": 10},
    }
    cfg = tmp_path / "config.json"; cfg.write_text(json.dumps(config))
    selectors = tmp_path / "selectors.json"; selectors.write_text(json.dumps({"panel": {}, "timeouts": {}}))
    credentials = tmp_path / "service.json"; credentials.write_text(json.dumps(
        {"client_email": "x", "private_key": "x", "token_uri": "x"}))
    report = validate_configuration(app_dir=tmp_path, config_path=cfg,
        selectors_path=selectors, credentials_path=credentials,
        sqlite_path=tmp_path / "processed.db", browser_profile_dir=tmp_path / "profile")
    check = next(c for c in report.checks if c.name == "manual_adjust")
    assert not check.ok
    assert "literal true or false" in check.detail
    assert "non-empty" in check.detail
    assert "must be less" in check.detail
    assert json.loads(cfg.read_text())["remark"] == "AUTO REMAINS"


def test_schema_is_additive_idempotent_and_keeps_auto_data(tmp_path):
    path = tmp_path / "production-v12.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE processed_transactions(tx_id TEXT PRIMARY KEY, result TEXT)")
    conn.execute("INSERT INTO processed_transactions VALUES('legacy','SUCCESS')")
    conn.commit(); conn.close()
    repo = ManualAdjustRepository(path)
    repo.initialize_schema(); repo.initialize_schema()
    assert repo._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    legacy = repo._conn.execute("SELECT * FROM processed_transactions").fetchone()
    assert tuple(legacy) == ("legacy", "SUCCESS")
    assert _cycle(repo)
    repo.close()


def test_shutdown_stops_pending_cycle_and_clears_local_execution(repo):
    cid = _cycle(repo)
    panel = NeverPanel()
    ctl = ManualAdjustController(repo, panel, {"manual_adjust": {
        "execution_enabled": True, "remark": "MANUAL", "lease_timeout_sec": 120,
        "heartbeat_interval_sec": 10}})
    ctl.start(cid, confirmed=True)
    assert ctl.shutdown() == "STOPPED"
    assert repo.get_cycle(cid)["status"] == "STOPPED"
    assert ctl.cycle_id is None and ctl.queue is None and ctl.current_transaction is None


def test_shutdown_preserves_submitting_for_stale_recovery(repo):
    cid = _cycle(repo); repo.confirm_and_start(cid, "executor", 120)
    tx = repo.get_pending_transactions(cid)[0]
    repo.claim_pending(tx.transaction_id, "executor")
    ctl = ManualAdjustController(repo, NeverPanel(), {"manual_adjust": {}}, executor_id="executor")
    ctl.cycle_id = cid; ctl.current_transaction = tx
    assert ctl.shutdown() == "HARD_STOPPED"
    assert repo.get_transaction(tx.transaction_id).status.value == "SUBMITTING"
    assert repo.get_cycle(cid)["status"] == "RUNNING"


def test_integrity_rejects_impossible_cycle_ownership(repo):
    cid = _cycle(repo)
    repo._conn.execute("UPDATE manual_adjust_cycles SET executor_id='ghost' WHERE cycle_id=?", (cid,))
    assert any("cannot retain executor" in error for error in repo.validate_cycle_integrity(cid))


def test_large_snapshot_is_linear_first_wins_and_exact(repo):
    rows = []
    for i in range(1000):
        username = f"User{i % 800}"
        classification = SourceClassification.READY if i < 800 else SourceClassification.DUPLICATE
        rows.append(ClassifiedSourceRow(i + 2, username, str(i + 1), "", username,
            username.lower(), i + 1, classification,
            winner_source_row=(i % 800) + 2 if i >= 800 else None))
    cid = _cycle(repo, rows)
    summary = repo.get_cycle_summary(cid)
    assert (summary.source_rows, summary.ready, summary.duplicates) == (1000, 800, 200)
    assert summary.total_adjustment_amount == sum(range(1, 801))
    assert len(repo.get_pending_transactions(cid)) == 800


@pytest.mark.parametrize("field,value,error", [
    ("remark", " ", "remark"),
    ("lease_timeout_sec", "120", "lease_timeout_sec"),
    ("lease_timeout_sec", True, "lease_timeout_sec"),
    ("lease_timeout_sec", 0, "lease_timeout_sec"),
    ("lease_timeout_sec", -1, "lease_timeout_sec"),
    ("heartbeat_interval_sec", "10", "heartbeat_interval_sec"),
    ("heartbeat_interval_sec", True, "heartbeat_interval_sec"),
    ("heartbeat_interval_sec", 0, "heartbeat_interval_sec"),
    ("heartbeat_interval_sec", -1, "heartbeat_interval_sec"),
])
def test_complete_runtime_config_gate_blocks_start(repo, field, value, error):
    cid = _cycle(repo); panel = NeverPanel()
    settings = {"execution_enabled": True, "remark": "MANUAL",
                "lease_timeout_sec": 120, "heartbeat_interval_sec": 10}
    settings[field] = value
    ctl = ManualAdjustController(repo, panel, {"manual_adjust": settings})
    with pytest.raises(RuntimeError, match=error):
        ctl.start(cid, confirmed=True)
    assert repo.get_cycle(cid)["status"] == "PREVIEW"
    assert repo.get_attempt_history(repo.get_pending_transactions(cid)[0].transaction_id) == []
    assert panel.calls == [] and ctl.queue is None


def test_unsafe_runtime_ratio_blocks_start(repo):
    cid = _cycle(repo); panel = NeverPanel()
    ctl = ManualAdjustController(repo, panel, {"manual_adjust": {
        "execution_enabled": True, "remark": "MANUAL",
        "lease_timeout_sec": 30, "heartbeat_interval_sec": 10}})
    with pytest.raises(RuntimeError, match=r"\* 3 must be less"):
        ctl.start(cid, confirmed=True)
    assert repo.get_cycle(cid)["status"] == "PREVIEW" and not panel.calls


def test_resume_uses_complete_runtime_config_gate(repo):
    cid = _cycle(repo); repo.confirm_and_start(cid, "first", 120)
    repo.evaluate_cycle_destination(cid, "first", stopped=True)
    ctl = ManualAdjustController(repo, NeverPanel(), {"manual_adjust": {
        "execution_enabled": True, "remark": "", "lease_timeout_sec": 120,
        "heartbeat_interval_sec": 10}})
    with pytest.raises(RuntimeError, match="remark"):
        ctl.resume(cid)
    assert repo.get_cycle(cid)["status"] == "STOPPED"


def test_retry_validates_before_retry_preparation(repo):
    cid = _cycle(repo); repo.confirm_and_start(cid, "first", 120)
    tx = repo.get_pending_transactions(cid)[0]
    attempt = repo.claim_pending(tx.transaction_id, "first")
    repo.finish_attempt(attempt["attempt_id"], AttemptResult.FAILED_NOT_SUBMITTED,
                        click_crossed=False, submission_phase="FORM_STARTED")
    repo.evaluate_cycle_destination(cid, "first")
    ctl = ManualAdjustController(repo, NeverPanel(), {"manual_adjust": {
        "execution_enabled": True, "remark": "MANUAL", "lease_timeout_sec": True,
        "heartbeat_interval_sec": 10}})
    with pytest.raises(RuntimeError, match="lease_timeout_sec"):
        ctl.retry_selected(cid, [tx.transaction_id], confirmed=True)
    assert repo.get_cycle(cid)["status"] == "FAILURE_REVIEW"
    assert repo.get_transaction(tx.transaction_id).status.value == "FAILED_NOT_SUBMITTED"


def _make_stale(repo, cycle_id):
    repo._conn.execute("UPDATE manual_adjust_cycles SET lease_heartbeat_at=NULL WHERE cycle_id=?", (cycle_id,))


def test_recover_missing_executor_pending_atomically(repo):
    cid = _cycle(repo); repo.confirm_and_start(cid, "lost", 120)
    repo._conn.execute("UPDATE manual_adjust_cycles SET executor_id=NULL,lease_heartbeat_at=NULL WHERE cycle_id=?", (cid,))
    assert repo.recover_stale_cycle(cid, 120) == "STOPPED"
    cycle = repo.get_cycle(cid)
    assert cycle["executor_id"] is None and cycle["lease_heartbeat_at"] is None


def test_recover_missing_executor_valid_submitting_to_unknown(repo):
    cid = _cycle(repo); repo.confirm_and_start(cid, "lost", 120)
    tx = repo.get_pending_transactions(cid)[0]
    repo.claim_pending(tx.transaction_id, "lost")
    repo._conn.execute("UPDATE manual_adjust_cycles SET executor_id=NULL,lease_heartbeat_at=NULL WHERE cycle_id=?", (cid,))
    assert repo.recover_stale_cycle(cid, 120) == "REVIEW_REQUIRED"
    assert repo.get_transaction(tx.transaction_id).status.value == "UNKNOWN"
    cycle = repo.get_cycle(cid)
    assert cycle["executor_id"] is None and cycle["lease_heartbeat_at"] is None


def test_recover_selected_stale_cycle_leaves_other_fresh_running(repo):
    stale = _cycle(repo); fresh = _cycle(repo)
    repo.confirm_and_start(stale, "old", 120)
    # Hostile cycle-level corruption creates the second fresh RUNNING cycle.
    repo._conn.execute("UPDATE manual_adjust_cycles SET status='RUNNING',executor_id='fresh',lease_heartbeat_at=datetime('now') WHERE cycle_id=?", (fresh,))
    _make_stale(repo, stale)
    assert repo.recover_stale_cycle(stale, 120) == "STOPPED"
    assert repo.get_cycle(fresh)["status"] == "RUNNING"
    assert repo._conn.execute("SELECT COUNT(*) FROM manual_adjust_cycles WHERE status='RUNNING'").fetchone()[0] == 1


def test_recovery_rejects_transaction_corruption_without_any_mutation(repo):
    cid = _cycle(repo); repo.confirm_and_start(cid, "lost", 120)
    tx = repo.get_pending_transactions(cid)[0]
    repo._conn.execute("UPDATE manual_adjust_transactions SET status='SUBMITTING' WHERE transaction_id=?", (tx.transaction_id,))
    _make_stale(repo, cid)
    before_cycle = repo.get_cycle(cid).copy()
    with pytest.raises(ValueError, match="SUBMITTING requires"):
        repo.recover_stale_cycle(cid, 120)
    assert repo.get_cycle(cid) == before_cycle
    assert repo.get_transaction(tx.transaction_id).status.value == "SUBMITTING"


def test_recovery_rejects_attempt_identity_corruption_without_mutation(repo):
    cid = _cycle(repo); repo.confirm_and_start(cid, "lost", 120)
    tx = repo.get_pending_transactions(cid)[0]
    repo.claim_pending(tx.transaction_id, "lost"); _make_stale(repo, cid)
    repo._conn.execute("UPDATE manual_adjust_transactions SET attempt_count=2 WHERE transaction_id=?", (tx.transaction_id,))
    before = (repo.get_cycle(cid).copy(), repo.get_transaction(tx.transaction_id))
    with pytest.raises(ValueError, match="attempt count mismatch"):
        repo.recover_stale_cycle(cid, 120)
    assert repo.get_cycle(cid) == before[0]
    assert repo.get_transaction(tx.transaction_id) == before[1]
    assert repo.get_cycle(cid)["status"] == "RUNNING"


def test_recovery_does_not_require_execution_enabled_but_rejects_bad_timeout(repo):
    cid = _cycle(repo); repo.confirm_and_start(cid, "lost", 120); _make_stale(repo, cid)
    ctl = ManualAdjustController(repo, NeverPanel(), {"manual_adjust": {
        "execution_enabled": False, "lease_timeout_sec": 120}})
    assert ctl.recover_stale(cid) == "STOPPED"
    cid2 = _cycle(repo); repo.confirm_and_start(cid2, "lost", 120); _make_stale(repo, cid2)
    bad = ManualAdjustController(repo, NeverPanel(), {"manual_adjust": {
        "execution_enabled": False, "lease_timeout_sec": "120"}})
    with pytest.raises(RuntimeError, match="positive integer"):
        bad.recover_stale(cid2)
    assert repo.get_cycle(cid2)["status"] == "RUNNING"


def test_large_snapshot_uses_production_loader_once(repo):
    rows = []
    for i in range(800):
        rows.append(RawManualAdjustRow(i + 2, f" User{i} ", str(i + 1), f"tx-{i}"))
    # Invalid first occurrences still own their normalized key.
    rows.extend([
        RawManualAdjustRow(802, " InvalidOwner ", "zero", "bad-owner"),
        RawManualAdjustRow(803, "invalidowner", "999", "duplicate-owner"),
        RawManualAdjustRow(804, "", "5", "blank"),
        RawManualAdjustRow(805, "negative", "-1", "negative"),
        RawManualAdjustRow(806, "zero", "0", "zero"),
    ])
    for i in range(195):
        rows.append(RawManualAdjustRow(807 + i, f" user{i} ", "999999", f"dup-{i}"))
    assert len(rows) == 1000

    class Sheet:
        spreadsheet_id = "production-sheet-id"
        master_name = "MASTER"
        calls = 0
        def read_manual_adjust_snapshot(self):
            self.calls += 1
            return rows

    sheet = Sheet(); cid = ManualAdjustLoader(sheet, repo).load()
    assert sheet.calls == 1
    summary = repo.get_cycle_summary(cid)
    assert (summary.source_rows, summary.ready, summary.duplicates, summary.invalid) == (1000, 800, 196, 4)
    assert summary.total_adjustment_amount == sum(range(1, 801))
    assert len(repo.get_source_rows(cid)) == 1000
    assert len(repo.get_pending_transactions(cid)) == 800
    cycle = repo.get_cycle(cid)
    assert cycle["spreadsheet_id"] == "production-sheet-id" and cycle["sheet_name"] == "MASTER"
    assert cycle["snapshot_fingerprint"] == snapshot_fingerprint("production-sheet-id", "MASTER", rows)
    duplicate = next(r for r in repo.get_source_rows(cid) if r["source_row"] == 803)
    assert duplicate["classification"] == "DUPLICATE" and duplicate["username_key"] == "invalidowner"
