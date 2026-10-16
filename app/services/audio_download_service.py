"""
Centralized audio download service for call recordings.
Supports:
- Direct provider download: CloudTalk API (/calls/recording/{call_id}.json) via cloudtalk_id (primary source).
- Fallback / Historical URL download:
  * Boston Medical archive (grabaciones.bostonmedical.info) with X-Api-Key header.
  * Twilio recordings (api.twilio.com) with HTTP Basic Auth.
  * Direct audio URLs.

Enforces:
- Strict content-type and binary payload validation (RIFF/WAVE required for application/octet-stream, rejection of HTML/SSO screens).
- Safe logging (masking API keys, credentials, and query signature parameters).
- Preserving exact URLs and query params for Boston Archive (no appending .mp3).
"""
import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from app.config import get_settings
from app.services.cloudtalk_service import CloudTalkService, is_valid_wav_bytes

logger = logging.getLogger(__name__)

# Valid audio MIME types accepted from remote archives
ALLOWED_AUDIO_CONTENT_TYPES = frozenset({
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mp4",
    "audio/x-m4a",
    "audio/aac",
    "audio/ogg",
    "application/octet-stream",
})


def detect_audio_format(audio_bytes: bytes, recording_url: str | None = None) -> str:
    """
    Determine audio format ('wav' or 'mp3') from binary signature first, then URL extension.
    """
    if is_valid_wav_bytes(audio_bytes):
        return "wav"
    if recording_url:
        clean_path = recording_url.strip().split("?")[0].lower()
        if clean_path.endswith(".wav"):
            return "wav"
    return "mp3"


