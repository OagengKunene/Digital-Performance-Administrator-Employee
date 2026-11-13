from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./.local/pae.db"
    google_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    ai_timeout_seconds: int = Field(default=300, ge=1, le=600)
    ai_max_model_calls: int = Field(default=24, ge=1, le=50)


@lru_cache
def settings():
    return Settings()
