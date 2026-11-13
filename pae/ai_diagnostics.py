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
        nested = getattr(item, "error", None)
        if isinstance(nested, BaseException):
            pending.append(nested)
        cause = item.__cause__ or item.__context__
        if cause is not None:
            pending.append(cause)

    def status(item):
        code = getattr(item, "code", None)
        if not isinstance(code, int):
            code = getattr(item, "status_code", None)
        if not isinstance(code, int):
            code = getattr(getattr(item, "response", None), "status_code", None)
        return code if isinstance(code, int) and 400 <= code <= 599 else None
    provider = next((item for item in errors if status(item)), None)
    root = provider or errors[-1]
    code = status(provider) if provider else None
    messages = {
        400: ("provider_request", "The AI provider rejected the request. Check key validity, API restrictions and model/request compatibility."),
        401: ("provider_auth", "The AI provider authentication failed. Check the API key in Codespaces secrets."),
        403: ("provider_access", "The AI provider denied access. Check key restrictions, project/API permissions and regional availability."),
        404: ("provider_model", "The configured model or endpoint was not found or is unavailable to this project."),
        429: ("provider_quota", "The AI provider rate or quota limit reached. Check the selected provider's quota dashboard before retrying."),
    }
    if code in messages:
        category, message = messages[code]
    elif code and code >= 500:
        category, message = "provider_unavailable", "The AI provider reported a server error. Retry later."
    elif code:
        category, message = "provider_request", "The AI provider rejected the request. Run scripts/check_ai.py to isolate provider access."
    elif any("Timeout" in type(item).__name__ for item in errors):
        category, message = "ai_timeout", "The AI request timed out; no report was published."
    elif any(type(item).__name__ in {"ConnectError", "ProxyError", "NetworkError", "ConnectFailure"} for item in errors):
        category, message = "provider_connection", "Cannot connect to the AI provider. Check the Codespace network and proxy settings."
    else:
        category, message = "workflow_error", "The ADK workflow failed. Run scripts/check_ai.py and share its diagnostic output."
    return {"message": message, "error_code": category,
            "error_type": type(root).__name__, "provider_code": code}
