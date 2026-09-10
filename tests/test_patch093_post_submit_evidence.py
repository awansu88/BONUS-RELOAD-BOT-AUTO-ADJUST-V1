"""PATCH-09.3 persistent-only post-submit evidence regression tests."""

import pytest

from core import panel_service
from core.panel_service import AutoSubmitOutcome
from tests.test_patch02_auto_submit_classification import FakePage, service


class CaptureLogger:
    def __init__(self):
        self.records = []

    def diagnostic(self, message):
        self.records.append(message)

    def diagnostic_warn(self, message):
        self.records.append(message)


class Request:
    def __init__(self, url, method="GET", resource_type="document", previous=None):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.redirected_from = previous


class Response:
    def __init__(self, *, status=200, body=b"Deposit telah disubmit", url=None,
                 previous=None, body_error=None):
        self.status = status
        self.ok = 200 <= status < 400
        self.url = url or "https://panel.example/deposit/manual?token=SECRET#private"
        self.request = Request(self.url, previous=previous)
        self._body = body
        self._body_error = body_error
        self.body_calls = 0

    def header_value(self, name):
        assert name == "content-type"
        return "text/html; charset=UTF-8"

    def body(self):
        self.body_calls += 1
        if self._body_error:
            raise self._body_error
        return self._body


@pytest.fixture
def evidence(monkeypatch):
    logger = CaptureLogger()
    monkeypatch.setattr(panel_service.AppLogger, "get", classmethod(lambda cls: logger))
    return logger.records


def submit(response, *, fail="", alert_text="Deposit telah disubmit"):
    page = FakePage(fail=fail, alert_text=alert_text)
    page.navigation_response = response
    return service(page, "Deposit telah disubmit").submit_deposit_classified(
        "010414", 5000, "BONUS RELOAD AUTO"
    )


def joined(records):
    return "\n".join(records)


def test_normal_success_records_skipped_body_and_attached_selector(evidence):
    response = Response()
    result = submit(response)
    log = joined(evidence)
    assert result.outcome is AutoSubmitOutcome.SUCCESS
    assert "phase=NAVIGATION" in log and "status=200" in log and "ok=true" in log
    assert "method=GET" in log and "resource_type=document" in log
    assert "content_type=text/html; charset=UTF-8" in log
    assert "body_available=false" in log and "body_error=SKIPPED_UNBOUNDED" in log
    assert "success_text_present=unavailable" in log and response.body_calls == 0
    assert "phase=RESULT_WAIT" in log and "result=ATTACHED" in log


def test_body_success_is_not_probed_and_dom_timeout_remains_unknown(evidence):
    response = Response()
    result = submit(response, fail="success")
    log = joined(evidence)
    assert result.outcome is AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT
    assert "success_text_present=unavailable" in log and response.body_calls == 0
    assert "phase=RESULT_WAIT" in log and "result=TIMEOUT" in log


def test_body_content_cannot_change_dom_timeout_classification(evidence):
    response = Response(body=b"ordinary form")
    result = submit(response, fail="success")
    assert result.outcome is AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT
    assert "success_text_present=unavailable" in joined(evidence)
    assert response.body_calls == 0


def test_body_read_failure_is_avoided_without_changing_success(evidence):
    response = Response(body_error=OSError("do not log this secret"))
    result = submit(response)
    log = joined(evidence)
    assert result.outcome is AutoSubmitOutcome.SUCCESS
    assert "body_available=false" in log and "body_error=SKIPPED_UNBOUNDED" in log
    assert response.body_calls == 0
    assert "do not log" not in log


def test_none_navigation_is_safe_and_remains_unknown(evidence):
    page = FakePage(same_document_navigation=True)
    result = service(page, "Deposit telah disubmit").submit_deposit_classified(
        "010414", 5000, "r"
    )
    assert result.outcome is AutoSubmitOutcome.UNKNOWN_AFTER_SUBMIT
    assert "phase=NAVIGATION" in joined(evidence) and "response=NONE" in joined(evidence)


def test_http_500_does_not_override_dom_success(evidence):
    result = submit(Response(status=500))
    assert result.outcome is AutoSubmitOutcome.SUCCESS
    assert "status=500" in joined(evidence) and "ok=false" in joined(evidence)


def test_redirect_summary_and_url_sanitization(evidence):
    previous = Request("https://panel.example/login?session=LEAK#secret", method="POST")
    response = Response(
        url="https://panel.example/deposit/manual?csrf=LEAK#secret", previous=previous
    )
    assert submit(response).outcome is AutoSubmitOutcome.SUCCESS
    log = joined(evidence)
    assert "redirects=1" in log
    assert "original_url=https://panel.example/login" in log
    assert "final_url=https://panel.example/deposit/manual" in log
    assert "LEAK" not in log and "csrf" not in log and "session" not in log
    assert panel_service._sanitize_url(
        "https://operator:password@panel.example/path?q=secret#fragment"
    ) == "https://panel.example/path"


def test_diagnostic_logging_failure_cannot_change_classification(monkeypatch):
    def fail(cls):
        raise RuntimeError("logger unavailable")

    monkeypatch.setattr(panel_service.AppLogger, "get", classmethod(fail))
    assert submit(Response()).outcome is AutoSubmitOutcome.SUCCESS


def test_unbounded_body_probe_cannot_reduce_selector_budget(evidence):
    class TimingPage(FakePage):
        selector_timeout = None

        def locator(self, selector):
            locator = super().locator(selector)
            original_wait = locator.wait_for
            def wait_for(**kwargs):
                if selector == "#success":
                    self.selector_timeout = kwargs["timeout"]
                return original_wait(**kwargs)
            locator.wait_for = wait_for
            return locator

    response = Response()
    page = TimingPage(alert_text="Deposit telah disubmit")
    page.navigation_response = response
    panel = service(page, "Deposit telah disubmit")
    panel.timeouts["success_wait_ms"] = 15_000

    result = panel.submit_deposit_classified("010414", 5000, "r")

    assert result.outcome is AutoSubmitOutcome.SUCCESS
    assert page.selector_timeout >= 14_900
    assert response.body_calls == 0


def test_renderer_dependent_page_and_preselector_probes_are_not_called(evidence):
    class RendererTrapPage(FakePage):
        evaluate_calls = title_calls = visibility_calls = count_calls = 0

        def evaluate(self, *_):
            self.evaluate_calls += 1
            raise RuntimeError("renderer hung")

        def title(self):
            self.title_calls += 1
            raise RuntimeError("renderer hung")

        def locator(self, selector):
            locator = super().locator(selector)

            def count():
                self.count_calls += 1
                raise RuntimeError("renderer hung")

            def is_visible():
                self.visibility_calls += 1
                raise RuntimeError("renderer hung")

            locator.count = count
            locator.is_visible = is_visible
            return locator

    page = RendererTrapPage(alert_text="Deposit telah disubmit")
    page.navigation_response = Response()
    result = service(page, "Deposit telah disubmit").submit_deposit_classified(
        "010414", 5000, "r"
    )
    log = joined(evidence)

    assert result.outcome is AutoSubmitOutcome.SUCCESS
    assert page.evaluate_calls == page.title_calls == 0
    assert page.count_calls == page.visibility_calls == 0
    assert "ready_state=not_probed" in log and "title=not_probed" in log
    assert "pre_count=not_probed" in log and "pre_visible=not_probed" in log
