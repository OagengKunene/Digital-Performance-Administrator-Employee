"""One short provider request without school records or printed credentials."""
import argparse
import json
import sys
import httpx
from pae.ai_diagnostics import describe_ai_error
from pae.providers import provider_details


def main(provider=None):
    provider, model, key = provider_details(provider)
    print("Provider: " + provider)
    print("API key: " + ("loaded" if key else "missing"))
    print("Configured model: " + model.replace(key, "<redacted>") if key else "Configured model: " + model)
    if not key:
        print("Add GROQ_API_KEY or GOOGLE_API_KEY in Codespaces secrets, grant repository access, then restart the Codespace.")
        return 1
    if key != key.strip():
        print("The saved key contains surrounding whitespace. Correct the secret and restart.")
        return 1
    print("Sending one short request without school records.")
    try:
        if provider == "groq":
            payload = {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}],
                       "max_completion_tokens": 128}
            if model.startswith("openai/gpt-oss"):
                payload["reasoning_effort"] = "low"
            response = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + key}, json=payload, timeout=30)
            response.raise_for_status()
            if not response.json().get("choices"):
                raise ValueError()
        else:
            from google import genai
            from google.genai import types
            with genai.Client(api_key=key, vertexai=False, http_options=types.HttpOptions(
                    timeout=20000, retry_options=types.HttpRetryOptions(attempts=1))) as client:
                result = client.models.generate_content(model=model, contents="Reply with OK.")
            if not result.candidates:
                raise ValueError()
        print("Provider generation: OK")
        print("This validates provider access, not the complete autonomous workflow.")
        return 0
    except Exception as error:
        print("Provider generation: FAILED")
        print(json.dumps(describe_ai_error(error), indent=2))
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["gemini", "groq"])
    sys.exit(main(parser.parse_args().provider))
