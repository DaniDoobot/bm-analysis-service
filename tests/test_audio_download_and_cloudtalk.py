"""
Tests for CloudTalk primary recording source, Boston Medical Archive (IA España) fallback,
and Twilio historical compatibility.
Covers:
1. CloudTalk metadata: GET /calls/index.json?call_id=123 -> finds call
2. Old incorrect endpoint: code no longer depends on /calls/{id}.json or Cdr.recording_link
3. CloudTalk recording: 200 + application/octet-stream + RIFF/WAVE -> accepted
4. Non-WAV application/octet-stream -> rejected
5. CloudTalk 404 + hs_call_recording_url -> fallback to IA España
6. CloudTalk timeout + fallback -> works
7. CloudTalk 429 -> controlled and without infinite retry loop (+ fallback when URL exists)
8. BOSTON_RECORDING_API_KEY -> sent ONLY to grabaciones.bostonmedical.info
9. Historical Twilio -> continues working
10. Zero credentials leaked in logs or exceptions
"""
import logging
import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_cloudtalk_test.db"

from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.services.audio_download_service import (
    AudioDownloadService,
    AudioDownloadError,
    detect_audio_format,
    sanitize_recording_url,
)
from app.services.cloudtalk_service import CloudTalkService, is_valid_wav_bytes
from app.services.hubspot_service import HubSpotService


