"""
Tests for CloudTalk and Boston Medical Archive audio downloading and call resolution.
Covers:
1. AudioDownloadService (Twilio Basic Auth, Boston Archive X-Api-Key, SSO redirect prevention, HTML rejection, URL sanitization)
2. HubSpotService (Direct lookup, fallback to search_call_by_external_id for CloudTalk calls)
3. CloudTalkService (API client, recording URL resolution, external ID bridge)
4. Transcription analysis pipeline integration with Boston Archive recordings
"""
import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_cloudtalk_test.db"

from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from app.config import Settings
from app.services.audio_download_service import (
    AudioDownloadService,
    AudioDownloadError,
    sanitize_recording_url,
)
from app.services.cloudtalk_service import CloudTalkService
from app.services.hubspot_service import HubSpotService


class TestAudioDownloadService(unittest.IsolatedAsyncioTestCase):

    def test_sanitize_recording_url(self):
        # Empty string
        self.assertEqual(sanitize_recording_url(""), "")

        # Twilio URL without query
        url1 = "https://api.twilio.com/2010-04-01/Accounts/AC123/Recordings/RE456"
        self.assertEqual(sanitize_recording_url(url1), url1)

        # Boston Archive URL with query parameter 'f'
        url2 = "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3"
        sanitized2 = sanitize_recording_url(url2)
        self.assertIn("grabaciones.bostonmedical.info", sanitized2)
        self.assertIn("f=%2A%2A%2A", sanitized2)
        self.assertNotIn("call_1319865630.mp3", sanitized2)

        # URL with token and secret
        url3 = "https://example.com/audio?token=supersecret12345&user=john"
        sanitized3 = sanitize_recording_url(url3)
        self.assertIn("token=%2A%2A%2A", sanitized3)
        self.assertNotIn("supersecret12345", sanitized3)

    def test_provider_detection(self):
        service = AudioDownloadService()
        self.assertEqual(
            service.detect_provider("https://api.twilio.com/2010-04-01/Accounts/AC/Recordings/RE123"),
            "twilio",
        )
        self.assertEqual(
            service.detect_provider("https://grabaciones.bostonmedical.info/boston/file.php?f=123"),
            "boston_archive",
        )
        self.assertEqual(
            service.detect_provider("https://my.cloudtalk.io/api/recordings/123"),
            "cloudtalk",
        )
        self.assertEqual(
            service.detect_provider("https://cdn.example.com/audio/call.mp3"),
            "direct",
        )

    async def test_download_empty_url_raises(self):
        service = AudioDownloadService()
        with self.assertRaises(AudioDownloadError) as ctx:
            await service.download_audio("")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_download_twilio_appends_mp3_and_uses_basic_auth(self):
        service = AudioDownloadService()
        service.twilio_account_sid = "test_sid"
        service.twilio_auth_token = "test_token"

        fake_audio = b"\xff\xfb\x90\x00" + b"\x00" * 200  # > 100 bytes

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.url = "https://api.twilio.com/2010-04-01/Accounts/AC/Recordings/RE123.mp3"
            mock_resp.headers = {"content-type": "audio/mpeg"}
            mock_resp.content = fake_audio
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            result = await service.download_audio("https://api.twilio.com/2010-04-01/Accounts/AC/Recordings/RE123")
            self.assertEqual(result, fake_audio)

            # Check that .mp3 was appended and auth was passed
            call_args = mock_get.call_args
            self.assertTrue(call_args[0][0].endswith(".mp3"))
            self.assertEqual(call_args[1]["auth"], ("test_sid", "test_token"))

    async def test_download_boston_archive_missing_api_key_raises(self):
        service = AudioDownloadService()
        service.boston_recording_api_key = ""

        with self.assertRaises(AudioDownloadError) as ctx:
            await service.download_audio("https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("BOSTON_RECORDING_API_KEY no configurada", ctx.exception.message)

    async def test_download_boston_archive_success(self):
        service = AudioDownloadService()
        service.boston_recording_api_key = "secret_archive_key"

        fake_audio = b"ID3\x04\x00\x00\x00\x00" + b"\x00" * 300
        target_url = "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.url = target_url
            mock_resp.headers = {"content-type": "audio/mpeg"}
            mock_resp.content = fake_audio
            mock_resp.raise_for_status = MagicMock()
            mock_resp.history = []
            mock_get.return_value = mock_resp

            result = await service.download_audio(target_url)
            self.assertEqual(result, fake_audio)

            # URL and query params must NOT be altered
            mock_get.assert_called_once()
            call_url = mock_get.call_args[0][0]
            self.assertEqual(call_url, target_url)

            # Header X-Api-Key must be present
            headers = mock_get.call_args[1].get("headers", {})
            self.assertEqual(headers.get("X-Api-Key"), "secret_archive_key")

    async def test_download_boston_archive_detects_microsoft_sso_redirect(self):
        service = AudioDownloadService()
        service.boston_recording_api_key = "test_key"

        target_url = "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.url = "https://login.microsoftonline.com/common/oauth2/authorize?client_id=123"
            mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
            mock_resp.content = b"<html><head><title>Sign In</title></head><body>Login</body></html>"
            mock_resp.history = []
            mock_get.return_value = mock_resp

            with self.assertRaises(AudioDownloadError) as ctx:
                await service.download_audio(target_url)

            self.assertEqual(ctx.exception.status_code, 502)
            self.assertIn("login de Microsoft", ctx.exception.message)

    async def test_download_boston_archive_rejects_html_content(self):
        service = AudioDownloadService()
        service.boston_recording_api_key = "test_key"

        target_url = "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.url = target_url
            mock_resp.headers = {"content-type": "text/html"}
            mock_resp.content = b"<!DOCTYPE html><html><body>Error</body></html>" + b"x" * 200
            mock_resp.history = []
            mock_get.return_value = mock_resp

            with self.assertRaises(AudioDownloadError) as ctx:
                await service.download_audio(target_url)

            self.assertEqual(ctx.exception.status_code, 502)
            self.assertIn("Se esperaba un archivo de audio, pero el servidor devolvi\xf3 una p\xe1gina HTML", ctx.exception.message)

    async def test_download_boston_archive_rejects_tiny_content(self):
        service = AudioDownloadService()
        service.boston_recording_api_key = "test_key"

        target_url = "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.url = target_url
            mock_resp.headers = {"content-type": "audio/mpeg"}
            mock_resp.content = b"empty"  # only 5 bytes
            mock_resp.history = []
            mock_get.return_value = mock_resp

            with self.assertRaises(AudioDownloadError) as ctx:
                await service.download_audio(target_url)

            self.assertEqual(ctx.exception.status_code, 502)
            self.assertIn("est\xe1 vac\xedo o truncado", ctx.exception.message)

    async def test_download_status_code_mappings(self):
        service = AudioDownloadService()
        service.boston_recording_api_key = "test_key"
        target_url = "https://grabaciones.bostonmedical.info/boston/file.php?f=123"

        # 401 Unauthorized
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_resp.url = target_url
            mock_get.return_value = mock_resp
            with self.assertRaises(AudioDownloadError) as ctx:
                await service.download_audio(target_url)
            self.assertEqual(ctx.exception.status_code, 502)
            self.assertIn("401", ctx.exception.message)

        # 403 Forbidden
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 403
            mock_resp.url = target_url
            mock_get.return_value = mock_resp
            with self.assertRaises(AudioDownloadError) as ctx:
                await service.download_audio(target_url)
            self.assertEqual(ctx.exception.status_code, 502)
            self.assertIn("403", ctx.exception.message)

        # 404 Not Found
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.url = target_url
            mock_get.return_value = mock_resp
            with self.assertRaises(AudioDownloadError) as ctx:
                await service.download_audio(target_url)
            self.assertEqual(ctx.exception.status_code, 404)
            self.assertIn("404", ctx.exception.message)


class TestHubSpotServiceCallResolution(unittest.IsolatedAsyncioTestCase):

    async def test_get_call_direct_hubspot_id(self):
        service = HubSpotService(is_demo=False)
        service.token = "fake_token"

        raw_hs_response = {
            "id": "523697956049",
            "properties": {
                "hs_call_direction": "INBOUND",
                "hs_call_duration": "120000",
                "hs_call_recording_url": "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3",
                "hs_timestamp": "2026-09-10T10:00:00Z",
                "hs_createdate": "2026-09-10T10:05:00Z",
                "hubspot_owner_id": "999888",
                "hs_call_external_id": "1319865630",
            },
            "associations": {},
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = raw_hs_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            call_data = await service.get_call("523697956049", is_demo=False)

            self.assertEqual(call_data["call_id"], "523697956049")
            self.assertEqual(call_data["cloudtalk_id"], "1319865630")
            self.assertEqual(call_data["recording_url"], "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3")
            self.assertEqual(call_data["call_direction"], "INBOUND")

    async def test_get_call_fallback_to_external_id_search(self):
        service = HubSpotService(is_demo=False)
        service.token = "fake_token"

        # Direct GET /crm/v3/objects/calls/1319865630 returns 404
        direct_resp = MagicMock()
        direct_resp.status_code = 404
        direct_resp.url = "https://api.hubapi.com/crm/v3/objects/calls/1319865630"

        # Search POST /crm/v3/objects/calls/search returns 200 with matching call
        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = {
            "total": 1,
            "results": [
                {
                    "id": "527583176938",
                    "properties": {
                        "hs_call_direction": "OUTBOUND",
                        "hs_call_duration": "45000",
                        "hs_call_recording_url": "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3",
                        "hs_timestamp": "2026-09-10T11:00:00Z",
                        "hubspot_owner_id": "111222",
                        "hs_call_external_id": "1319865630",
                    },
                    "associations": {},
                }
            ],
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=direct_resp):
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=search_resp):
                call_data = await service.get_call("1319865630", is_demo=False)

                self.assertIsNotNone(call_data)
                self.assertEqual(call_data["call_id"], "527583176938")
                self.assertEqual(call_data["cloudtalk_id"], "1319865630")
                self.assertEqual(
                    call_data["recording_url"],
                    "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3",
                )


class TestCloudTalkService(unittest.IsolatedAsyncioTestCase):

    def test_is_configured_property(self):
        service = CloudTalkService()
        service.api_key_id = ""
        service.api_key_secret = ""
        self.assertFalse(service.is_configured)

        service.api_key_id = "key123"
        service.api_key_secret = "secret456"
        self.assertTrue(service.is_configured)

    async def test_get_call_unconfigured_returns_none(self):
        service = CloudTalkService()
        service.api_key_id = ""
        service.api_key_secret = ""
        res = await service.get_call("1319865630")
        self.assertIsNone(res)

    async def test_get_recording_url_configured(self):
        service = CloudTalkService()
        service.api_key_id = "test_key"
        service.api_key_secret = "test_secret"

        call_payload = {
            "Call": {
                "id": 1319865630,
                "recording_url": "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3",
            }
        }

        with patch.object(service, "get_call", new_callable=AsyncMock, return_value=call_payload):
            url = await service.get_recording_url("1319865630")
            self.assertEqual(
                url,
                "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3",
            )


class TestTranscriptionAnalysisServiceIntegration(unittest.IsolatedAsyncioTestCase):

    async def test_pipeline_downloads_and_analyzes_boston_archive_call(self):
        from app.services.transcription_analysis_service import analyze_transcription_pipeline

        fake_audio = b"ID3\x04\x00\x00\x00\x00" + b"\x00" * 300
        fake_transcription = "Hola, llamo de Boston Medical Group para su consulta..."
        fake_ai_output = '{"tipo_llamada": "PRIMERA_VISITA", "score": 95, "resumen": "Llamada correcta"}'

        mock_db = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = ["cita", "confirmacion", "cancelacion", "reagendo", "falta", "otros", "PRIMERA_VISITA"]
        mock_exec_res = MagicMock()
        mock_exec_res.scalars.return_value = mock_scalars
        mock_exec_res.all.return_value = []
        mock_db.execute.return_value = mock_exec_res

        mock_analysis_record = MagicMock()
        mock_analysis_record.analysis_id = "test-uuid-1234"
        mock_analysis_record.service_id = None
        mock_analysis_record.company_id = 1
        mock_analysis_record.created_at = datetime.now(timezone.utc)

        with patch("app.core.side_effects.resolve_company_demo_status", return_value=(1, False)):
            with patch("app.services.hubspot_service.HubSpotService.get_call", new_callable=AsyncMock) as mock_hs:
                mock_hs.return_value = {
                    "call_id": "527583176938",
                    "cloudtalk_id": "1319865630",
                    "recording_url": "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3",
                }
                with patch("app.services.audio_download_service.AudioDownloadService.download_audio", new_callable=AsyncMock) as mock_dl:
                    mock_dl.return_value = fake_audio

                    with patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_tr:
                        mock_tr.return_value = {"text": fake_transcription}

                        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_ai:
                            mock_ai.return_value = fake_ai_output

                            with patch("app.services.transcription_analysis_service.save_analysis", new_callable=AsyncMock) as mock_save:
                                with patch("app.services.criteria_service.get_active_criteria", new_callable=AsyncMock, return_value=[]):
                                    mock_save.return_value = mock_analysis_record

                                    res = await analyze_transcription_pipeline(
                                        db=mock_db,
                                        call_id="1319865630",
                                        custom_prompt_text="Prompt de prueba para análisis.",
                                    )

                                    self.assertTrue(res.get("ok"))
                                    self.assertEqual(res.get("call_id"), "1319865630")
                                    self.assertEqual(res.get("result", {}).get("tipo_llamada"), "PRIMERA_VISITA")

                                    # Verify downloader was invoked with exact Boston Archive URL
                                    mock_dl.assert_called_once_with(
                                        "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3"
                                    )

    async def test_pipeline_handles_download_audio_failure(self):
        from app.services.transcription_analysis_service import analyze_transcription_pipeline

        mock_db = AsyncMock()

        with patch("app.core.side_effects.resolve_company_demo_status", return_value=(1, False)):
            with patch("app.services.hubspot_service.HubSpotService.get_call", new_callable=AsyncMock) as mock_hs:
                mock_hs.return_value = {
                    "call_id": "527583176938",
                    "cloudtalk_id": "1319865630",
                    "recording_url": "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3",
                }
                with patch("app.services.audio_download_service.AudioDownloadService.download_audio", new_callable=AsyncMock) as mock_dl:
                    mock_dl.side_effect = AudioDownloadError(
                        "El servidor de grabaciones redirigió a la pantalla de login de Microsoft (302 SSO).",
                        status_code=502,
                        stage="download_audio",
                    )

                    res = await analyze_transcription_pipeline(
                        db=mock_db,
                        call_id="1319865630",
                        custom_prompt_text="Prompt de prueba.",
                    )

                    self.assertFalse(res.get("ok"))
                    self.assertEqual(res.get("stage"), "download_audio")
                    self.assertIn("login de Microsoft", res.get("error_message"))
