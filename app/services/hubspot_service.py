"""
HubSpot service — wraps HubSpot CRM API v3.
"""
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

HUBSPOT_API_BASE = "https://api.hubapi.com"


class HubSpotService:
    def __init__(self):
        self.token = settings.hubspot_access_token
        self.portal_id = settings.hubspot_portal_id
        if not self.token:
            logger.warning("HUBSPOT_ACCESS_TOKEN not set — HubSpot calls will fail")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def build_hubspot_url(self, call_id: str) -> str:
        if self.portal_id:
            return f"https://app.hubspot.com/calls/{self.portal_id}/review/{call_id}"
        return f"https://app.hubspot.com/calls/review/{call_id}"

    async def get_call(self, call_id: str) -> dict[str, Any]:
        """
        Fetch call engagement from HubSpot CRM API.
        Returns normalized metadata dict.
        """
        url = f"{HUBSPOT_API_BASE}/crm/v3/objects/calls/{call_id}"
        params = {
            "properties": ",".join([
                "hs_call_direction",
                "hs_call_duration",
                "hs_call_recording_url",
                "hs_timestamp",
                "hs_createdate",
                "hubspot_owner_id",
                "hs_call_status",
                "hs_call_title",
            ])
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            data = response.json()

        props = data.get("properties", {})
        owner_id = props.get("hubspot_owner_id")

        return {
            "call_id": call_id,
            "hubspot_url": self.build_hubspot_url(call_id),
            "call_direction": props.get("hs_call_direction"),
            "call_duration": props.get("hs_call_duration"),
            "recording_url": props.get("hs_call_recording_url"),
            "call_timestamp": props.get("hs_timestamp") or props.get("hs_createdate"),
            "hs_timestamp": props.get("hs_timestamp"),
            "hs_createdate": props.get("hs_createdate"),
            "hubspot_owner_id": owner_id,
            "agente_telefonico": owner_id,  # Will be enriched if owner lookup is added
            "status": props.get("hs_call_status"),
        }

    async def get_owner_name(self, owner_id: str) -> str | None:
        """Optionally resolve owner_id to a display name."""
        if not owner_id:
            return None
        url = f"{HUBSPOT_API_BASE}/crm/v3/owners/{owner_id}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, headers=self._headers())
                response.raise_for_status()
                data = response.json()
                first = data.get("firstName", "")
                last = data.get("lastName", "")
                return f"{first} {last}".strip() or owner_id
        except Exception as e:
            logger.warning("Could not resolve owner %s: %s", owner_id, e)
            return owner_id
