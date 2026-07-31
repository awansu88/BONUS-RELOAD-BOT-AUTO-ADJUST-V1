"""
v1.2.0 Production Hardening — Configuration validation tests.

Category: C-7. The validator must never crash on missing / corrupt
config or credentials — it produces a report with hints instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config_validator import ConfigReport, validate_configuration


VALID_CFG = {
    "google_credentials": "credentials/service_account.json",
    "sqlite_path": "processed.db",
    "panel_url": "https://panel.example.com/deposit",
    "sheet_names": {"master": "MASTER", "manual_bonus_reload": "MANUAL BONUS RELOAD"},
    "columns": {
        "user_id": 2, "sheet_data": 4, "time_stamp": 5,
        "true_amount": 6, "tx_id": 9,
    },
    "required_headers": {
        "user_id": "USER ID", "sheet_data": "SHEET DATA",
        "time_stamp": "TIME STAMP", "true_amount": "TRUE AMOUNT", "tx_id": "TX_ID",
    },
    "bonus_rules": {
        "daily_limit": 10000,
        "tiers": [{"min_deposit": 50000, "bonus": 5000}],
    },
    "batch_size": 100,
    "monitoring_interval_sec": 10,
    "remark": "BONUS RELOAD AUTO",
    "browser": {"user_data_dir": "browser_profile_bonus_reload"},
}


VALID_SEL = {"panel": {"username": "#u"}, "timeouts": {"field_wait_ms": 5000}}


VALID_CRED = {
    "type": "service_account",
    "client_email": "bot@example.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def good_environment(tmp_path):
    cfg_path = tmp_path / "config" / "config.json"
    sel_path = tmp_path / "config" / "selectors.json"
    cred_path = tmp_path / "credentials" / "service_account.json"
    _write(cfg_path, VALID_CFG)
    _write(sel_path, VALID_SEL)
    _write(cred_path, VALID_CRED)
    profile_dir = tmp_path / "browser_profile_bonus_reload"
    return {
        "app_dir": tmp_path,
        "config_path": cfg_path,
        "selectors_path": sel_path,
        "credentials_path": cred_path,
        "sqlite_path": tmp_path / "processed.db",
        "browser_profile_dir": profile_dir,
    }


def test_valid_environment_reports_all_ok(good_environment):
    report = validate_configuration(**good_environment)
    assert isinstance(report, ConfigReport)
    assert report.all_ok, report.summary()
    # Verify every mandated check is present.
    names = {c.name for c in report.checks}
    for expected in (
        "config keys", "selectors sections", "panel_url",
        "bonus_rules", "columns mapping", "required_headers mapping",
        "credentials/service_account.json",
        "SQLite path writable", "Browser profile writable",
    ):
        assert expected in names


def test_missing_config_file_flagged(good_environment):
    good_environment["config_path"].unlink()
    report = validate_configuration(**good_environment)
    assert not report.all_ok
    check = next(c for c in report.checks if c.name == "config/config.json")
    assert not check.ok
    assert "missing" in check.detail


def test_corrupt_config_json_flagged(good_environment):
    good_environment["config_path"].write_text("{not json", encoding="utf-8")
    report = validate_configuration(**good_environment)
    check = next(c for c in report.checks if c.name == "config/config.json")
    assert not check.ok
    assert "parse error" in check.detail


def test_panel_url_scheme_validated(good_environment):
    cfg = dict(VALID_CFG)
    cfg["panel_url"] = "ftp://nope"
    _write(good_environment["config_path"], cfg)
    report = validate_configuration(**good_environment)
    check = next(c for c in report.checks if c.name == "panel_url")
    assert not check.ok
    assert "unsupported scheme" in check.detail


def test_empty_panel_url_gives_hint(good_environment):
    cfg = dict(VALID_CFG)
    cfg["panel_url"] = ""
    _write(good_environment["config_path"], cfg)
    report = validate_configuration(**good_environment)
    check = next(c for c in report.checks if c.name == "panel_url")
    assert not check.ok
    assert "http://" in check.hint


def test_missing_credentials_flagged(good_environment):
    good_environment["credentials_path"].unlink()
    report = validate_configuration(**good_environment)
    check = next(c for c in report.checks if c.name == "credentials/service_account.json")
    assert not check.ok
    assert "not found" in check.detail
    assert "credentials/" in check.hint


def test_credentials_missing_fields_flagged(good_environment):
    _write(good_environment["credentials_path"], {"client_email": "only"})
    report = validate_configuration(**good_environment)
    check = next(c for c in report.checks if c.name == "credentials/service_account.json")
    assert not check.ok
    assert "missing fields" in check.detail


def test_bad_bonus_rules_flagged(good_environment):
    cfg = json.loads(good_environment["config_path"].read_text())
    cfg["bonus_rules"]["daily_limit"] = 0
    _write(good_environment["config_path"], cfg)
    report = validate_configuration(**good_environment)
    rule_check = next(c for c in report.checks if c.name == "bonus_rules")
    assert not rule_check.ok


def test_summary_is_readable(good_environment):
    report = validate_configuration(**good_environment)
    text = report.summary()
    assert "Configuration validation" in text
    assert "[PASS]" in text
