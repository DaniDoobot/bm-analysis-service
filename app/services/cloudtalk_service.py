"""
Minimal CloudTalk API client for resolving call metadata and recording URLs.
Used as an auxiliary resolver when call details or recordings are not directly available in HubSpot.
"""
import logging
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class CloudTalkService:
    def __init__(self):
        settings = get_settings()
        self.api_key_id = (settings.cloudtalk_api_key_id or "").strip()
        self.api_key_secret = (settings.cloudtalk_api_key_secret or "").strip()
        self.base_url = (settings.cloudtalk_api_base_url or "https://my.cloudtalk.io/api").rstrip("/")

    @property
    def is_configured(self) -> bool:
        """Returns True if CloudTalk API credentials are configured."""
        return bool(self.api_key_id and self.api_key_secret)

    def _auth(self) -> tuple[str, str] | None:
        if self.is_configured:
            return (self.api_key_id, self.api_key_secret)
        return None

    async def get_call(self, call_id: str, timeout: int = 15) -> Optional[dict[str, Any]]:
        """
        Fetch call details from CloudTalk API by call ID.
        Returns parsed dict or None if not found or unconfigured.
        """
        if not self.is_configured:
            logger.info("CloudTalk API not configured; skipping direct CloudTalk call lookup for call_id=%s", call_id)
            return None

        clean_id = str(call_id).strip()
        url = f"{self.base_url}/calls/{clean_id}.json"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, auth=self._auth())
                if response.status_code == 404:
                    logger.info("Call %s not found in CloudTalk API (404)", clean_id)
                    return None
                response.raise_for_status()
                data = response.json()
                # CloudTalk responses usually wrap in responseData or data
                return data.get("responseData") or data.get("data") or data
        except Exception as e:
            logger.warning("Error fetching call %s from CloudTalk API: %s", clean_id, e)
            return None

    async def get_recording_url(self, call_id: str) -> Optional[str]:
        """Extract recording URL from CloudTalk call metadata."""
        call_data = await self.get_call(call_id)
        if not call_data:
            return None

        # CloudTalk typically provides recording_url or Call.recording_url
        if isinstance(call_data, dict):
            url = call_data.get("recording_url") or call_data.get("public_recording_url")
            if not url and "Call" in call_data and isinstance(call_data["Call"], dict):
                url = call_data["Call"].get("recording_url") or call_data["Call"].get("public_recording_url")
            if url:
                return str(url).strip()
        return None

    async def find_hubspot_call(self, cloudtalk_call_id: str, is_demo: bool = False) -> Optional[dict[str, Any]]:
        """
        Locates the corresponding HubSpot call object using CloudTalk call ID (hs_call_external_id).
        """
        from app.services.hubspot_service import HubSpotService
        hs = HubSpotService(is_demo=is_demo)
        return await hs.search_call_by_external_id(cloudtalk_call_id, is_demo=is_demo)
