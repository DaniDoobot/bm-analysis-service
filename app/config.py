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
    database_url: str = "postgresql+asyncpg://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@127.0.0.1:5433/n8n"

    # ── Azure OpenAI ──────────────────────────────────────────────────────────
    azure_openai_api_version: str = "2025-01-01-preview"

    azure_openai_text_endpoint: str = "https://azure-openai-gpt-4-ecustomer.openai.azure.com/"
    azure_openai_text_api_key: str = "192a3799f9594ee1b77cc6c0191f8710"
    azure_openai_text_deployment: str = "gpt-4.1"

    azure_openai_audio_endpoint: str = "https://gpt4-ecustomer-embedded.openai.azure.com/"
    azure_openai_audio_api_key: str = "387c53ae7c3144c289f3a94800e80f8c"
    azure_openai_audio_deployment: str = "gpt-audio-1.5"

    azure_openai_transcription_endpoint: str = "https://azure-openai-gpt-4-ecustomer.openai.azure.com/"
    azure_openai_transcription_api_key: str = "192a3799f9594ee1b77cc6c0191f8710"
    azure_openai_transcription_deployment: str = "gpt-4.1"

    # ── HubSpot ───────────────────────────────────────────────────────────────
    hubspot_access_token: str = ""
    hubspot_portal_id: str = ""

    # ── Twilio ────────────────────────────────────────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""

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
