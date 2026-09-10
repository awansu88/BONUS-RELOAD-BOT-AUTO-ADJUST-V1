"""PATCH-10 deterministic early verification and isolation tests."""

from core.panel_service import AutoSubmitOutcome, PanelService
from tests.test_patch02_auto_submit_classification import FakePage, service


class InitContext:
    def __init__(self, page):
        self.pages = [page]
        self.binding = None
        self.script = ""

    def expose_binding(self, name, callback):
        assert name == "__patch10Capture"
        self.binding = callback

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
        "url": "https://panel.example/deposit/manual", "text": "Deposit TELAH Disubmit",
    })
    assert attempt_a["capture"]["source"] == "context_init_observer"

    attempt_b = {"id": "B", "page": page, "generation": 1, "capture": None}
    panel._auto_attempt = attempt_b
    assert attempt_b["capture"] is None
    # A delayed callback from generation A cannot satisfy B.
    capture({"page": page, "frame": page.main_frame}, {
        "url": "https://panel.example/deposit/manual", "text": "Deposit telah disubmit",
    })
    assert attempt_b["capture"] is None


def test_stale_iframe_wrong_page_path_and_wrong_text_are_rejected():
    panel, page, capture = verifier()
    panel._auto_attempt = {"id": "A", "page": page, "generation": 1, "capture": None}
    panel._document_generation[id(page)] = 1  # stale/current document
    good = {"url": "https://panel.example/deposit/manual", "text": "Deposit telah disubmit"}
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
    original_wait = page.wait_for_selector

    def expect_navigation(**kwargs):
        observed["navigation"] = kwargs
        return original_expect(**kwargs)

    def click(selector, **kwargs):
        observed["click"] = kwargs
        return original_click(selector, **kwargs)

    def wait(selector, **kwargs):
        if selector == "#success":
            observed["success"] = kwargs
        return original_wait(selector, **kwargs)

    page.expect_navigation = expect_navigation
    page.click = click
    page.wait_for_selector = wait
    result = service(page, "Deposit telah disubmit").submit_deposit_classified("a", 5000, "r")
    assert result.outcome is AutoSubmitOutcome.SUCCESS
    assert observed["navigation"]["wait_until"] == "commit"
    assert observed["success"]["state"] == "attached"
    assert 0 < observed["success"]["timeout"] <= observed["click"]["timeout"] <= 5


def test_init_script_uses_text_content_and_never_visibility_or_cross_page_fetch():
    panel, _, _ = verifier()
    script = panel._context.script
    assert "textContent" in script and "MutationObserver" in script
    assert "innerText" not in script and "visible" not in script
    assert "/deposit/pending" not in script
    assert "/deposit/history" not in script
    assert "/deposit/transactions" not in script
    assert "fetch(" not in script