FAKE_WAV_BYTES = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 200
FAKE_MP3_BYTES = b"ID3\x04\x00\x00\x00\x00" + b"\x00" * 300


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

    def test_provider_detection_and_wav_detection(self):
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
            service.detect_provider("https://my.cloudtalk.io/api/calls/recording/123.json"),
            "cloudtalk",
        )
        self.assertEqual(
            service.detect_provider("https://cdn.example.com/audio/call.mp3"),
            "direct",
        )
        self.assertTrue(is_valid_wav_bytes(FAKE_WAV_BYTES))
        self.assertFalse(is_valid_wav_bytes(FAKE_MP3_BYTES))
        self.assertEqual(detect_audio_format(FAKE_WAV_BYTES, None), "wav")
        self.assertEqual(detect_audio_format(FAKE_MP3_BYTES, "https://grabaciones.bostonmedical.info/boston/file.php?f=call.mp3"), "mp3")

    async def test_download_empty_url_raises(self):
        service = AudioDownloadService()
        with self.assertRaises(AudioDownloadError) as ctx:
            await service.download_audio("")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_case_9_download_twilio_appends_mp3_and_uses_basic_auth(self):
        """Case 9: Historical Twilio recording download continues working with Basic Auth and .mp3 suffix."""
        service = AudioDownloadService()
        service.twilio_account_sid = "test_sid"
        service.twilio_auth_token = "test_token"
        service.boston_recording_api_key = "boston_secret_key_should_not_be_sent"

        fake_audio = b"\xff\xfb\x90\x00" + b"\x00" * 200

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

            # Check that .mp3 was appended, Basic Auth was passed, and X-Api-Key was NOT sent
            call_args = mock_get.call_args
            self.assertTrue(call_args[0][0].endswith(".mp3"))
            self.assertEqual(call_args[1]["auth"], ("test_sid", "test_token"))
            self.assertNotIn("X-Api-Key", call_args[1].get("headers", {}))

    async def test_download_boston_archive_missing_api_key_raises(self):
        service = AudioDownloadService()
        service.boston_recording_api_key = ""

        with self.assertRaises(AudioDownloadError) as ctx:
            await service.download_audio("https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("BOSTON_RECORDING_API_KEY no configurada", ctx.exception.message)

    async def test_case_8_boston_api_key_only_sent_to_boston_archive(self):
        """Case 8: BOSTON_RECORDING_API_KEY is sent ONLY to grabaciones.bostonmedical.info, never to CloudTalk/Twilio/direct."""
        service = AudioDownloadService()
        service.boston_recording_api_key = "secret_archive_key"

        target_url = "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.mp3"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.url = target_url
            mock_resp.headers = {"content-type": "audio/mpeg"}
            mock_resp.content = FAKE_MP3_BYTES
            mock_resp.raise_for_status = MagicMock()
            mock_resp.history = []
            mock_get.return_value = mock_resp

            result = await service.download_audio(target_url)
            self.assertEqual(result, FAKE_MP3_BYTES)

            call_url = mock_get.call_args[0][0]
            self.assertEqual(call_url, target_url)
            headers = mock_get.call_args[1].get("headers", {})
            self.assertEqual(headers.get("X-Api-Key"), "secret_archive_key")

        # Direct CDN URL must NOT receive X-Api-Key
        direct_url = "https://cdn.example.com/audio/call_123.mp3"
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get_direct:
            mock_resp2 = MagicMock()
            mock_resp2.status_code = 200
            mock_resp2.url = direct_url
            mock_resp2.headers = {"content-type": "audio/mpeg"}
            mock_resp2.content = FAKE_MP3_BYTES
            mock_resp2.raise_for_status = MagicMock()
            mock_resp2.history = []
            mock_get_direct.return_value = mock_resp2

            await service.download_audio(direct_url)
            direct_headers = mock_get_direct.call_args[1].get("headers", {})
            self.assertNotIn("X-Api-Key", direct_headers)

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
            self.assertIn("Se esperaba un archivo de audio, pero el servidor devolvió una página HTML", ctx.exception.message)

    async def test_case_4_rejects_non_wav_application_octet_stream(self):
        """Case 4: Arbitrary application/octet-stream without RIFF/WAVE header is rejected."""
        service = AudioDownloadService()
        service.boston_recording_api_key = "test_key"
        target_url = "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1319865630.bin"

        # Non-WAV binary payload with application/octet-stream
        arbitrary_binary = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.url = target_url
            mock_resp.headers = {"content-type": "application/octet-stream"}
            mock_resp.content = arbitrary_binary
            mock_resp.raise_for_status = MagicMock()
            mock_resp.history = []
            mock_get.return_value = mock_resp

            with self.assertRaises(AudioDownloadError) as ctx:
                await service.download_audio(target_url)

            self.assertEqual(ctx.exception.status_code, 502)
            self.assertIn("RIFF/WAVE", ctx.exception.message)

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
            self.assertIn("está vacío o truncado", ctx.exception.message)

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

    async def test_case_5_cloudtalk_404_falls_back_to_boston_archive(self):
        """Case 5: CloudTalk 404 + hs_call_recording_url -> falls back to IA España (grabaciones.bostonmedical.info)."""
        ct_service = CloudTalkService()
        ct_service.api_key_id = "ct_id"
        ct_service.api_key_secret = "ct_secret"

        service = AudioDownloadService(cloudtalk_service=ct_service)
        service.boston_recording_api_key = "boston_key"
        fallback_url = "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1320873716.mp3"

        ct_404_resp = MagicMock()
        ct_404_resp.status_code = 404
        ct_404_resp.headers = {}

        boston_200_resp = MagicMock()
        boston_200_resp.status_code = 200
        boston_200_resp.url = fallback_url
        boston_200_resp.headers = {"content-type": "audio/mpeg"}
        boston_200_resp.content = FAKE_MP3_BYTES
        boston_200_resp.raise_for_status = MagicMock()
        boston_200_resp.history = []

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=[ct_404_resp, boston_200_resp]) as mock_get:
            result = await service.download_audio(fallback_url, cloudtalk_id="1320873716")
            self.assertEqual(result, FAKE_MP3_BYTES)
            self.assertEqual(mock_get.call_count, 2)

            # First attempt was CloudTalk direct recording endpoint
            first_url = mock_get.call_args_list[0][0][0]
            self.assertEqual(first_url, "https://my.cloudtalk.io/api/calls/recording/1320873716.json")
            self.assertNotIn("X-Api-Key", mock_get.call_args_list[0][1].get("headers", {}))

            # Second attempt was IA España fallback with X-Api-Key
            second_url = mock_get.call_args_list[1][0][0]
            self.assertEqual(second_url, fallback_url)
            self.assertEqual(mock_get.call_args_list[1][1]["headers"]["X-Api-Key"], "boston_key")

    async def test_case_6_cloudtalk_timeout_falls_back_to_boston_archive(self):
        """Case 6: CloudTalk timeout + hs_call_recording_url -> falls back to IA España."""
        ct_service = CloudTalkService()
        ct_service.api_key_id = "ct_id"
        ct_service.api_key_secret = "ct_secret"

        service = AudioDownloadService(cloudtalk_service=ct_service)
        service.boston_recording_api_key = "boston_key"
        fallback_url = "https://grabaciones.bostonmedical.info/boston/file.php?f=call_1320870665.mp3"

        boston_200_resp = MagicMock()
        boston_200_resp.status_code = 200
        boston_200_resp.url = fallback_url
        boston_200_resp.headers = {"content-type": "audio/mpeg"}
        boston_200_resp.content = FAKE_MP3_BYTES
        boston_200_resp.raise_for_status = MagicMock()
        boston_200_resp.history = []

        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=[httpx.ReadTimeout("CloudTalk timed out"), boston_200_resp],
        ) as mock_get:
            result = await service.download_audio(fallback_url, cloudtalk_id="1320870665")
            self.assertEqual(result, FAKE_MP3_BYTES)
            self.assertEqual(mock_get.call_count, 2)


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

    async def test_case_1_and_2_get_call_uses_index_json_and_not_calls_id_json(self):
        """
        Case 1 & 2:
        - GET /calls/index.json?call_id=123 finds the call.
        - Code no longer calls /calls/{id}.json and does not use Cdr.recording_link as audio URL.
        """
        service = CloudTalkService()
        service.api_key_id = "test_key_id"
        service.api_key_secret = "test_key_secret"

        index_payload = {
            "responseData": {
                "itemsCount": 1,
                "data": [
                    {
                        "Cdr": {
                            "id": "123",
                            "type": "incoming",
                            "billsec": "153",
                            "talking_time": "152",
                            "recorded": True,
                            "started_at": "2026-10-16T08:36:20+02:00",
                            "recording_link": "https://analytics.cloudtalk.io/call-details/123",
                        },
                        "Agent": {"id": "99", "fullname": "Agente Test"},
                    }
                ],
            }
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = index_payload
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            call_meta = await service.get_call("123")
            self.assertIsNotNone(call_meta)
            self.assertEqual(call_meta["id"], "123")
            self.assertTrue(call_meta["recorded"])
            self.assertEqual(call_meta["billsec"], "153")

            # Verify exact URL and params: /calls/index.json?call_id=123 (NOT /calls/123.json)
            called_url = mock_get.call_args[0][0]
            called_params = mock_get.call_args[1].get("params")
            self.assertEqual(called_url, "https://my.cloudtalk.io/api/calls/index.json")
            self.assertNotIn("/calls/123.json", called_url)
            self.assertEqual(called_params, {"call_id": "123"})

            # Verify get_recording_url returns direct API recording endpoint, never analytics.cloudtalk.io
            rec_url = await service.get_recording_url("123")
            self.assertEqual(rec_url, "https://my.cloudtalk.io/api/calls/recording/123.json")
            self.assertNotIn("analytics.cloudtalk.io", rec_url)

    async def test_case_3_cloudtalk_download_recording_accepts_octet_stream_riff_wave(self):
        """Case 3: CloudTalk recording returning 200 + application/octet-stream + RIFF/WAVE is accepted."""
        service = CloudTalkService()
        service.api_key_id = "test_key_id"
        service.api_key_secret = "test_key_secret"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {
                "content-type": "application/octet-stream",
                "content-disposition": 'attachment; filename="1320873716.wav"',
            }
            mock_resp.content = FAKE_WAV_BYTES
            mock_get.return_value = mock_resp

            audio_bytes = await service.download_recording("1320873716")
            self.assertEqual(audio_bytes, FAKE_WAV_BYTES)

            called_url = mock_get.call_args[0][0]
            self.assertEqual(called_url, "https://my.cloudtalk.io/api/calls/recording/1320873716.json")
            self.assertEqual(mock_get.call_args[1]["auth"], ("test_key_id", "test_key_secret"))

    async def test_case_4_cloudtalk_download_recording_rejects_non_wav_octet_stream(self):
        """Case 4: CloudTalk recording returning application/octet-stream without RIFF/WAVE header is rejected."""
        service = CloudTalkService()
        service.api_key_id = "test_key_id"
        service.api_key_secret = "test_key_secret"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {"content-type": "application/octet-stream"}
            mock_resp.content = b'{"error": "not a wav file"}' + b"x" * 100
            mock_get.return_value = mock_resp

            with self.assertRaises(AudioDownloadError) as ctx:
                await service.download_recording("1320873716")
            self.assertEqual(ctx.exception.status_code, 502)
            self.assertIn("RIFF/WAVE", ctx.exception.message)

    async def test_case_7_cloudtalk_429_controlled_without_infinite_loop(self):
        """Case 7: CloudTalk HTTP 429 is handled in a controlled manner without an infinite retry loop."""
        service = CloudTalkService()
        service.api_key_id = "test_key_id"
        service.api_key_secret = "test_key_secret"

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "60", "x-cloudtalkapi-limit": "60"}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=resp_429) as mock_get:
            with self.assertRaises(AudioDownloadError) as ctx:
                await service.download_recording("1320873716")
            self.assertEqual(ctx.exception.status_code, 429)
            # Retry-After=60 > 5s max reasonable wait, so it fails immediately after 1 request (no loop)
            self.assertEqual(mock_get.call_count, 1)

        # Also test small Retry-After=0.01: retries at most ONCE, then raises 429 if still 429
        resp_429_short = MagicMock()
        resp_429_short.status_code = 429
        resp_429_short.headers = {"Retry-After": "0.01"}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=resp_429_short) as mock_get_short:
            with self.assertRaises(AudioDownloadError) as ctx2:
                await service.download_recording("1320873716")
            self.assertEqual(ctx2.exception.status_code, 429)
            self.assertEqual(mock_get_short.call_count, 2)

    async def test_case_10_no_credentials_leaked_in_logs_or_exceptions(self):
        """Case 10: No API keys, secrets, or tokens appear in log records or raised exceptions."""
        secret_boston = "SUPER_SECRET_BOSTON_KEY_99999"
        secret_ct_id = "SUPER_SECRET_CT_ID_88888"
        secret_ct_sec = "SUPER_SECRET_CT_SECRET_77777"
        secret_url_hash = "SECRET_HASH_TOKEN_66666"

        ct_service = CloudTalkService()
        ct_service.api_key_id = secret_ct_id
        ct_service.api_key_secret = secret_ct_sec

        downloader = AudioDownloadService(cloudtalk_service=ct_service)
        downloader.boston_recording_api_key = secret_boston

        fallback_url = f"https://grabaciones.bostonmedical.info/r/RE123.mp3?f={secret_url_hash}"

        ct_403 = MagicMock()
        ct_403.status_code = 403
        ct_403.headers = {}

        boston_401 = MagicMock()
        boston_401.status_code = 401
        boston_401.url = fallback_url
        boston_401.headers = {}
        boston_401.history = []

        log_messages: list[str] = []

        class ListHandler(logging.Handler):
            def emit(self, record):
                log_messages.append(self.format(record))

        handler = ListHandler()
        root_logger = logging.getLogger("app.services")
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)

        try:
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=[ct_403, boston_401]):
                with self.assertRaises(AudioDownloadError) as ctx:
                    await downloader.download_audio(fallback_url, cloudtalk_id="1320873716")

            exc_text = str(ctx.exception.message)
            combined_logs = "\n".join(log_messages)

            for secret in (secret_boston, secret_ct_id, secret_ct_sec, secret_url_hash):
                self.assertNotIn(secret, exc_text)
                self.assertNotIn(secret, combined_logs)
        finally:
            root_logger.removeHandler(handler)


