"""Credential-safe AI failure metadata; never serialise exception messages."""
from collections import deque


def describe_ai_error(error):
    pending = deque([error])
    seen = set()
    errors = []
    while pending and len(errors) < 12:
        item = pending.popleft()
        if id(item) in seen:
            continue
        seen.add(id(item))
        errors.append(item)
        pending.extend(getattr(item, "exceptions", ()))
        cause = item.__cause__ or item.__context__
        if cause is not None:
            pending.append(cause)

    provider = next((item for item in errors
                     if isinstance(getattr(item, "code", None), int)
                     and 400 <= item.code <= 599), None)
    root = provider or errors[-1]
    code = provider.code if provider else None
    messages = {
        400: ("provider_request", "Gemini rejected the request. Check key validity, API restrictions and model/request compatibility."),
        401: ("provider_auth", "Gemini authentication failed. Check the API key in Codespaces secrets."),
        403: ("provider_access", "Gemini denied access. Check key restrictions, project/API permissions and regional availability."),
        404: ("provider_model", "The configured Gemini model or endpoint was not found or is unavailable to this project."),
        429: ("provider_quota", "Gemini rate or quota limit reached. Check this project's model quota in Google AI Studio before retrying."),
    }
    if code in messages:
        category, message = messages[code]
    elif code and code >= 500:
        category, message = "provider_unavailable", "Gemini reported a server error. Retry later."
    elif code:
        category, message = "provider_request", "Gemini rejected the request. Run scripts/check_ai.py to isolate provider access."
    elif any("Timeout" in type(item).__name__ for item in errors):
        category, message = "ai_timeout", "The AI request timed out; no report was published."
    elif any(type(item).__name__ in {"ConnectError", "ProxyError", "NetworkError", "ConnectFailure"} for item in errors):
        category, message = "provider_connection", "Cannot connect to Gemini. Check the Codespace network and proxy settings."
    else:
        category, message = "workflow_error", "The ADK workflow failed. Run scripts/check_ai.py and share its diagnostic output."
    return {"message": message, "error_code": category,
            "error_type": type(root).__name__, "provider_code": code}
