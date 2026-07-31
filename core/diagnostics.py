"""
Startup diagnostics — BUG-014.

Runs *once* at application launch. Checks the writability / presence of
every runtime path the bot depends on and returns a report the operator
can see (dashboard sidebar, or dumped straight to the console when the
GUI hasn't loaded yet).

Design rules:
    * Never raise.
    * Never terminate the application, no matter what fails.
    * Missing / read-only paths produce a WARNING check-item; the
      application continues.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DiagnosticsReport:
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def summary(self) -> str:
        lines = ["Startup Diagnostics"]
        for c in self.checks:
            tag = "PASS" if c.ok else "WARN"
            suffix = f" — {c.detail}" if c.detail else ""
            lines.append(f"  [{tag}] {c.name}{suffix}")
        return "\n".join(lines)


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


def run_diagnostics(
    *,
    app_dir: Path,
    resource_dir: Path,
    config_path: Path,
    selectors_path: Path,
    credentials_path: Path,
    sqlite_path: Path,
    logs_dir: Path,
    screenshots_dir: Path,
    browser_profile_dir: Path,
    logger_file_handler_ok: Optional[bool] = None,
    logger_file_handler_error: Optional[str] = None,
) -> DiagnosticsReport:
    r = DiagnosticsReport()

    # ---- Config ----
    r.checks.append(
        CheckResult(
            "config/config.json",
            config_path.exists(),
            "" if config_path.exists() else f"missing at {config_path}",
        )
    )
    r.checks.append(
        CheckResult(
            "config/selectors.json",
            selectors_path.exists(),
            "" if selectors_path.exists() else f"missing at {selectors_path}",
        )
    )

    # ---- Credentials ----
    cred_ok = credentials_path.exists() and credentials_path.stat().st_size > 0
    r.checks.append(
        CheckResult(
            "credentials/service_account.json",
            cred_ok,
            ""
            if cred_ok
            else "not found — drop the real key into credentials/ before CONNECT SHEET",
        )
    )

    # ---- SQLite ----
    ok = False
    detail = ""
    try:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(sqlite_path))
        conn.execute("SELECT 1").fetchone()
        conn.close()
        ok = True
    except Exception as exc:
        detail = f"open failed: {exc}"
    r.checks.append(CheckResult(f"SQLite ({sqlite_path.name})", ok, detail))

    # ---- Logs ----
    if logger_file_handler_ok is False:
        r.checks.append(
            CheckResult(
                "Logs (file handler)",
                False,
                (
                    logger_file_handler_error
                    or "file handler unavailable — running with console-only logging"
                ),
            )
        )
    else:
        r.checks.append(
            CheckResult(
                "Logs (file handler)",
                _writable(logs_dir),
                "" if _writable(logs_dir) else f"logs/ not writable at {logs_dir}",
            )
        )

    # ---- Screenshots ----
    r.checks.append(
        CheckResult(
            "Screenshots",
            _writable(screenshots_dir),
            "" if _writable(screenshots_dir) else f"not writable at {screenshots_dir}",
        )
    )

    # ---- Browser profile ----
    r.checks.append(
        CheckResult(
            "Browser profile",
            _writable(browser_profile_dir),
            ""
            if _writable(browser_profile_dir)
            else f"not writable at {browser_profile_dir}",
        )
    )

    # ---- Resource dir (frozen mode only) ----
    if resource_dir != app_dir:
        r.checks.append(
            CheckResult(
                "Bundled Chromium",
                (resource_dir / "pw-browsers").exists(),
                ""
                if (resource_dir / "pw-browsers").exists()
                else "pw-browsers/ missing from bundle — Playwright will fail",
            )
        )

    return r
