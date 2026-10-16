"""
CloudTalk API client for resolving call metadata and downloading call recordings directly.
Primary recording source for CloudTalk calls identified via HubSpot hs_call_external_id.
"""
import asyncio
import logging
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Valid MIME types returned by CloudTalk recording endpoint
CLOUDTALK_ALLOWED_RECORDING_CONTENT_TYPES = frozenset({
    "application/octet-stream",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
})

# Maximum Retry-After wait (in seconds) before failing fast to avoid blocking workers
MAX_REASONABLE_RETRY_AFTER_SECONDS = 5.0


def is_valid_wav_bytes(data: bytes) -> bool:
    """Validate RIFF/WAVE binary signature at the start of the payload."""
    return bool(data and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE")


def _parse_retry_after(header_value: str | None) -> float | None:
    """Parse Retry-After header if it represents a numeric value in seconds."""
    if not header_value:
        return None
    try:
        val = float(str(header_value).strip())
        return val if val >= 0 else None
    except (ValueError, TypeError):
        return None


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

    def get_recording_endpoint_url(self, call_id: str) -> str:
        """Build the direct CloudTalk API recording endpoint URL for a call_id."""
        clean_id = str(call_id).strip()
        return f"{self.base_url}/calls/recording/{clean_id}.json"

    @staticmethod
    def _normalize_call_item(item: dict[str, Any], requested_call_id: str) -> dict[str, Any]:
        """
        Normalize a CloudTalk call item from /calls/index.json responseData.data.
        NOTE: Cdr.recording_link points to https://analytics.cloudtalk.io/call-details/{id} (HTML UI)
        and must NEVER be used as an audio download URL.
        """
        cdr = item.get("Cdr") if isinstance(item.get("Cdr"), dict) else {}
        call_obj = item.get("Call") if isinstance(item.get("Call"), dict) else {}
        resolved_id = str(
            cdr.get("id")
            or call_obj.get("id")
            or item.get("id")
            or requested_call_id
        ).strip()

        recorded_val = cdr.get("recorded")
        if recorded_val is None and "recorded" in item:
            recorded_val = item.get("recorded")

        return {
            "id": resolved_id,
            "call_id": resolved_id,
            "recorded": bool(recorded_val) if recorded_val is not None else None,
            "type": cdr.get("type") or item.get("type"),
            "billsec": cdr.get("billsec") or item.get("billsec"),
            "talking_time": cdr.get("talking_time") or item.get("talking_time"),
            "started_at": cdr.get("started_at") or item.get("started_at"),
            "public_internal": cdr.get("public_internal"),
            "public_external": cdr.get("public_external"),
            "Cdr": cdr or item.get("Cdr"),
            "Agent": item.get("Agent"),
            "Contact": item.get("Contact"),
            "CallNumber": item.get("CallNumber"),
        }

    async def get_call(self, call_id: str, timeout: int = 15) -> Optional[dict[str, Any]]:
        """
        Fetch call metadata from CloudTalk API using GET /calls/index.json?call_id={call_id}.
        Does NOT use /calls/{id}.json (which returns 404 in CloudTalk API v1).
        Returns normalized call dict or None if not found, rate-limited, or unconfigured.
        """
        if not self.is_configured:
            logger.info("CloudTalk API not configured; skipping direct CloudTalk call lookup for call_id=%s", call_id)
            return None

        clean_id = str(call_id or "").strip()
        if not clean_id:
            return None

        url = f"{self.base_url}/calls/index.json"
        params = {"call_id": clean_id}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, params=params, auth=self._auth())

                if response.status_code == 429:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    logger.warning(
                        "CloudTalk API rate limit (429) on get_call: provider=cloudtalk, call_id=%s, status=429, retry_after=%s",
                        clean_id,
                        retry_after,
                    )
                    if retry_after is not None and 0 < retry_after <= MAX_REASONABLE_RETRY_AFTER_SECONDS:
                        await asyncio.sleep(retry_after)
                        response = await client.get(url, params=params, auth=self._auth())
                    if response.status_code == 429:
                        return None

                if response.status_code == 404:
                    logger.info("Call %s not found in CloudTalk API (404)", clean_id)
                    return None
                if response.status_code in (401, 403):
                    logger.warning(
                        "CloudTalk API auth error on get_call: provider=cloudtalk, call_id=%s, status=%d",
                        clean_id,
                        response.status_code,
                    )
                    return None

                response.raise_for_status()
                payload = response.json()

                if not isinstance(payload, dict):
                    return None

                resp_data = payload.get("responseData")
                items: list[Any] = []
                if isinstance(resp_data, dict):
                    raw_data = resp_data.get("data")
                    if isinstance(raw_data, list):
                        items = raw_data
                    elif isinstance(raw_data, dict):
                        items = [raw_data]
                    elif "Cdr" in resp_data or "Call" in resp_data or "id" in resp_data:
                        items = [resp_data]
                elif isinstance(payload.get("data"), list):
                    items = payload["data"]
                elif "Cdr" in payload or "Call" in payload or "id" in payload:
                    items = [payload]

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    cdr = item.get("Cdr") if isinstance(item.get("Cdr"), dict) else {}
                    call_obj = item.get("Call") if isinstance(item.get("Call"), dict) else {}
                    item_id = str(cdr.get("id") or call_obj.get("id") or item.get("id") or "").strip()
                    if item_id == clean_id:
                        return self._normalize_call_item(item, clean_id)

                logger.info("Call %s not found in CloudTalk index.json responseData", clean_id)
                return None

        except Exception as e:
            logger.warning(
                "Error fetching call metadata from CloudTalk API: provider=cloudtalk, call_id=%s, error=%s",
                clean_id,
                type(e).__name__,
            )
            return None

    async def get_recording_url(self, call_id: str) -> Optional[str]:
        """
        Resolve direct CloudTalk API recording endpoint URL if the call exists.
        Never returns Cdr.recording_link (analytics.cloudtalk.io HTML SPA page).
        """
        call_data = await self.get_call(call_id)
        if not call_data:
            return None

        if isinstance(call_data, dict):
            # Ignore analytics.cloudtalk.io links; only return direct API recording endpoint
            for candidate_key in ("recording_url", "public_recording_url"):
                val = call_data.get(candidate_key)
                if not val and isinstance(call_data.get("Call"), dict):
                    val = call_data["Call"].get(candidate_key)
                if val and "analytics.cloudtalk.io" not in str(val).lower():
                    return str(val).strip()

            if call_data.get("recorded") is False:
                return None

            return self.get_recording_endpoint_url(call_id)
        return None

    async def download_recording(self, call_id: str, timeout: int = 120) -> bytes:
        """
        Download call recording WAV directly from CloudTalk API:
        GET {base_url}/calls/recording/{call_id}.json with HTTP Basic Auth.

        Validates:
        - HTTP 200 (with controlled handling of 404, 401/403, 429, timeouts)
        - Size > 0
        - Compatible Content-Type (application/octet-stream, audio/wav, audio/x-wav, audio/wave)
        - Binary RIFF/WAVE header signature
        """
        from app.services.audio_download_service import AudioDownloadError

        clean_id = str(call_id or "").strip()
        if not clean_id:
            raise AudioDownloadError(
                "El call_id de CloudTalk está vacío.",
                status_code=400,
                stage="download_audio",
            )

        if not self.is_configured:
            raise AudioDownloadError(
                "Credenciales de CloudTalk (CLOUDTALK_API_KEY_ID / CLOUDTALK_API_KEY_SECRET) no configuradas.",
                status_code=502,
                stage="download_audio",
            )

        url = self.get_recording_endpoint_url(clean_id)
        logger.info("Initiating CloudTalk recording download: provider=cloudtalk, call_id=%s", clean_id)

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url, auth=self._auth())

                # Controlled HTTP 429 rate-limit handling (at most 1 retry if Retry-After is small)
                if response.status_code == 429:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    logger.warning(
                        "CloudTalk recording download rate-limited: provider=cloudtalk, call_id=%s, status=429, retry_after=%s",
                        clean_id,
                        retry_after,
                    )
                    if retry_after is not None and 0 < retry_after <= MAX_REASONABLE_RETRY_AFTER_SECONDS:
                        await asyncio.sleep(retry_after)
                        response = await client.get(url, auth=self._auth())

                    if response.status_code == 429:
                        raise AudioDownloadError(
                            f"Error 429: Límite de peticiones (Rate Limit) alcanzado en CloudTalk para call_id={clean_id}.",
                            status_code=429,
                            stage="download_audio",
                        )

        except AudioDownloadError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            logger.warning(
                "Connection error downloading recording from CloudTalk: provider=cloudtalk, call_id=%s, error=%s",
                clean_id,
                type(e).__name__,
            )
            raise AudioDownloadError(
                f"No se pudo conectar con CloudTalk API para call_id={clean_id}: error de red o timeout.",
                status_code=504,
                stage="download_audio",
            )
        except httpx.TimeoutException as e:
            logger.warning(
                "Timeout downloading recording from CloudTalk: provider=cloudtalk, call_id=%s, timeout=%ds, error=%s",
                clean_id,
                timeout,
                type(e).__name__,
            )
            raise AudioDownloadError(
                f"Timeout al descargar la grabación desde CloudTalk para call_id={clean_id} tras {timeout}s.",
                status_code=504,
                stage="download_audio",
            )
        except Exception as e:
            logger.warning(
                "Unexpected error requesting recording from CloudTalk: provider=cloudtalk, call_id=%s, error=%s",
                clean_id,
                type(e).__name__,
            )
            raise AudioDownloadError(
                f"Error inesperado al descargar grabación desde CloudTalk para call_id={clean_id}.",
                status_code=502,
                stage="download_audio",
            )

        if response.status_code in (401, 403):
            logger.warning(
                "CloudTalk recording auth/permission error: provider=cloudtalk, call_id=%s, status=%d",
                clean_id,
                response.status_code,
            )
            raise AudioDownloadError(
                f"Error {response.status_code}: No autorizado al descargar grabación desde CloudTalk para call_id={clean_id}.",
                status_code=502,
                stage="download_audio",
            )

        if response.status_code == 404:
            logger.info(
                "CloudTalk recording not found: provider=cloudtalk, call_id=%s, status=404",
                clean_id,
            )
            raise AudioDownloadError(
                f"Error 404: Grabación no encontrada en CloudTalk para call_id={clean_id}.",
                status_code=404,
                stage="download_audio",
            )

        if response.status_code != 200:
            logger.warning(
                "CloudTalk recording unexpected HTTP status: provider=cloudtalk, call_id=%s, status=%d",
                clean_id,
                response.status_code,
            )
            raise AudioDownloadError(
                f"Error HTTP {response.status_code} al descargar grabación desde CloudTalk para call_id={clean_id}.",
                status_code=502,
                stage="download_audio",
            )

        content = response.content
        content_type = response.headers.get("content-type", "").lower().split(";")[0].strip()

        if not content or len(content) == 0:
            logger.warning(
                "CloudTalk recording empty payload: provider=cloudtalk, call_id=%s, status=200",
                clean_id,
            )
            raise AudioDownloadError(
                f"La grabación descargada desde CloudTalk está vacía (0 bytes) para call_id={clean_id}.",
                status_code=502,
                stage="download_audio",
            )

        if content_type not in CLOUDTALK_ALLOWED_RECORDING_CONTENT_TYPES:
            logger.warning(
                "CloudTalk recording invalid content-type: provider=cloudtalk, call_id=%s, content_type=%s",
                clean_id,
                content_type,
            )
            raise AudioDownloadError(
                f"Tipo de contenido no compatible desde CloudTalk ({content_type}) para call_id={clean_id}.",
                status_code=502,
                stage="download_audio",
            )

        if not is_valid_wav_bytes(content):
            logger.warning(
                "CloudTalk recording failed RIFF/WAVE binary validation: provider=cloudtalk, call_id=%s, content_type=%s, size_bytes=%d",
                clean_id,
                content_type,
                len(content),
            )
            raise AudioDownloadError(
                f"El archivo descargado desde CloudTalk no contiene una cabecera WAV (RIFF/WAVE) válida para call_id={clean_id}.",
                status_code=502,
                stage="download_audio",
            )

        logger.info(
            "CloudTalk recording downloaded successfully: provider=cloudtalk, call_id=%s, status=200, content_type=%s, size_bytes=%d",
            clean_id,
            content_type,
            len(content),
        )
        return content

    async def find_hubspot_call(self, cloudtalk_call_id: str, is_demo: bool = False) -> Optional[dict[str, Any]]:
        """
        Locates the corresponding HubSpot call object using CloudTalk call ID (hs_call_external_id).
        """
        from app.services.hubspot_service import HubSpotService
        hs = HubSpotService(is_demo=is_demo)
        return await hs.search_call_by_external_id(cloudtalk_call_id, is_demo=is_demo)

