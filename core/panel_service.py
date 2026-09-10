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

import json
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)
from .logger import AppLogger
from .performance_telemetry import get_telemetry, timed


def _sanitize_url(value: object) -> str:
    """Return only a URL's scheme, authority, and path for diagnostics."""
    try:
        parts = urlsplit(str(value or ""))
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        try:
            authority = f"{host}:{parts.port}" if parts.port is not None else host
        except ValueError:
            authority = host
        return urlunsplit((parts.scheme, authority, parts.path, "", ""))
    except Exception:
        return "<unavailable>"


def _evidence_value(value: object) -> str:
    """Keep evidence single-line and compact without exposing object reprs."""
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "unavailable"
    return str(value).replace("\r", " ").replace("\n", " ")[:200]


def _emit_auto_evidence(fields: Dict[str, object], *, warning: bool = False) -> None:
    """Best-effort persistent-only output; logging can never affect a submit."""
    try:
        message = "[AUTO_EVIDENCE] " + " ".join(
            f"{key}={_evidence_value(value)}" for key, value in fields.items()
        )
        logger = AppLogger.get()
        method = logger.diagnostic_warn if warning else logger.diagnostic
        method(message)
    except Exception:
        pass


def _page_evidence(page: Page) -> Dict[str, object]:
    """Collect only Playwright state documented as locally available.

    Renderer-dependent calls (evaluate/title/locators) are deliberately not
    made: evidence must never compete with mandatory result verification.
    """
    fields: Dict[str, object] = {}
    try:
        fields["page_url"] = _sanitize_url(page.url)
    except Exception as exc:
        fields["page_url_error"] = type(exc).__name__
    try:
        fields["page_closed"] = bool(page.is_closed())
    except Exception as exc:
        fields["page_closed_error"] = type(exc).__name__
    fields["ready_state"] = "not_probed"
    fields["title"] = "not_probed"
    return fields


def _collect_navigation_evidence(response) -> Dict[str, object]:
    """Collect non-sensitive main-resource metadata, entirely fail-open."""
    fields: Dict[str, object] = {}
    for name in ("status", "ok"):
        try:
            fields[name] = getattr(response, name)
        except Exception as exc:
            fields[f"{name}_error"] = type(exc).__name__
    try:
        fields["final_url"] = _sanitize_url(response.url)
    except Exception as exc:
        fields["final_url_error"] = type(exc).__name__

    request = None
    try:
        request = response.request
        fields["method"] = request.method
        fields["resource_type"] = request.resource_type
    except Exception as exc:
        fields["request_error"] = type(exc).__name__

    redirects = 0
    original = request
    try:
        while original is not None and original.redirected_from is not None:
            redirects += 1
            original = original.redirected_from
        fields["redirects"] = redirects
        if original is not None:
            fields["original_url"] = _sanitize_url(original.url)
    except Exception as exc:
        fields["redirect_error"] = type(exc).__name__

    try:
        fields["content_type"] = response.header_value("content-type")
    except Exception as exc:
        fields["content_type_error"] = type(exc).__name__

    # Sync Playwright exposes no bounded response-body read.  Never call
    # response.body()/text() here: either may wait on an unhealthy renderer or
    # network stream and steal time from (or extend) financial verification.
    fields["body_available"] = False
    fields["body_len"] = "unavailable"
    fields["success_text_present"] = "unavailable"
    fields["body_error"] = "SKIPPED_UNBOUNDED"
    fields["body_read_ms"] = 0.0
    return fields


@dataclass
class SubmitResult:
    ok: bool
    detail: str = ""


class AutoSubmitOutcome(str, Enum):
    """The only three remotely meaningful outcomes for an AUTO attempt."""

    SUCCESS = "SUCCESS"
    FAILED_NOT_SUBMITTED = "FAILED_NOT_SUBMITTED"
    UNKNOWN_AFTER_SUBMIT = "UNKNOWN_AFTER_SUBMIT"


