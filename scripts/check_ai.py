"""Check ADK construction and make one tiny Gemini request, without school data."""
import json
import os
import sys

from pae.ai_diagnostics import describe_ai_error
from pae.config import settings


def main():
    config = settings()
    print("GOOGLE_API_KEY: " + ("loaded" if config.google_api_key else "missing"))
    print("Configured model: " + config.gemini_model.replace(config.google_api_key, "<redacted>")
          if config.google_api_key else "Configured model: " + config.gemini_model)
    if not config.google_api_key:
        print("Add GOOGLE_API_KEY to Codespaces secrets, grant this repository access, then stop and restart the Codespace.")
        return 1
    if config.google_api_key != config.google_api_key.strip():
        print("The API key contains surrounding whitespace. Correct the saved secret and restart the Codespace.")
        return 1
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"true", "1"}:
        print("GOOGLE_GENAI_USE_VERTEXAI is enabled, but this setup expects the Gemini Developer API. Remove that setting and restart before testing.")
        return 1
    try:
        from pae.agents import build_team
        build_team({"mean_percentage": 75.0, "marked_count": 3, "missing_count": 1})
        print("ADK team construction: OK")
    except Exception as error:
        print("ADK team construction: FAILED")
        print(json.dumps(describe_ai_error(error), indent=2))
        return 1

    print("Testing Gemini with one short request; no school records are sent.")
    try:
        from google import genai
        from google.genai import types
        with genai.Client(api_key=config.google_api_key, vertexai=False,
                          http_options=types.HttpOptions(timeout=20000,
                              retry_options=types.HttpRetryOptions(attempts=1))) as client:
            response = client.models.generate_content(
                model=config.gemini_model, contents="Reply with the single word OK.",
            )
        if not response.candidates:
            print("Gemini returned no candidates; retry the check or inspect provider restrictions.")
            return 1
        print("Gemini generation: OK")
        print("Provider access works. Retry Agent reports; if it fails, share the new error_code and error_type.")
        return 0
    except Exception as error:
        print("Gemini generation: FAILED")
        print(json.dumps(describe_ai_error(error), indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
