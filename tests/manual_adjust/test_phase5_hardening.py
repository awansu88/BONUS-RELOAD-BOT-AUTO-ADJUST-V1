from __future__ import annotations

import json
import sqlite3

import pytest

from core.config_validator import validate_configuration
from core.manual_adjust_controller import ManualAdjustController
from core.manual_adjust_models import ClassifiedSourceRow, SourceClassification
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
        "execution_enabled": True, "remark": "MANUAL", "lease_timeout_sec": 120}})
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