@dataclass(frozen=True)
class AutoSubmitResult:
    outcome: AutoSubmitOutcome
    # False means only that crossing was not positively proven.  In the
    # CLICK_UNCERTAIN phase it is emphatically not proof that no click occurred.
    click_crossed: bool
    phase: str
    detail: str = ""
    evidence: str = ""
    accounting_error: bool = False


class _AutoPhasePersistenceError(RuntimeError):
    pass


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
        # PATCH-10 verification state lives in Python, not in a renderer.  The
        # init-script callback can therefore latch a short-lived alert even if
        # that document is immediately replaced.
        self._auto_attempt: Optional[Dict[str, object]] = None
        self._document_generation: Dict[int, int] = {}
        self._early_verifier_installed = False

    def _install_early_submit_verifier(self) -> None:
        """Install the new-document watcher once on the persistent context."""
        if self._early_verifier_installed or self._context is None:
            return
        selector = self.selectors["panel"]["success_alert"]
        phrase = self.selectors.get("success_text", "")

        def navigated(frame) -> None:
            try:
                page = frame.page
                if frame == page.main_frame:
                    key = id(page)
                    self._document_generation[key] = (
                        self._document_generation.get(key, 0) + 1
                    )
            except Exception:
                pass

        def captured(source, payload) -> None:
            try:
                attempt = self._auto_attempt
                page = source.get("page") if isinstance(source, dict) else source.page
                frame = source.get("frame") if isinstance(source, dict) else source.frame
                if not attempt or page is not attempt["page"] or frame != page.main_frame:
                    return
                if self._document_generation.get(id(page), 0) <= attempt["generation"]:
                    return
                if urlsplit(str(payload.get("url", ""))).path != "/deposit/manual":
                    return
                text = str(payload.get("text", ""))
                if phrase and phrase.casefold() not in text.casefold():
                    return
                attempt["capture"] = {
                    "text": text, "at": time.perf_counter(), "source": "context_init_observer"
                }
            except Exception:
                # Verification callbacks are best effort until the navigation
                # response and latched payload are jointly validated below.
                pass

        self._context.expose_binding("__patch10Capture", captured)
        config_json = json.dumps({"selector": selector, "phrase": phrase})
        script = """(() => {
          const {selector, phrase} = CONFIG;
          const report = (node) => {
            if (!(node instanceof Element)) return;
            const matches = node.matches(selector) ? [node] : node.querySelectorAll(selector);
            for (const el of matches) {
              const text = el.textContent || '';
              if (!phrase || text.toLocaleLowerCase().includes(phrase.toLocaleLowerCase()))
                window.__patch10Capture({url: location.href, text});
            }
          };
          const start = () => {
            if (document.documentElement) report(document.documentElement);
            new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(report)))
              .observe(document, {subtree: true, childList: true});
          };
          start();
        })();""".replace("CONFIG", config_json)
        self._context.add_init_script(script=script)
        for page in self._context.pages:
            self._document_generation.setdefault(id(page), 0)
            page.on("framenavigated", navigated)
        def page_created(page) -> None:
            self._document_generation.setdefault(id(page), 0)
            page.on("framenavigated", navigated)

        self._context.on("page", page_created)
        self._early_verifier_installed = True

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

    def _context_is_alive(self) -> bool:
        if self._context is None:
            return False
        try:
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
        self._auto_attempt = None
        self._document_generation.clear()
        self._early_verifier_installed = False

    # ------------------------------------------------------------------
    def open_panel(self, panel_url: Optional[str] = None) -> None:
        """Launch Chromium (or reuse a live one) and navigate to the panel URL."""
        url = (panel_url or self.panel_url or "").strip()
        if not url:
            raise ValueError("Panel URL is empty. Configure it in Settings.")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("Panel URL must start with http:// or https://")

        # If a previous context died (operator closed the window), drop it.
        if self._context is not None and not self._context_is_alive():
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
            self._install_early_submit_verifier()
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

    def probe_auto_panel_ready(self) -> None:
        """Verify AUTO can use the panel without mutating the form.

        This deliberately performs only a bounded selector visibility check.
        In particular it does not focus, fill, inspect values, or submit any
        field.  A login page therefore remains available to the operator but
        is not reported as a recovered AUTO panel.
        """
        if not self.is_alive() or not self._page:
            raise RuntimeError("panel page is not usable")
        username = self.selectors["panel"]["username"]
        field_wait = int(self.timeouts.get("field_wait_ms", 8000))
        self._page.wait_for_selector(username, timeout=field_wait, state="visible")

    def recover_auto_panel(self) -> None:
        """Restore and verify AUTO browser infrastructure only.

        ``open_panel`` owns defensive disposal/relaunch and always uses the
        configured persistent profile.  Attaching and readiness probing are
        intentionally separate from every financial form helper.
        """
        # A previous attachment is not evidence that this recovery attempt is
        # usable.  Invalidate it before navigation so failures in open/goto or
        # attach cannot leave START enabled on stale readiness.
        self._attached = False
        try:
            self.open_panel(self.panel_url)
            self.attach()
            self.probe_auto_panel_ready()
        except Exception:
            self._attached = False
            raise

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

    @timed("panel.submit.total", context=lambda self, user_id, bonus, remark, **kw: {
        "user_id": user_id})
    def submit_deposit_classified(
        self, user_id: str, bonus: int, remark: str,
        phase_hook: Optional[Callable[[str], None]] = None,
    ) -> AutoSubmitResult:
        """Submit an AUTO award with a durable, conservative click boundary.

        Every hook is synchronous: returning from ``CLICK_RETURNED`` is the
        durable acknowledgement that the database recorded the returned click.
        A hook failure before entering ``page.click`` is safely pre-click; a
        hook failure after it returns is an accounting error with an ambiguous
        remote outcome.
        """
        method_started = time.perf_counter()

        def result(outcome, phase, detail="", evidence="", *, crossed=False,
                   accounting_error=False):
            self._auto_attempt = None
            return AutoSubmitResult(outcome, crossed, phase, str(detail),
                                    str(evidence), accounting_error)

        if not self.is_alive():
            self._dispose()
            return result(AutoSubmitOutcome.FAILED_NOT_SUBMITTED,
                          "FAILED_PRE_CLICK", "browser closed")
        if not self.is_attached or not self._page:
            return result(AutoSubmitOutcome.FAILED_NOT_SUBMITTED,
                          "FAILED_PRE_CLICK", "panel not attached")

        page = self._page
        panel = self.selectors["panel"]
        defaults = self.selectors.get("defaults", {})
        success_text = self.selectors.get("success_text", "")
        field_wait = int(self.timeouts.get("field_wait_ms", 8000))
        success_wait = int(self.timeouts.get("success_wait_ms", 15000))

        def phase(name: str) -> None:
            if phase_hook is not None:
                try:
                    phase_hook(name)
                except Exception as exc:
                    raise _AutoPhasePersistenceError(str(exc)) from exc

        telemetry = get_telemetry()
        phase_started = time.perf_counter()
        current = "FORM_STARTED"
        try:
            phase(current)
            page.wait_for_selector(panel["username"], timeout=field_wait)
            telemetry.record_since("panel.fields_ready", phase_started)
            phase_started = time.perf_counter()
            self._fill(page, panel["username"], str(user_id))
            current = "USERNAME_FILLED"; phase(current)
            self._fill(page, panel["amount"], str(int(bonus)))
            current = "AMOUNT_FILLED"; phase(current)
            self._fill(page, panel["remark"], remark)
            current = "REMARK_FILLED"; phase(current)
            self._maybe_select(page, panel.get("payment_dropdown"), defaults.get("payment"))
            self._maybe_select(page, panel.get("currency_dropdown"), defaults.get("currency"))
            telemetry.record_since("panel.form_fill", phase_started)
            current = "READY_TO_CLICK"; phase(current)

            current = "SUBMIT_CLICK_BOUNDARY"; phase(current)
        except Exception as exc:
            # No call to page.click has been entered.
            try:
                phase("FAILED_PRE_CLICK")
            except Exception:
                pass
            return result(AutoSubmitOutcome.FAILED_NOT_SUBMITTED,
                          "FAILED_PRE_CLICK", exc, current,
                          accounting_error=isinstance(exc, _AutoPhasePersistenceError))

        phase_started = time.perf_counter()
        result_deadline = phase_started + (success_wait / 1000.0)
        submit_started = phase_started
        click_returned_at: Optional[float] = None
        click_returned = False
        attempt = {
            "id": uuid.uuid4().hex, "page": page,
            "generation": self._document_generation.get(id(page), 0),
            "capture": None,
        }
        self._auto_attempt = attempt
        try:
            # Playwright 1.55's expect_navigation waiter is installed when the
            # context manager is entered, before the click can trigger a fast
            # same-URL document reload.  Page navigation is main-frame only;
            # iframe navigations do not satisfy this expectation.
            with page.expect_navigation(
                wait_until="commit",
                timeout=max(1, int((result_deadline - time.perf_counter()) * 1000)),
            ) as navigation_info:
                page.click(
                    panel["submit"],
                    timeout=max(1, int((result_deadline - time.perf_counter()) * 1000)),
                )
                click_returned = True
                click_returned_at = time.perf_counter()
                telemetry.record_since("panel.submit_click", phase_started)
                current = "CLICK_RETURNED"; phase(current)
                current = "WAITING_NAVIGATION"; phase(current)
            navigation_completed_at = time.perf_counter()
            # Playwright also reports same-document History API/hash changes as
            # navigation with a null response.  Require a main-resource
            # response as proof that a new main-frame document was committed;
            # its HTTP status is deliberately irrelevant to classification.
            navigation_response = navigation_info.value
            if navigation_response is None:
                _emit_auto_evidence({
                    "user": user_id, "phase": "NAVIGATION",
                    "response": "NONE",
                    "click_entered_ms": round(
                        (submit_started - method_started) * 1000, 1),
                    "click_returned_ms": round(
                        ((click_returned_at or submit_started) - method_started) * 1000, 1),
                    "navigation_completed_ms": round(
                        (navigation_completed_at - method_started) * 1000, 1),
                    **_page_evidence(page),
                }, warning=True)
                raise RuntimeError("same-document navigation is not a fresh document")
            if urlsplit(str(navigation_response.url)).path != "/deposit/manual":
                raise RuntimeError("fresh navigation did not commit /deposit/manual")
        except Exception as exc:
            if click_returned:
                classified = result(
                    AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT, current, exc,
                    "click returned but fresh submit navigation was not proven",
                    crossed=True,
                    accounting_error=isinstance(exc, _AutoPhasePersistenceError),
                )
                self._auto_attempt = None
                return classified
            accounting_error = False
            try:
                phase("CLICK_UNCERTAIN")
            except Exception as phase_exc:
                accounting_error = True
                exc = RuntimeError(f"{exc}; CLICK_UNCERTAIN persistence failed: {phase_exc}")
            classified = result(AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT,
                          "CLICK_UNCERTAIN", exc,
                          "click call did not return; dispatch may have occurred",
                          accounting_error=accounting_error)
            self._auto_attempt = None
            return classified

        phase_started = time.perf_counter()
        try:
            current = "NAVIGATION_OBSERVED"; phase(current)
        except Exception as exc:
            self._auto_attempt = None
            return result(AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT, current, exc,
                          "fresh navigation observed but durable phase evidence failed",
                          crossed=True, accounting_error=True)

        phase_started = time.perf_counter()
        selector = panel["success_alert"]
        remaining_ms = max(1, int((result_deadline - time.perf_counter()) * 1000))
        selector_started = time.perf_counter()
        selector_result = "ERROR"
        selector_error: Optional[str] = None
        try:
            current = "WAITING_FRESH_RESULT"; phase(current)
            # No diagnostic Playwright or logging calls are permitted before
            # this mandatory wait.  It receives the same deadline calculation
            # as PR #19, apart from negligible local Python assignments.
            remaining_ms = max(1, int((result_deadline - time.perf_counter()) * 1000))
            selector_started = time.perf_counter()
            capture = attempt.get("capture")
            if capture is None:
                page.wait_for_selector(selector, timeout=remaining_ms, state="attached")
                remaining_ms = max(1, int((result_deadline - time.perf_counter()) * 1000))
                text = page.locator(selector).first.text_content(timeout=remaining_ms) or ""
                capture = {"text": text, "at": time.perf_counter(), "source": "fresh_dom_attached"}
            selector_wait_ms = round((time.perf_counter() - selector_started) * 1000, 1)
            selector_result = "ATTACHED"
            text = str(capture["text"])
            evidence = f"fresh navigation observed; success alert attached: {text}"
            if success_text:
                if success_text.lower() not in evidence.lower():
                    current = "AMBIGUOUS_RESPONSE"
                    phase(current)
                    classified_result = result(
                        AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT, current,
                        f"unexpected alert: {text!r}", evidence, crossed=True,
                    )
            if current != "AMBIGUOUS_RESPONSE":
                current = "SUCCESS_OBSERVED"; phase(current)
                telemetry.record_since("panel.outcome_wait", phase_started,
                                       result="SUCCESS")
                classified_result = result(
                    AutoSubmitOutcome.SUCCESS, current, evidence=evidence, crossed=True
                )
        except Exception as exc:
            selector_result = "TIMEOUT" if "Timeout" in type(exc).__name__ else "ERROR"
            selector_error = type(exc).__name__
            selector_wait_ms = round((time.perf_counter() - selector_started) * 1000, 1)
            # Once page.click returned, every inability to prove fresh success
            # remains quota-bearing UNKNOWN.
            final_phase = current if current == "AMBIGUOUS_RESPONSE" else "AMBIGUOUS_RESPONSE"
            accounting_error = isinstance(exc, _AutoPhasePersistenceError)
            if final_phase != current:
                try:
                    phase(final_phase)
                except Exception as phase_exc:
                    accounting_error = True
                    exc = RuntimeError(f"{exc}; phase persistence failed: {phase_exc}")
            classified_result = result(
                AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT, final_phase, exc,
                "fresh success could not be verified", crossed=True,
                accounting_error=accounting_error,
            )

        # Classification and all mandatory Playwright verification are now
        # complete.  Only bounded/local metadata and persistent diagnostics
        # follow; renderer-dependent optional probes remain explicitly skipped.
        navigation_fields = _collect_navigation_evidence(navigation_response)
        navigation_fields.update({
            "user": user_id, "phase": "NAVIGATION",
            "click_entered_ms": round((submit_started - method_started) * 1000, 1),
            "click_returned_ms": round(
                ((click_returned_at or submit_started) - method_started) * 1000, 1),
            "navigation_completed_ms": round(
                (navigation_completed_at - method_started) * 1000, 1),
            **_page_evidence(page),
        })
        navigation_fields = {
            "user": navigation_fields.pop("user"),
            "phase": navigation_fields.pop("phase"),
            **navigation_fields,
        }
        _emit_auto_evidence(navigation_fields)
        result_fields: Dict[str, object] = {
            "user": user_id, "phase": "RESULT_WAIT", "selector": selector,
            "pre_count": "not_probed", "pre_visible": "not_probed",
            "wait_timeout_ms": remaining_ms, "result": selector_result,
            "selector_wait_ms": selector_wait_ms,
            "navigation_commit_ms": round((navigation_completed_at - submit_started) * 1000, 1),
            "success_capture_ms": round((capture["at"] - submit_started) * 1000, 1) if capture else "unavailable",
            "success_verify_ms": selector_wait_ms,
            "verification_total_ms": round((time.perf_counter() - submit_started) * 1000, 1),
            "success_capture_source": capture["source"] if capture else "unavailable",
            "total_submit_ms": round((time.perf_counter() - method_started) * 1000, 1),
            **_page_evidence(page),
        }
        if selector_error is not None:
            result_fields["error"] = selector_error
        _emit_auto_evidence(
            result_fields,
            warning=classified_result.outcome is AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT,
        )
        self._auto_attempt = None
        return classified_result

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
    @timed("panel.screenshot")
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
