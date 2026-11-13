import asyncio
import json
import logging
import pytest
from google.genai.errors import ClientError, ServerError
from pae.ai_diagnostics import describe_ai_error


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


def test_openai_compatible_status_codes_are_safe():
    class ProviderError(Exception):
        status_code = 429
    detail = describe_ai_error(ProviderError("private provider body"))
    assert detail["provider_code"] == 429
    assert detail["error_code"] == "provider_quota"
    assert "private" not in json.dumps(detail)
