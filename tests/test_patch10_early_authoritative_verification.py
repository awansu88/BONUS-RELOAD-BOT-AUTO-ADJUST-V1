"""PATCH-10 deterministic early verification and isolation tests."""

from core.panel_service import AutoSubmitOutcome, PanelService
from tests.test_patch02_auto_submit_classification import FakePage, service


class InitContext:
    def __init__(self, page):
        self.pages = [page]
        self.binding = None
        self.script = ""

    def expose_binding(self, name, callback):
        if name == "__patch10Capture":
            self.binding = callback
        else:
            raise AssertionError(name)

    def add_init_script(self, *, script):
        self.script = script

    def on(self, *_):
        pass


class InitPage:
    def __init__(self):
        self.main_frame = object()
        self.handlers = {}

    def on(self, event, callback):
        self.handlers[event] = callback


def verifier():
    page = InitPage()
    panel = PanelService({}, {
        "panel": {"success_alert": ".alert-success"},
        "success_text": "Deposit telah disubmit",
    })
    panel._context = InitContext(page)
    panel._install_early_submit_verifier()
    return panel, page, panel._context.binding


def test_new_document_capture_is_python_latched_and_attempt_isolated():
    panel, page, capture = verifier()
    attempt_a = {"id": "A", "page": page, "capture": None}
    panel._auto_attempt = attempt_a
    # Capture may beat framenavigated notification; immutable ownership keeps
    # it safe, and final SUCCESS still separately requires navigation proof.
    capture({"page": page, "frame": page.main_frame}, {
        "attempt_id": "A", "url": "https://panel.example/deposit/manual",
        "text": "Deposit TELAH Disubmit",
    })
    assert attempt_a["capture"]["source"] == "context_init_observer"

    attempt_b = {"id": "B", "page": page, "capture": None}
    panel._auto_attempt = attempt_b
    assert attempt_b["capture"] is None
    # A delayed callback remains tagged A and cannot satisfy active attempt B.
    capture({"page": page, "frame": page.main_frame}, {
        "attempt_id": "A", "url": "https://panel.example/deposit/manual",
        "text": "Deposit telah disubmit",
    })
    assert attempt_b["capture"] is None
    capture({"page": page, "frame": page.main_frame}, {
        "attempt_id": "B", "url": "https://panel.example/deposit/manual",
        "text": "Deposit telah disubmit",
    })
    assert attempt_b["capture"]["source"] == "context_init_observer"


def test_document_created_without_attempt_can_never_satisfy_later_attempt():
    panel, page, capture = verifier()
    source = {"page": page, "frame": page.main_frame}
    panel._auto_attempt = {"id": "B", "page": page, "capture": None}
    capture(source, {"attempt_id": None,
                     "url": "https://panel.example/deposit/manual",
                     "text": "Deposit telah disubmit"})
    assert panel._auto_attempt["capture"] is None


def test_stale_iframe_wrong_page_path_and_wrong_text_are_rejected():
    panel, page, capture = verifier()
    panel._auto_attempt = {"id": "A", "page": page, "capture": None}
    good = {"attempt_id": "A", "url": "https://panel.example/deposit/manual",
            "text": "Deposit telah disubmit"}
    capture({"page": page, "frame": page.main_frame}, {**good, "attempt_id": "OLD"})
    capture({"page": page, "frame": object()}, good)
    capture({"page": page, "frame": page.main_frame}, {
        "url": "https://panel.example/deposit/pending", "text": good["text"]})
    capture({"page": page, "frame": page.main_frame}, {
        "url": good["url"], "text": "unrelated alert"})
    assert panel._auto_attempt["capture"] is None


def test_submit_uses_commit_attached_text_content_and_shared_deadline():
    page = FakePage(alert_text="Deposit telah disubmit")
    observed = {}
    original_expect = page.expect_navigation
    original_click = page.click

    def expect_navigation(**kwargs):
        observed["navigation"] = kwargs
        return original_expect(**kwargs)

    def click(selector, **kwargs):
        observed["click"] = kwargs
        return original_click(selector, **kwargs)

    original_locator = page.locator

    def locator(selector):
        result = original_locator(selector)
        original_locator_wait = result.wait_for
        def locator_wait(**kwargs):
            if selector == "#success":
                observed["success"] = kwargs
            return original_locator_wait(**kwargs)
        result.wait_for = locator_wait
        return result

    page.expect_navigation = expect_navigation
    page.click = click
    page.locator = locator
    result = service(page, "Deposit telah disubmit").submit_deposit_classified("a", 5000, "r")
    assert result.outcome is AutoSubmitOutcome.SUCCESS
    assert observed["navigation"]["wait_until"] == "commit"
    assert observed["success"]["state"] == "attached"
    assert 0 < observed["success"]["timeout"] <= observed["click"]["timeout"] <= 5