class AudioDownloadError(Exception):
    """Specific exception raised when a recording cannot be downloaded or validated."""

    def __init__(self, message: str, status_code: int = 502, stage: str = "download_audio"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.stage = stage


def sanitize_recording_url(url: str) -> str:
    """
    Sanitizes URL for safe logging by masking sensitive query parameters (e.g., f=..., token=...).
    Preserves scheme, host, and path.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        masked_pairs = []
        for k, v in query_pairs:
            # Mask signature parameters or tokens
            if k.lower() in ("f", "token", "sig", "signature", "key", "auth"):
                masked_pairs.append((k, "***"))
            elif len(v) > 8:
                masked_pairs.append((k, f"{v[:3]}...{v[-3:]}"))
            else:
                masked_pairs.append((k, v))
        masked_query = urlencode(masked_pairs)
        return urlunparse(parsed._replace(query=masked_query))
    except Exception:
        # Fallback: strip query entirely if parsing fails
        return url.split("?")[0] + "?[masked_query]"


class AudioDownloadService:
    def __init__(self, cloudtalk_service: CloudTalkService | None = None):
        settings = get_settings()
        self.twilio_account_sid = settings.twilio_account_sid
        self.twilio_auth_token = settings.twilio_auth_token
        self.boston_recording_api_key = (settings.boston_recording_api_key or "").strip()
        self._cloudtalk_service = cloudtalk_service

    @property
    def cloudtalk_service(self) -> CloudTalkService:
        if self._cloudtalk_service is None:
            self._cloudtalk_service = CloudTalkService()
        return self._cloudtalk_service

    @staticmethod
    def is_twilio_url(url: str) -> bool:
        """Check if URL points to Twilio API."""
        return "twilio.com" in url or "api.twilio.com" in url

    @staticmethod
    def is_boston_archive_url(url: str) -> bool:
        """Check if URL points to Boston Medical archive server."""
        return "grabaciones.bostonmedical.info" in url or "bostonmedical.info" in url

    def detect_provider(self, url: str) -> str:
        """Detect remote recording provider based on URL host."""
        if self.is_twilio_url(url):
            return "twilio"
        if self.is_boston_archive_url(url):
            return "boston_archive"
        if "cloudtalk.io" in url:
            return "cloudtalk"
        return "direct"

    async def download_from_cloudtalk(self, call_id: str, timeout: int = 120) -> bytes:
        """
        Download recording directly from CloudTalk API using call_id.
        """
        return await self.cloudtalk_service.download_recording(call_id, timeout=timeout)

    async def download_audio(
        self,
        recording_url: str | None = None,
        timeout: int = 120,
        cloudtalk_id: str | None = None,
    ) -> bytes:
        """
        Download recording audio into memory.

        Strategy:
        1. Primary: If cloudtalk_id is provided, attempt direct WAV download from CloudTalk API.
           If CloudTalk fails (404, 403, 429, timeout, invalid response, controlled error)
           and recording_url is available, log safely and fall back to recording_url.
        2. Fallback / Historical: Download from recording_url (IA España grabaciones.bostonmedical.info
           with X-Api-Key, or Twilio with Basic Auth, or direct URL).
        """
        clean_cloudtalk_id = str(cloudtalk_id or "").strip()
        clean_recording_url = str(recording_url or "").strip()

        if clean_cloudtalk_id:
            try:
                return await self.download_from_cloudtalk(clean_cloudtalk_id, timeout=timeout)
            except AudioDownloadError as ct_err:
                has_fallback = bool(clean_recording_url)
                logger.warning(
                    "CloudTalk primary download failed: provider=cloudtalk, call_id=%s, status=%d, fallback_applied=%s, reason=%s",
                    clean_cloudtalk_id,
                    ct_err.status_code,
                    has_fallback,
                    ct_err.message,
                )
                if not has_fallback:
                    raise
            except Exception as ct_exc:
                has_fallback = bool(clean_recording_url)
                logger.warning(
                    "CloudTalk primary download unexpected error: provider=cloudtalk, call_id=%s, status=502, fallback_applied=%s, error=%s",
                    clean_cloudtalk_id,
                    has_fallback,
                    type(ct_exc).__name__,
                )
                if not has_fallback:
                    raise AudioDownloadError(
                        f"No se pudo descargar la grabación desde CloudTalk para call_id={clean_cloudtalk_id}.",
                        status_code=502,
                        stage="download_audio",
                    )

        if not clean_recording_url:
            raise AudioDownloadError(
                "La URL de grabación está vacía.",
                status_code=400,
                stage="download_audio",
            )

        url = clean_recording_url
        provider = self.detect_provider(url)
        sanitized_url = sanitize_recording_url(url)
        parsed_domain = urlparse(url).netloc

        # Guard against Cdr.recording_link (analytics.cloudtalk.io HTML SPA page)
        if "analytics.cloudtalk.io" in parsed_domain.lower():
            logger.error(
                "Rejected CloudTalk analytics HTML link as recording URL: provider=cloudtalk, url=%s",
                sanitized_url,
            )
            raise AudioDownloadError(
                "El enlace analytics.cloudtalk.io no es un archivo de audio descargable (es una página HTML).",
                status_code=502,
                stage="download_audio",
            )

        auth = None
        headers: dict[str, str] = {}

        if provider == "twilio":
            # Twilio recording API expects .mp3 or .wav extension appended
            if not url.endswith((".mp3", ".wav", ".MP3", ".WAV")):
                url = url + ".mp3"
            if self.twilio_account_sid and self.twilio_auth_token:
                auth = (self.twilio_account_sid, self.twilio_auth_token)
            else:
                logger.warning(
                    "Downloading Twilio recording without TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN: %s",
                    sanitized_url,
                )

        elif provider == "boston_archive":
            # Boston Archive rules:
            # - DO NOT modify the URL.
            # - DO NOT strip query params (e.g., ?f=...).
            # - DO NOT append .mp3.
            # - Enforce X-Api-Key ONLY for grabaciones.bostonmedical.info.
            if not self.boston_recording_api_key:
                raise AudioDownloadError(
                    "BOSTON_RECORDING_API_KEY no configurada. Configure la variable de entorno para acceder al archivo de grabaciones de Boston Medical.",
                    status_code=502,
                    stage="download_audio",
                )
            headers["X-Api-Key"] = self.boston_recording_api_key

        elif provider == "cloudtalk":
            if self.cloudtalk_service.is_configured:
                auth = (self.cloudtalk_service.api_key_id, self.cloudtalk_service.api_key_secret)

        logger.info(
            "Initiating audio download: provider=%s, domain=%s, url=%s",
            provider,
            parsed_domain,
            sanitized_url,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                kwargs: dict[str, Any] = {}
                if auth:
                    kwargs["auth"] = auth
                if headers:
                    kwargs["headers"] = headers

                response = await client.get(url, **kwargs)

        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            logger.error("Connection error downloading audio from %s: %s", sanitized_url, type(e).__name__)
            raise AudioDownloadError(
                f"No se pudo conectar con el servidor de grabaciones ({parsed_domain}): error de red o timeout.",
                status_code=504,
                stage="download_audio",
            )
        except httpx.TimeoutException as e:
            logger.error("Timeout downloading audio from %s: %s", sanitized_url, type(e).__name__)
            raise AudioDownloadError(
                f"Timeout al descargar la grabación desde {parsed_domain} tras {timeout}s.",
                status_code=504,
                stage="download_audio",
            )
        except Exception as e:
            logger.error("Unexpected error requesting audio from %s: %s", sanitized_url, type(e).__name__)
            raise AudioDownloadError(
                f"Error al descargar grabación desde {parsed_domain}: {type(e).__name__}",
                status_code=502,
                stage="download_audio",
            )

        # ── 1. Check for SSO / Microsoft Login Redirects ──────────────────────
        final_url_str = str(response.url).lower()
        if "login.microsoftonline.com" in final_url_str or "login.live.com" in final_url_str:
            logger.error(
                "Recording request redirected to Microsoft Login: provider=%s, final_url=%s",
                provider,
                final_url_str.split("?")[0],
            )
            raise AudioDownloadError(
                "El servidor de grabaciones redirigió a la pantalla de login de Microsoft (302 SSO). "
                "Compruebe que BOSTON_RECORDING_API_KEY esté correctamente configurada y autorizada.",
                status_code=502,
                stage="download_audio",
            )

        # Check response history for SSO redirects
        if hasattr(response, "history") and response.history:
            for hist in response.history:
                loc = str(hist.headers.get("location", "")).lower()
                if "login.microsoftonline.com" in loc or "login.live.com" in loc:
                    logger.error("Redirect history contains Microsoft Login: %s", loc.split("?")[0])
                    raise AudioDownloadError(
                        "El servidor de grabaciones redirigió a la pantalla de login de Microsoft (302 SSO). "
                        "Compruebe que BOSTON_RECORDING_API_KEY esté correctamente configurada y autorizada.",
                        status_code=502,
                        stage="download_audio",
                    )

        # ── 2. Handle HTTP Status Codes ───────────────────────────────────────
        if response.status_code == 401:
            logger.error("HTTP 401 Unauthorized downloading audio from %s", sanitized_url)
            raise AudioDownloadError(
                f"Error 401: No autorizado al descargar la grabación desde {parsed_domain}. Compruebe credenciales / X-Api-Key.",
                status_code=502,
                stage="download_audio",
            )
        if response.status_code == 403:
            logger.error("HTTP 403 Forbidden downloading audio from %s", sanitized_url)
            raise AudioDownloadError(
                f"Error 403: Acceso denegado al descargar la grabación desde {parsed_domain}. Compruebe permisos de la API key.",
                status_code=502,
                stage="download_audio",
            )
        if response.status_code == 404:
            logger.warning("HTTP 404 Not Found downloading audio from %s", sanitized_url)
            raise AudioDownloadError(
                f"Error 404: Grabación no encontrada en el servidor remoto ({parsed_domain}).",
                status_code=404,
                stage="download_audio",
            )
        if response.status_code == 429:
            logger.warning("HTTP 429 Too Many Requests downloading audio from %s", sanitized_url)
            raise AudioDownloadError(
                f"Error 429: Límite de peticiones excedido en el servidor remoto ({parsed_domain}).",
                status_code=429,
                stage="download_audio",
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error("HTTP error %s downloading audio from %s", response.status_code, sanitized_url)
            raise AudioDownloadError(
                f"Error HTTP {response.status_code} al descargar grabación desde {parsed_domain}.",
                status_code=502,
                stage="download_audio",
            )

        content = response.content
        content_type = response.headers.get("content-type", "").lower().split(";")[0].strip()

        # ── 3. Content-Type and Payload Validation ────────────────────────────
        # Detect and reject HTML responses
        content_preview = content[:256].strip().lower()
        if (
            "text/html" in content_type
            or content_preview.startswith(b"<!doctype")
            or content_preview.startswith(b"<html")
        ):
            logger.error(
                "Invalid audio content received (HTML): provider=%s, content_type=%s, preview=%s",
                provider,
                content_type,
                content_preview[:80],
            )
            raise AudioDownloadError(
                f"Tipo de contenido inválido ({content_type}). Se esperaba un archivo de audio, "
                "pero el servidor devolvió una página HTML (posible portal de autenticación o error web).",
                status_code=502,
                stage="download_audio",
            )

        # Reject application/octet-stream unless binary header confirms RIFF/WAVE
        if content_type == "application/octet-stream" and not is_valid_wav_bytes(content):
            logger.error(
                "Rejected non-WAV application/octet-stream payload: provider=%s, size_bytes=%d, url=%s",
                provider,
                len(content),
                sanitized_url,
            )
            raise AudioDownloadError(
                "Tipo de contenido application/octet-stream rechazado: la cabecera binaria no corresponde a un archivo WAV válido (RIFF/WAVE).",
                status_code=502,
                stage="download_audio",
            )

        if content_type and content_type not in ALLOWED_AUDIO_CONTENT_TYPES:
            logger.error(
                "Unsupported audio content-type: provider=%s, content_type=%s, url=%s",
                provider,
                content_type,
                sanitized_url,
            )
            raise AudioDownloadError(
                f"Tipo de contenido no soportado ({content_type}) al descargar grabación.",
                status_code=502,
                stage="download_audio",
            )

        # Validate minimum audio size
        if len(content) < 100:
            logger.error("Audio content too small (%d bytes) from %s", len(content), sanitized_url)
            raise AudioDownloadError(
                f"El archivo de audio descargado está vacío o truncado ({len(content)} bytes).",
                status_code=502,
                stage="download_audio",
            )

        logger.info(
            "Audio downloaded successfully: provider=%s, domain=%s, status=%d, content_type=%s, size_bytes=%d, url=%s",
            provider,
            parsed_domain,
            response.status_code,
            content_type,
            len(content),
            sanitized_url,
        )

        return content

    async def get_recording_metadata(self, recording_url: str) -> dict[str, Any] | None:
        """
        Fetch recording metadata (Twilio only).
        Returns None for non-Twilio URLs.
        """
        if not recording_url or not self.is_twilio_url(recording_url):
            return None

        base_url = recording_url
        for ext in (".mp3", ".wav", ".MP3", ".WAV"):
            if base_url.endswith(ext):
                base_url = base_url[:-len(ext)]
                break
        if not base_url.endswith(".json"):
            base_url = base_url + ".json"

        auth = (self.twilio_account_sid, self.twilio_auth_token) if (self.twilio_account_sid and self.twilio_auth_token) else None
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                kwargs: dict[str, Any] = {}
                if auth:
                    kwargs["auth"] = auth
                response = await client.get(base_url, **kwargs)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.warning(
                "Failed to fetch Twilio recording metadata from %s: %s",
                sanitize_recording_url(base_url),
                type(e).__name__,
            )
            return None

