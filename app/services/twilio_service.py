"""
Twilio service — downloads recording audio in-memory.
"""
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class TwilioService:
    def __init__(self):
        self.account_sid = settings.twilio_account_sid
        self.auth_token = settings.twilio_auth_token
        if not self.account_sid:
            logger.warning("TWILIO_ACCOUNT_SID not set — Twilio downloads will fail")

    def _auth(self) -> tuple[str, str]:
        return (self.account_sid, self.auth_token)

    def is_twilio_url(self, url: str) -> bool:
        return "twilio.com" in url or "api.twilio.com" in url

    async def download_audio(self, recording_url: str) -> bytes:
        """
        Download a Twilio recording as raw bytes (in memory).
        Handles Twilio Basic Auth automatically.
        """
        if not recording_url:
            raise ValueError("recording_url is empty")

        # Twilio recording URLs may need .mp3 appended
        if self.is_twilio_url(recording_url) and not recording_url.endswith((".mp3", ".wav")):
            recording_url = recording_url + ".mp3"

        auth = self._auth() if self.is_twilio_url(recording_url) else None

        async with httpx.AsyncClient(timeout=120) as client:
            kwargs = {"follow_redirects": True}
            if auth:
                kwargs["auth"] = auth
            response = await client.get(recording_url, **kwargs)
            response.raise_for_status()
            return response.content
