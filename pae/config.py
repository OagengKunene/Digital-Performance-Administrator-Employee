from functools import lru_cache
from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./.local/pae.db"
    google_api_key: str = ""
    groq_api_key: str = ""
    ai_provider: Literal["gemini", "groq", "offline"] = "gemini"
    ai_fallback_provider: Literal["", "gemini", "groq", "offline"] = ""
    groq_model: str = "openai/gpt-oss-20b"
    gemini_model: str = "gemini-3.6-flash"
    ai_timeout_seconds: int = Field(default=300, ge=1, le=600)
    ai_max_model_calls: int = Field(default=10, ge=1, le=50)
    ai_requests_per_minute: int = Field(default=4, ge=1, le=60)
    ai_retry_attempts: int = Field(default=3, ge=1, le=3)
    ai_retry_delay_seconds: int = Field(default=15, ge=1, le=120)
    worker_poll_seconds: float = Field(default=2, ge=0.1, le=30)
    autonomous_daily_report_limit: int = Field(default=1, ge=0, le=100)


@lru_cache
def settings():
    return Settings()