class TestTranscriptionAnalysisServiceIntegration(unittest.IsolatedAsyncioTestCase):

    async def test_pipeline_prioritizes_cloudtalk_and_analyzes_call(self):
        from app.services.transcription_analysis_service import analyze_transcription_pipeline

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
                    "call_id": "539511971825",
                    "cloudtalk_id": "1320873716",
                    "recording_url": None,
                }
                with patch("app.services.audio_download_service.AudioDownloadService.download_audio", new_callable=AsyncMock) as mock_dl:
                    mock_dl.return_value = FAKE_WAV_BYTES

                    with patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_tr:
                        mock_tr.return_value = {"text": fake_transcription}

                        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_ai:
                            mock_ai.return_value = fake_ai_output

                            with patch("app.services.transcription_analysis_service.save_analysis", new_callable=AsyncMock) as mock_save:
                                with patch("app.services.criteria_service.get_active_criteria", new_callable=AsyncMock, return_value=[]):
                                    mock_save.return_value = mock_analysis_record

                                    res = await analyze_transcription_pipeline(
                                        db=mock_db,
                                        call_id="1320873716",
                                        custom_prompt_text="Prompt de prueba para análisis.",
                                    )

                                    self.assertTrue(res.get("ok"))
                                    self.assertEqual(res.get("call_id"), "1320873716")
                                    self.assertEqual(res.get("result", {}).get("tipo_llamada"), "PRIMERA_VISITA")

                                    # Verify downloader was invoked with cloudtalk_id even when recording_url is None
                                    mock_dl.assert_called_once_with(
                                        None,
                                        cloudtalk_id="1320873716",
                                    )
                                    # Verify Whisper received call.wav filename for WAV bytes
                                    self.assertEqual(mock_tr.call_args[1]["filename"], "call.wav")

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

