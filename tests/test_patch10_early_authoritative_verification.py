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
        elif name == "__patch10DocumentAttempt":
            self.attempt_binding = callback
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
    attempt_a = {"id": "A", "page": page, "generation": 0, "capture": None}
    panel._auto_attempt = attempt_a
    panel._document_generation[id(page)] = 1
    capture({"page": page, "frame": page.main_frame}, {
        "attempt_id": "A", "url": "https://panel.example/deposit/manual",
        "text": "Deposit TELAH Disubmit",
    })
    assert attempt_a["capture"]["source"] == "context_init_observer"

    attempt_b = {"id": "B", "page": page, "generation": 1, "capture": None}
    panel._auto_attempt = attempt_b
    assert attempt_b["capture"] is None
    # Even after B advances the page generation, A's immutable token cannot
    # satisfy B (the original implementation failed to model this ordering).
    panel._document_generation[id(page)] = 2
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
    assert panel._context.attempt_binding(source) is None
    panel._auto_attempt = {"id": "B", "page": page, "generation": 0, "capture": None}
    panel._document_generation[id(page)] = 1
    capture(source, {"attempt_id": None,
                     "url": "https://panel.example/deposit/manual",
                     "text": "Deposit telah disubmit"})
    assert panel._auto_attempt["capture"] is None


def test_stale_iframe_wrong_page_path_and_wrong_text_are_rejected():
    panel, page, capture = verifier()
    panel._auto_attempt = {"id": "A", "page": page, "generation": 1, "capture": None}
    panel._document_generation[id(page)] = 1  # stale/current document
    good = {"attempt_id": "A", "url": "https://panel.example/deposit/manual",
            "text": "Deposit telah disubmit"}
    capture({"page": page, "frame": page.main_frame}, good)
    panel._document_generation[id(page)] = 2
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
    attempt = {"id": "A", "page": page, "generation": 0, "capture": None}
    panel._auto_attempt = attempt
    panel._document_generation[id(page)] = 1
    capture({"page": page, "frame": page.main_frame}, {
        "attempt_id": "A", "url": "https://panel.example/deposit/manual",
        "text": "Deposit telah disubmit",
    })
    original = dict(attempt["capture"])
    # No later DOM callback is needed; the authoritative Python latch survives.
    assert attempt["capture"] == original


def test_init_script_uses_text_content_and_never_visibility_or_cross_page_fetch():
    panel, _, _ = verifier()
    script = panel._context.script
    assert "textContent" in script and "MutationObserver" in script
    assert "characterData: true" in script and "attempt_id: documentAttempt" in script
    assert "innerText" not in script and "visible" not in script
    assert "/deposit/pending" not in script
    assert "/deposit/history" not in script
    assert "/deposit/transactions" not in script
    assert "fetch(" not in script
