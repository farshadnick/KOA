from pathlib import Path

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
    )

    mattermost_url: HttpUrl
    mattermost_bot_token: str = Field(min_length=1)
    google_sheet_id: str = Field(min_length=1)
    google_sheet_tab: str = "Support Hours"
    google_credentials_file: Path = Path("/config/google-service-account.json")
    sheet_refresh_seconds: float = Field(default=300.0, ge=10.0)
    rule_cache_path: Path = Path("/data/mattermost-bot/rules.json")
    log_level: str = "INFO"

