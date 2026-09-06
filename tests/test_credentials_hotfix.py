"""PATCH-09.1 credential UX and fail-fast regression tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.credentials import (
    CredentialValidationError, install_service_account_file,
    validate_service_account_file,
)
from core.sheet_service import ConnectionInfo, SheetService


def credential(private_key="TOP-SECRET-PRIVATE-KEY"):
    return {
        "type": "service_account",
        "client_email": "bot@example.invalid",
        "private_key": private_key,
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def config():
    return {
        "panel_url": "", "google_credentials": "old.json", "batch_size": 100,
        "manual_reload_interval_sec": 30, "polling_delay_sec": 2,
        "monitoring_interval_sec": 10, "remark": "BONUS RELOAD AUTO",
        "bonus_rules": {"daily_limit": 10000, "tiers": []},
        "columns": {}, "required_headers": {},
        "sheet_names": {"master": "MASTER", "manual_bonus_reload": "MANUAL"},
    }


def test_valid_service_account_passes(tmp_path):
    path = write_json(tmp_path / "valid.json", credential())
    assert validate_service_account_file(path) == path


@pytest.mark.parametrize("kind", ["missing", "malformed", "ordinary"])
def test_invalid_local_files_fail_without_secrets(tmp_path, kind):
    path = tmp_path / "candidate.json"
    if kind == "malformed":
        path.write_text('{"private_key":"TOP-SECRET-PRIVATE-KEY"', encoding="utf-8")
    elif kind == "ordinary":
        write_json(path, {"private_key": "TOP-SECRET-PRIVATE-KEY", "hello": "world"})
    with pytest.raises(CredentialValidationError) as caught:
        validate_service_account_file(path)
    assert "TOP-SECRET-PRIVATE-KEY" not in str(caught.value)


@pytest.mark.parametrize("field", ["private_key", "client_email", "token_uri"])
def test_required_service_account_fields_are_enforced(tmp_path, field):
    payload = credential()
    payload[field] = ""
    with pytest.raises(CredentialValidationError, match=field):
        validate_service_account_file(write_json(tmp_path / "invalid.json", payload))


def test_install_atomically_replaces_managed_file(tmp_path):
    target = write_json(tmp_path / "managed" / "service.json", credential("OLD"))
    source = write_json(tmp_path / "download.json", credential("NEW"))
    assert install_service_account_file(source, target) == target
    assert json.loads(target.read_text())["private_key"] == "NEW"
    assert not list(target.parent.glob("*.tmp"))


def test_invalid_install_and_copy_failure_preserve_working_target(tmp_path, monkeypatch):
    target = write_json(tmp_path / "managed.json", credential("OLD"))
    invalid = write_json(tmp_path / "invalid.json", {"not": "credentials"})
    with pytest.raises(CredentialValidationError):
        install_service_account_file(invalid, target)
    assert json.loads(target.read_text())["private_key"] == "OLD"

    source = write_json(tmp_path / "valid.json", credential("NEW"))
    monkeypatch.setattr("core.credentials.os.replace", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        install_service_account_file(source, target)
    assert json.loads(target.read_text())["private_key"] == "OLD"


def test_source_equal_destination_is_safe(tmp_path):
    target = write_json(tmp_path / "managed.json", credential())
    before = target.read_bytes()
    assert install_service_account_file(target, target) == target
    assert target.read_bytes() == before


def test_settings_uses_picker_managed_path_and_prepare_then_commit_contract():
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    settings = source[source.index("class SettingsDialog"):source.index("class PreviewDialog")]
    assert "QFileDialog.getOpenFileName" in settings
    assert '"JSON Files (*.json)"' in settings
    assert "self.creds.setReadOnly(True)" in settings
    assert "install_service_account_file(source, self.credentials_path)" in settings
    assert 'candidate["google_credentials"] = str(self.credentials_path)' in settings
    assert settings.index("install_service_account_file") < settings.index("self.config.clear()")


def test_switching_sheet_credentials_clears_all_old_auth_state(tmp_path):
    service = SheetService("old.json", config())
    service._client = object()
    service._spreadsheet = object()
    service._master = object()
    service._manual = object()
    service._spreadsheet_id = "old-id"
    service.set_credentials_path(str(tmp_path / "managed.json"))
    assert service.credentials_path == str(tmp_path / "managed.json")
    assert service._client is service._spreadsheet is service._master is service._manual is None
    assert service._spreadsheet_id == ""


def test_connect_preflight_precedes_retry_and_transient_ladder_is_preserved():
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    handler = source[source.index("    def _on_connect"):source.index("    def _reload_manual_list")]
    assert handler.index("self.sheet.validate_credentials()") < handler.index("retry_with_ladder(")
    assert "except CredentialValidationError" in handler
    assert "info = self.sheet.connect(url)" in handler


def test_old_config_without_hotfix_key_remains_supported(tmp_path):
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    assert 'config.get("google_credentials", "credentials/service_account.json")' in source
