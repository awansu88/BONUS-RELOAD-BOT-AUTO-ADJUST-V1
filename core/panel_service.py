"""
Playwright wrapper for the deposit panel.

Uses the *sync* Playwright API. We deliberately avoid asyncio + threads;
the dashboard drives this via a QTimer that pumps one transaction at a
time.  A single browser is reused across the whole session.

Operator flow:
    1. `open_panel()` launches a persistent Chromium (user data dir) so
       cookies survive between runs, then navigates to the configured panel
       URL.  The operator manually logs in.
    2. When the operator clicks READY on the dashboard, `attach()` grabs
       the current page for automation.
    3. `submit_deposit()` fills the form and clicks submit, then waits for
       the success alert.  It does NOT refresh the page.
    4. `close()` shuts everything down (called on app exit or full reset).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)


@dataclass
class SubmitResult:
    ok: bool
    detail: str = ""


class ManualSubmitOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED_NOT_SUBMITTED = "FAILED_NOT_SUBMITTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ManualSubmitResult:
    outcome: ManualSubmitOutcome
    click_crossed: Optional[bool]
    phase: str
    detail: str = ""
    evidence: str = ""


class PanelService:
    def __init__(self, config: Dict, selectors: Dict) -> None:
        self.config = config
        self.selectors = selectors
        self.panel_url: str = config.get("panel_url", "") or ""
        self.browser_conf = config.get("browser", {})
        self.timeouts = selectors.get("timeouts", {})

        self._pw: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._attached: bool = False

    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.is_alive()

    @property
    def is_attached(self) -> bool:
        return self._attached and self.is_alive()

    def is_alive(self) -> bool:
        """True only if the persistent context and its page are still usable.
        Detects manual (X) close by the operator."""
        if self._context is None or self._page is None:
            return False
        try:
            if self._page.is_closed():
                return False
            # Accessing context.pages raises if the context is dead.
            _ = self._context.pages
            return True
        except Exception:
            return False

    def _dispose(self) -> None:
        """Drop dead references so the next open_panel() starts fresh."""
        self._attached = False
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._pw = None

    # ------------------------------------------------------------------
    def open_panel(self, panel_url: Optional[str] = None) -> None:
        """Launch Chromium (or reuse a live one) and navigate to the panel URL."""
        url = (panel_url or self.panel_url or "").strip()
        if not url:
            raise ValueError("Panel URL is empty. Configure it in Settings.")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("Panel URL must start with http:// or https://")

        # If a previous context died (operator closed the window), drop it.
        if self._context is not None and not self.is_alive():
            self._dispose()

        if self._context is None:
            self._pw = sync_playwright().start()
            user_dir = Path(self.browser_conf.get("user_data_dir", "browser_profile"))
            user_dir.mkdir(parents=True, exist_ok=True)

            viewport = self.browser_conf.get("viewport", {"width": 1366, "height": 768})

            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(user_dir),
                headless=bool(self.browser_conf.get("headless", False)),
                viewport=viewport,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        elif self._page is None or self._page.is_closed():
            # Context alive but last page gone — spin up a new one.
            self._page = self._context.new_page()

        assert self._page is not None
        self._page.goto(url, wait_until="domcontentloaded")
        self._attached = False

    # ------------------------------------------------------------------
    def attach(self) -> None:
        """Bind to the current page after operator logs in manually."""
        if not self.is_open:
            raise RuntimeError("Panel is not open")
        # If operator opened new tabs, prefer the frontmost visible page.
        pages = [p for p in self._context.pages if not p.is_closed()]  # type: ignore
        if pages:
            self._page = pages[-1]
        self._attached = True

    # ------------------------------------------------------------------
    def submit_deposit(self, user_id: str, bonus: int, remark: str) -> SubmitResult:
        if not self.is_alive():
            # Operator likely closed the browser window mid-run.
            self._dispose()
            return SubmitResult(False, "browser closed")
        if not self.is_attached or not self._page:
            return SubmitResult(False, "panel not attached")

        panel = self.selectors["panel"]
        defaults = self.selectors.get("defaults", {})
        success_text = self.selectors.get("success_text", "")
        field_wait = int(self.timeouts.get("field_wait_ms", 8000))
        success_wait = int(self.timeouts.get("success_wait_ms", 15000))

        page = self._page
        try:
            # --- USERNAME
            page.wait_for_selector(panel["username"], timeout=field_wait)
            self._fill(page, panel["username"], str(user_id))

            # --- AMOUNT (bonus)
            self._fill(page, panel["amount"], str(int(bonus)))

            # --- REMARK
            self._fill(page, panel["remark"], remark)

            # --- OPTIONAL dropdowns (leave as-is if already selected)
            self._maybe_select(page, panel.get("payment_dropdown"), defaults.get("payment"))
            self._maybe_select(page, panel.get("currency_dropdown"), defaults.get("currency"))

            # --- SUBMIT
            page.click(panel["submit"])

            # --- WAIT SUCCESS
            selector = panel["success_alert"]
            page.wait_for_selector(selector, timeout=success_wait, state="visible")

            if success_text:
                try:
                    text = page.locator(selector).first.inner_text(timeout=1000)
                    if success_text.lower() not in (text or "").lower():
                        return SubmitResult(False, f"unexpected alert: {text!r}")
                except PWTimeout:
                    pass

            return SubmitResult(True)
        except PWTimeout as exc:
            return SubmitResult(False, f"timeout: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            return SubmitResult(False, f"error: {exc}")

    def submit_adjustment(self, user_id: str, amount: int, remark: str,
                          phase_hook=None) -> ManualSubmitResult:
        """Submit an exact Manual amount with conservative click classification.

        ``phase_hook`` is called before each observable phase.  In particular,
        SUBMIT_CLICK_BOUNDARY must return successfully before the remote click.
        This method deliberately does not use Validator or AUTO bonus logic.
        """
        if not self.is_alive():
            return ManualSubmitResult(ManualSubmitOutcome.FAILED_NOT_SUBMITTED,
                                      False, "PANEL_UNAVAILABLE", "browser closed")
        if not self.is_attached or not self._page:
            return ManualSubmitResult(ManualSubmitOutcome.FAILED_NOT_SUBMITTED,
                                      False, "PANEL_UNAVAILABLE", "panel not attached")
        page = self._page
        panel = self.selectors["panel"]
        defaults = self.selectors.get("defaults", {})
        success_text = self.selectors.get("success_text", "")
        field_wait = int(self.timeouts.get("field_wait_ms", 8000))
        success_wait = int(self.timeouts.get("success_wait_ms", 15000))

        def phase(name: str) -> None:
            if phase_hook is not None:
                phase_hook(name)

        current = "FORM_STARTED"
        try:
            phase(current)
            page.wait_for_selector(panel["username"], timeout=field_wait)
            self._fill(page, panel["username"], str(user_id))
            current = "USERNAME_FILLED"; phase(current)
            self._fill(page, panel["amount"], str(int(amount)))
            current = "AMOUNT_FILLED"; phase(current)
            self._fill(page, panel["remark"], str(remark))
            current = "REMARK_FILLED"; phase(current)
            self._maybe_select(page, panel.get("payment_dropdown"), defaults.get("payment"))
            self._maybe_select(page, panel.get("currency_dropdown"), defaults.get("currency"))
            current = "READY_TO_CLICK"; phase(current)
            current = "SUBMIT_CLICK_BOUNDARY"; phase(current)
        except Exception as exc:
            return ManualSubmitResult(ManualSubmitOutcome.FAILED_NOT_SUBMITTED,
                                      False, current, str(exc))

        try:
            page.click(panel["submit"])
        except Exception as exc:
            return ManualSubmitResult(ManualSubmitOutcome.UNKNOWN, None,
                                      "CLICK_UNCERTAIN", str(exc))

        try:
            current = "CLICK_RETURNED"; phase(current)
            current = "WAITING_RESULT"; phase(current)
            selector = panel["success_alert"]
            page.wait_for_selector(selector, timeout=success_wait, state="visible")
            if success_text:
                text = page.locator(selector).first.inner_text(timeout=1000)
                if success_text.lower() not in (text or "").lower():
                    return ManualSubmitResult(ManualSubmitOutcome.UNKNOWN, True,
                                              "AMBIGUOUS_RESPONSE",
                                              f"unexpected alert: {text!r}", str(text or ""))
            current = "SUCCESS_OBSERVED"; phase(current)
            return ManualSubmitResult(ManualSubmitOutcome.SUCCESS, True, current,
                                      evidence="success alert observed")
        except Exception as exc:
            return ManualSubmitResult(ManualSubmitOutcome.UNKNOWN, True, current, str(exc))

    # ------------------------------------------------------------------
    def screenshot(self, path: str) -> None:
        if self._page and not self._page.is_closed():
            try:
                self._page.screenshot(path=path, full_page=False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def close(self) -> None:
        self._dispose()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _fill(page: Page, selector: str, value: str) -> None:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=8000)
        loc.click()
        # Clear any pre-existing value before typing.
        try:
            loc.fill("")
        except Exception:
            pass
        loc.fill(value)

    @staticmethod
    def _maybe_select(page: Page, selector: Optional[str], value: Optional[str]) -> None:
        if not selector or not value:
            return
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                return
            current = loc.evaluate("el => el.value || el.textContent || ''")
            if current and value.lower() in str(current).lower():
                return
            try:
                loc.select_option(label=value)
            except Exception:
                loc.select_option(value=value)
        except Exception:
            # Dropdown not present or not a <select>; ignore silently
            return
