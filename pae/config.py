from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./.local/pae.db"
    google_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    ai_timeout_seconds: int = 90
    ai_max_model_calls: int = 12


@lru_cache
def settings():
    return Settings()
