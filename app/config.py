from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/platform.db"
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost:5174,http://127.0.0.1:5174,http://127.0.0.1:8001"
    model_base_url: str = "http://127.0.0.1:4000/v1"
    model_console_url: str = "http://127.0.0.1:4000/ui"
    knowledge_url: str = "http://127.0.0.1:8002"
    knowledge_public_url: str = "http://127.0.0.1:8002/api/v1"
    knowledge_service_token: str = ""
    model_key_encryption_key: str = ""
    # Applies to both official user calls and independent model keys.
    model_call_capture_enabled: bool = True
    model_call_retention_days: int = 7
    github_client_id: str = ""
    github_client_secret: str = ""
    github_callback_url: str = ""
    release_allowed_hosts: str = ""


@lru_cache
def settings():
    return Settings()
