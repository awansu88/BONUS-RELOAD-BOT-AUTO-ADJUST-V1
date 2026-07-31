"""
Production hardening v1.2.0 — Configuration validation (C-7).

Category D (Diagnostics). Runs at startup + on demand from the
Maintenance Center. Validates:

    * config.json — required keys, sane types, panel URL scheme
    * selectors.json — required sections
    * credentials/service_account.json — exists, is valid JSON,
      has the fields Google's service-account keys always ship with
    * SQLite path — parent directory exists / is writable
    * Browser profile directory — writable
    * Google configuration — bonus_rules structure, sheet_names,
      required column indices

Never crashes. Every check returns a `ConfigCheck` with a
human-readable explanation for the operator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class ConfigCheck:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""


@dataclass
class ConfigReport:
    checks: List[ConfigCheck] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", hint: str = "") -> None:
        self.checks.append(ConfigCheck(name, ok, detail, hint))

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def summary(self) -> str:
        lines = ["Configuration validation"]
        for c in self.checks:
            tag = "PASS" if c.ok else "FAIL"
            extra = f" — {c.detail}" if c.detail else ""
            hint = f"  hint: {c.hint}" if c.hint else ""
            lines.append(f"  [{tag}] {c.name}{extra}")
            if hint:
                lines.append(hint)
        return "\n".join(lines)


REQUIRED_CONFIG_KEYS = (
    "google_credentials",
    "sqlite_path",
    "panel_url",
    "sheet_names",
    "columns",
    "required_headers",
    "bonus_rules",
    "batch_size",
    "monitoring_interval_sec",
    "remark",
    "browser",
)

REQUIRED_SELECTOR_SECTIONS = ("panel", "timeouts")


def _readable_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    probe = path / ".write-test.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def validate_configuration(
    *,
    app_dir: Path,
    config_path: Path,
    selectors_path: Path,
    credentials_path: Path,
    sqlite_path: Path,
    browser_profile_dir: Path,
) -> ConfigReport:
    r = ConfigReport()

    # ---- config.json ----
    cfg: Dict[str, Any] = {}
    if not config_path.exists():
        r.add(
            "config/config.json",
            False,
            f"missing at {config_path}",
            "reinstall or copy the seed from _internal/config/",
        )
    else:
        try:
            cfg = _readable_json(config_path) or {}
            missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
            if missing:
                r.add(
                    "config keys",
                    False,
                    f"missing: {', '.join(missing)}",
                    "restore missing keys from the seed config",
                )
            else:
                r.add("config keys", True, f"{len(REQUIRED_CONFIG_KEYS)} keys present")
        except Exception as exc:
            r.add(
                "config/config.json",
                False,
                f"parse error: {exc}",
                "the file is not valid JSON — restore a backup",
            )

    # ---- selectors.json ----
    if not selectors_path.exists():
        r.add(
            "config/selectors.json",
            False,
            f"missing at {selectors_path}",
            "restore from the seed selectors",
        )
    else:
        try:
            sel = _readable_json(selectors_path) or {}
            missing = [s for s in REQUIRED_SELECTOR_SECTIONS if s not in sel]
            if missing:
                r.add(
                    "selectors sections",
                    False,
                    f"missing: {', '.join(missing)}",
                    "restore from the seed selectors",
                )
            else:
                r.add("selectors sections", True)
        except Exception as exc:
            r.add(
                "config/selectors.json",
                False,
                f"parse error: {exc}",
                "the file is not valid JSON — restore a backup",
            )

    # ---- panel URL ----
    if cfg:
        panel_url = str(cfg.get("panel_url") or "").strip()
        if not panel_url:
            r.add(
                "panel_url",
                False,
                "empty",
                "open Settings and paste the panel URL (http:// or https://)",
            )
        elif not (panel_url.startswith("http://") or panel_url.startswith("https://")):
            r.add(
                "panel_url",
                False,
                f"unsupported scheme: {panel_url!r}",
                "the URL must start with http:// or https://",
            )
        else:
            r.add("panel_url", True, panel_url)

    # ---- bonus rules ----
    if cfg:
        rules = cfg.get("bonus_rules") or {}
        ok_rules = True
        detail = ""
        try:
            daily = int(rules.get("daily_limit", 0))
            if daily <= 0:
                ok_rules = False
                detail = "daily_limit must be > 0"
            tiers = rules.get("tiers") or []
            if not tiers:
                ok_rules = False
                detail = "no tiers configured"
            for t in tiers:
                if int(t.get("min_deposit", 0)) <= 0 or int(t.get("bonus", 0)) <= 0:
                    ok_rules = False
                    detail = "tier must have positive min_deposit + bonus"
        except Exception as exc:
            ok_rules = False
            detail = f"bad structure: {exc}"
        r.add(
            "bonus_rules",
            ok_rules,
            detail,
            "" if ok_rules else "restore bonus_rules block from a backup config",
        )

        # ---- required columns / headers ----
        cols = cfg.get("columns") or {}
        heads = cfg.get("required_headers") or {}
        missing_cols = [
            k for k in ("user_id", "sheet_data", "time_stamp", "true_amount", "tx_id")
            if k not in cols
        ]
        missing_heads = [
            k for k in ("user_id", "sheet_data", "time_stamp", "true_amount", "tx_id")
            if k not in heads
        ]
        r.add(
            "columns mapping",
            not missing_cols,
            "" if not missing_cols else f"missing: {', '.join(missing_cols)}",
            "the columns block maps each header to its 1-based column index",
        )
        r.add(
            "required_headers mapping",
            not missing_heads,
            "" if not missing_heads else f"missing: {', '.join(missing_heads)}",
            "the required_headers block declares the exact MASTER headers",
        )

    # ---- credentials ----
    cred_ok = credentials_path.exists() and credentials_path.stat().st_size > 0
    if cred_ok:
        try:
            data = _readable_json(credentials_path)
            need = ("client_email", "private_key", "token_uri")
            missing = [k for k in need if k not in data]
            if missing:
                r.add(
                    "credentials/service_account.json",
                    False,
                    f"missing fields: {', '.join(missing)}",
                    "download a fresh service-account JSON from Google Cloud Console",
                )
            else:
                r.add(
                    "credentials/service_account.json",
                    True,
                    f"client_email={data.get('client_email','?')}",
                )
        except Exception as exc:
            r.add(
                "credentials/service_account.json",
                False,
                f"parse error: {exc}",
                "the credentials file is not valid JSON",
            )
    else:
        r.add(
            "credentials/service_account.json",
            False,
            "not found",
            "drop the real service-account JSON into credentials/ before CONNECT SHEET",
        )

    # ---- database directory writable ----
    r.add(
        "SQLite path writable",
        _writable(sqlite_path.parent),
        "" if _writable(sqlite_path.parent) else f"not writable: {sqlite_path.parent}",
        "check folder permissions",
    )

    # ---- browser profile writable ----
    r.add(
        "Browser profile writable",
        _writable(browser_profile_dir),
        ""
        if _writable(browser_profile_dir)
        else f"not writable: {browser_profile_dir}",
        "delete the folder if it was created read-only and try again",
    )

    return r
