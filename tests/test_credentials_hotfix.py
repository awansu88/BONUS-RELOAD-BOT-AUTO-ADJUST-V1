"""PATCH-09.1 credential UX and fail-fast regression tests."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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


def production_sheet_config():
    return {
        "columns": {"user_id": 2, "sheet_data": 4, "time_stamp": 5,
                    "true_amount": 6, "tx_id": 9},
        "required_headers": {"user_id": "USER ID", "sheet_data": "KEY_ID",
                             "time_stamp": "TIME STAMP", "true_amount": "TRUE AMOUNT",
                             "tx_id": "TX_ID"},
        "sheet_names": {"master": "MASTER", "manual_bonus_reload": "MANUAL"},
    }


class Worksheet:
    def __init__(self, title, headers=()): self.title, self.headers = title, list(headers)
    def row_values(self, _): return list(self.headers)


class Spreadsheet:
    title = "PRODUCTION"
    def __init__(self, tabs, headers):
        self.tabs = {name: Worksheet(name, headers if name == "MASTER" else ())
                     for name in tabs}
    def worksheets(self): return list(self.tabs.values())
    def worksheet(self, name): return self.tabs[name]


def connect_result(monkeypatch, *, tabs=("MASTER", "MANUAL"), column_d="KEY_ID"):
    headers = ["", "USER ID", "", column_d, "TIME STAMP",
               "TRUE AMOUNT", "", "", "TX_ID"]
    service = SheetService("unused", production_sheet_config())
    book = Spreadsheet(tabs, headers)
    monkeypatch.setattr(
        service, "_authorize", lambda: SimpleNamespace(open_by_key=lambda _: book)
    )
    return service.connect("x" * 20)


def test_default_config_uses_production_key_id_contract():
    default = json.loads(Path("config/config.json").read_text(encoding="utf-8"))
    assert default["columns"]["sheet_data"] == 4
    assert default["required_headers"]["sheet_data"] == "KEY_ID"


def test_production_contract_connects_and_source_contract_errors_are_not_retryable(monkeypatch):
    assert connect_result(monkeypatch).ok
    assert connect_result(monkeypatch, column_d="SHEET DATA").retryable is False
    assert connect_result(monkeypatch, column_d="WRONG").retryable is False
    assert connect_result(monkeypatch, column_d="").retryable is False
    assert connect_result(monkeypatch, tabs=("MANUAL",)).retryable is False
    assert connect_result(monkeypatch, tabs=("MASTER",)).retryable is False
    assert SheetService("unused", production_sheet_config()).connect("bad-url").retryable is False


def test_unclassified_remote_failure_remains_retryable(monkeypatch):
    service = SheetService("unused", production_sheet_config())
    monkeypatch.setattr(
        service, "_authorize",
        lambda: (_ for _ in ()).throw(ConnectionError("temporary network outage")),
    )
    info = service.connect("x" * 20)
    assert not info.ok and info.retryable


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
    assert "install_service_account_file(" in settings
    assert "self._selected_credentials, self.credentials_path" in settings
    assert 'candidate["google_credentials"] = str(self.credentials_path)' in settings
    assert settings.index("install_service_account_file") < settings.index("self.config.clear()")


def test_noncredential_save_does_not_install_and_reports_unchanged():
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    settings = source[source.index("class SettingsDialog"):source.index("class PreviewDialog")]
    assert "self.credentials_changed = False" in settings
    assert "if self._selected_credentials is not None:" in settings
    assert "self.credentials_changed = self._selected_credentials is not None" in settings
    install_guard = settings.index("if self._selected_credentials is not None:")
    install = settings.index("install_service_account_file(", install_guard)
    config_write = settings.index("self._write_config_atomic(candidate)", install)
    assert install_guard < install < config_write


def test_dashboard_resets_sheet_and_manual_ui_only_for_real_replacement():
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    handler = source[source.index("    def _open_settings"):source.index("    def _open_database")]
    change_guard = handler.index("if dlg.credentials_changed:")
    reset = handler.index("self.sheet.set_credentials_path", change_guard)
    clear_queue = handler.index("self.queue = None", change_guard)
    manual_sync = handler.index("self.manual_view.set_sheet_connected(False)", change_guard)
    unchanged = handler.index('self.logger.info("Settings updated")', change_guard)
    assert change_guard < reset < clear_queue < manual_sync < unchanged


@pytest.mark.parametrize("state", ["running", "monitoring", "recovering", "stopping"])
def test_all_active_auto_states_block_credential_replacement(state):
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    handler = source[source.index("    def _open_settings"):source.index("    def _open_database")]
    assert f'"{state}"' in handler
    assert "and not self._manual_execution_blocks_auto()" in handler
    assert "credential_replacement_allowed=credential_replacement_allowed" in handler


def test_dialog_disables_browse_and_defensively_rejects_replacement():
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    settings = source[source.index("class SettingsDialog"):source.index("class PreviewDialog")]
    assert "self.credentials_browse.setEnabled(credential_replacement_allowed)" in settings
    assert settings.count("if not self.credential_replacement_allowed:") >= 2
    assert "Stop AUTO / Manual execution before replacing Google credentials." in settings


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
    assert "if not info.ok and info.retryable:" in handler
    assert handler.index("if not info.ok:") > handler.index("retry_with_ladder(")


def test_old_config_without_hotfix_key_remains_supported(tmp_path):
    source = Path("ui/dashboard.py").read_text(encoding="utf-8")
    assert 'config.get("google_credentials", "credentials/service_account.json")' in source
