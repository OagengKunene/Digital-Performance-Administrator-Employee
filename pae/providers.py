"""Explicit provider selection; keys stay in process settings, never job records."""
import os
from .config import settings

# Use the catalog bundled with the pinned connector, avoiding an unrelated
# GitHub metadata request on each cold start. Provider inference stays online.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


class ProviderNotConfigured(Exception):
    pass


def provider_details(provider=None):
    config = settings()
    provider = provider or config.ai_provider
    if provider == "groq":
        return provider, config.groq_model, config.groq_api_key
    if provider == "gemini":
        return provider, config.gemini_model, config.google_api_key
    raise ProviderNotConfigured()


def configured(provider=None):
    return bool(provider_details(provider)[2])


def make_model(provider, model):
    _, _, key = provider_details(provider)
    if not key:
        raise ProviderNotConfigured()
    if provider == "groq":
        from google.adk.models.lite_llm import LiteLlm
        options = {"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}
        return LiteLlm(model="groq/" + model, api_key=key, num_retries=0,
                       timeout=60, **options)
    from google.adk.models.google_llm import Gemini
    from google.genai import types
    return Gemini(model=model, client_kwargs={"api_key": key},
                  retry_options=types.HttpRetryOptions(attempts=1))
