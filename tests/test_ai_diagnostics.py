import asyncio
import json
import logging
import pytest
from google.genai.errors import ClientError, ServerError
from pae.ai_diagnostics import describe_ai_error
from pae.agents import run_reviewed_report


@pytest.mark.parametrize("code,category", [
    (400, "provider_request"), (401, "provider_auth"),
    (403, "provider_access"), (404, "provider_model"),
    (429, "provider_quota"), (503, "provider_unavailable"),
])
def test_provider_errors_are_classified_without_messages(code, category):
    error_class = ServerError if code >= 500 else ClientError
    error = error_class(code, {"error": {"message": "private-key-and-learner-data"}})
    detail = describe_ai_error(error)
    assert detail["error_code"] == category
    assert detail["provider_code"] == code
    assert "private-key-and-learner-data" not in json.dumps(detail)


def test_wrapped_provider_error_is_found():
    inner = ClientError(429, {"error": {"message": "private provider body"}})
    detail = describe_ai_error(ExceptionGroup("private wrapper", [inner]))
    assert detail["provider_code"] == 429
    assert detail["error_code"] == "provider_quota"
    assert "private" not in json.dumps(detail)


def test_unknown_error_does_not_expose_its_message():
    detail = describe_ai_error(ValueError("secret API key"))
    assert detail["error_code"] == "workflow_error"
    assert detail["error_type"] == "ValueError"
    assert "secret API key" not in json.dumps(detail)


def test_workflow_failure_returns_and_logs_safe_diagnostics(monkeypatch, caplog):
    def fail(_):
        raise ValueError("secret-key-and-private-prompt")
    monkeypatch.setattr("pae.agents.build_team", fail)
    with caplog.at_level(logging.ERROR, logger="pae.agents"):
        result = asyncio.run(run_reviewed_report(
            {"mean_percentage": 75.0, "marked_count": 3, "missing_count": 1},
            "Private school request", "diagnostic-test-run"))
    assert result["status"] == "failed"
    assert result["result"]["error_type"] == "ValueError"
    assert "diagnostic-test-run" in caplog.text
    assert "secret-key-and-private-prompt" not in caplog.text + json.dumps(result)
