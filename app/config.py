"""
Application configuration.
All settings are read from environment variables (or .env file).
"""
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = ""
    frontend_public_url: str = "https://speechbm.doobot.ai"

    # ── Azure OpenAI ──────────────────────────────────────────────────────────
    azure_openai_api_version: str = "2025-01-01-preview"
    gemini_api_key: str = ""
    gemini_model: str = "models/gemini-3.1-flash-live-preview"
    gemini_analysis_model: str = "gemini-3-flash-preview"
    gemini_report_model: str = "gemini-3-flash-preview"
    gemini_temperature: float = 0.2
    gemini_max_output_tokens: int | None = None
    gemini_timeout_seconds: int = 120
    ai_provider: str = "gemini"

    azure_openai_text_endpoint: str = "https://azure-openai-gpt-4-ecustomer.openai.azure.com/"
    azure_openai_text_api_key: str = ""
    azure_openai_text_deployment: str = "gpt-4.1"

    azure_openai_audio_endpoint: str = "https://gpt4-ecustomer-embedded.openai.azure.com/"
    azure_openai_audio_api_key: str = ""
    azure_openai_audio_deployment: str = "gpt-audio-1.5"

    azure_openai_transcription_endpoint: str = "https://azure-openai-gpt-4-ecustomer.openai.azure.com/"
    azure_openai_transcription_api_key: str = ""
    azure_openai_transcription_deployment: str = "gpt-4.1"

    # ── HubSpot ───────────────────────────────────────────────────────────────
    hubspot_access_token: str = ""
    hubspot_portal_id: str = "140451581"

    # ── Twilio ────────────────────────────────────────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""

    # ── Automation Scheduler ──────────────────────────────────────────────────
    enable_automation_scheduler: bool = False

    # ── Personalized Training Scheduler ───────────────────────────────────────
    enable_training_scheduler: bool = True
    training_interval_days: int = 14
    training_lookback_days: int = 14

    # ── Password Reveal Flag ──────────────────────────────────────────────────
    allow_password_reveal: bool = True

    # ── Structure Permissions Flag ────────────────────────────────────────────
    enable_structure_permissions: bool = False

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: str = "*"

    @property
    def allowed_origins(self) -> List[str]:
        """Return list of allowed CORS origins."""
        raw = self.cors_origins.strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
