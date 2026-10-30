"""Typed application settings. This is the only module that reads environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    app_env: str = "development"
    timezone: str = "America/New_York"
    log_level: str = "INFO"

    # Dashboard auth
    dashboard_username: str = "me"
    dashboard_password_hash: SecretStr | None = None
    session_secret: SecretStr | None = None

    # Database
    database_url: SecretStr | None = None

    # Claude
    anthropic_api_key: SecretStr | None = None
    claude_classifier_model: str = "claude-opus-5-5"
    claude_writer_model: str = "claude-opus-5-5"

    # Gmail
    gmail_credentials_file: Path = Path("credentials.json")
    gmail_readonly_token_file: Path = Path("token.json")
    gmail_send_token_file: Path = Path("token_send.json")
    gmail_oauth_port: int = Field(8080, ge=1024, le=65535)
    # Encrypts Gmail tokens stored in the database (Fernet key; created automatically locally)
    token_encryption_key: SecretStr | None = None
    # Where this app is reachable; Google sends you back to {PUBLIC_BASE_URL}/oauth/google/callback
    public_base_url: str = "http://localhost:8000"

    # Tracker
    poll_interval_minutes: int = Field(60, ge=1)
    initial_backfill_days: int = Field(30, ge=0)
    classifier_confidence_threshold: float = Field(0.75, ge=0, le=1)
    digest_hour: int = Field(20, ge=0, le=23)
    scheduler_enabled: bool = True
    embeddings_enabled: bool = True

    # Notifications
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    slack_webhook_url: SecretStr | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_whatsapp_from: str | None = None
    twilio_whatsapp_to: str | None = None

    # Cold mailing
    outreach_daily_cap: int = Field(30, ge=1)
    outreach_min_gap_seconds: int = Field(60, ge=0)
    outreach_max_gap_seconds: int = Field(180, ge=0)
    outreach_dedupe_days: int = Field(30, ge=0)
    followup_business_days: int = Field(5, ge=1)

    @field_validator(
        "dashboard_password_hash",
        "session_secret",
        "database_url",
        "anthropic_api_key",
        "telegram_bot_token",
        "telegram_chat_id",
        "slack_webhook_url",
        "token_encryption_key",
        "twilio_account_sid",
        "twilio_auth_token",
        "twilio_whatsapp_from",
        "twilio_whatsapp_to",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value):
        """`KEY=` in .env means "not configured", not an empty secret."""
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _check_gap_range(self) -> "Settings":
        if self.outreach_min_gap_seconds > self.outreach_max_gap_seconds:
            raise ValueError("OUTREACH_MIN_GAP_SECONDS must be <= OUTREACH_MAX_GAP_SECONDS")
        return self

    def resolve(self, path: Path) -> Path:
        """Resolve a relative file setting against the project root."""
        return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache
def get_settings() -> Settings:
    return Settings()