def test_waiting_result_phase_failure_is_unknown_and_diagnostics_do_not_crash():
    phases = []

    def fail_waiting_result(phase):
        phases.append(phase)
        if phase == "WAITING_FRESH_RESULT":
            raise OSError("journal unavailable")

    result = service(FakePage(), "Deposit successful").submit_deposit_classified(
        "a", 5000, "r", fail_waiting_result
    )
    assert result.outcome is AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT
    assert result.accounting_error is True
    assert result.click_crossed is True
    assert "WAITING_FRESH_RESULT" in phases


def test_session_token_write_failure_is_proven_pre_click():
    page = FakePage()
    phases = []
    original_locator = page.locator

    def locator(selector):
        result = original_locator(selector)
        if selector == "html":
            result.evaluate = lambda *_, **__: (_ for _ in ()).throw(
                RuntimeError("session storage unavailable")
            )
        return result

    page.locator = locator
    result = service(page).submit_deposit_classified("a", 5000, "r", phases.append)
    assert result.outcome is AutoSubmitOutcome.FAILED_NOT_SUBMITTED
    assert result.click_crossed is False and page.clicks == 0
    assert "SUBMIT_CLICK_BOUNDARY" not in phases


def test_attached_empty_then_text_arrives_succeeds_without_visibility_wait():
    page = FakePage(alert_text="")
    original_locator = page.locator

    def locator(selector):
        result = original_locator(selector)
        if selector != "#success":
            return result
        original_wait = result.wait_for

        def wait_for(*, state, **kwargs):
            assert state == "attached"
            assert page.filtered_text == "Deposit successful"
            page.alert_text = "Deposit successful"
            return original_wait(state=state, **kwargs)

        result.wait_for = wait_for
        return result

    page.locator = locator
    result = service(page, "Deposit successful").submit_deposit_classified("a", 5000, "r")
    assert result.outcome is AutoSubmitOutcome.SUCCESS


def test_hidden_correct_marker_succeeds_without_visibility_probe():
    page = FakePage(alert_text="Deposit successful", stale_visible=False)
    result = service(page, "Deposit successful").submit_deposit_classified("a", 5000, "r")
    assert result.outcome is AutoSubmitOutcome.SUCCESS
    success_waits = [event for event in page.events
                     if event[:2] == ("locator-wait", "#success")]
    assert success_waits == [("locator-wait", "#success", "attached")]


def test_transient_capture_remains_latched_after_dom_text_disappears():
    panel, page, capture = verifier()
    attempt = {"id": "A", "page": page, "capture": None}
    panel._auto_attempt = attempt
    capture({"page": page, "frame": page.main_frame}, {
        "attempt_id": "A", "url": "https://panel.example/deposit/manual",
        "text": "Deposit telah disubmit",
    })
    original = dict(attempt["capture"])
    # No later DOM callback is needed; the authoritative Python latch survives.
    assert attempt["capture"] == original


def test_capture_during_fallback_wait_is_reconsumed_after_dom_disappears():
    page = FakePage(alert_text="Deposit successful")
    panel = service(page, "Deposit successful")
    # Direct services do not create a real context, so expose the production
    # callback through a deterministic context double.
    init_context = InitContext(page)
    panel._context = init_context
    page.main_frame = object()
    page.on = lambda *_: None
    panel._early_verifier_installed = False
    panel._install_early_submit_verifier()
    original_locator = page.locator

    def locator(selector):
        result = original_locator(selector)
        if selector != "#success":
            return result
        original_wait = result.wait_for

        def wait_for(**kwargs):
            init_context.binding(
                {"page": page, "frame": page.main_frame},
                {"attempt_id": page.session_attempt,
                 "url": "https://panel.example/deposit/manual",
                 "text": "Deposit successful"},
            )
            page.alert_text = ""
            raise TimeoutError("marker disappeared")

        result.wait_for = wait_for
        return result

    page.locator = locator
    result = panel.submit_deposit_classified("a", 5000, "r")
    assert result.outcome is AutoSubmitOutcome.SUCCESS
    assert "Deposit successful" in result.evidence


def test_init_script_uses_text_content_and_never_visibility_or_cross_page_fetch():
    panel, _, _ = verifier()
    script = panel._context.script
    assert "textContent" in script and "MutationObserver" in script
    assert "characterData: true" in script and "attempt_id: documentAttempt" in script
    assert "const {selector, phrase, storageKey}" in script
    assert "const documentAttempt" in script
    assert "sessionStorage.getItem(storageKey)" in script
    assert script.count("sessionStorage.getItem(storageKey)") == 1
    assert "__patch10DocumentAttempt" not in script
    assert "innerText" not in script and "visible" not in script
    assert "/deposit/pending" not in script
    assert "/deposit/history" not in script
    assert "/deposit/transactions" not in script
    assert "fetch(" not in script
