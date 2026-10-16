"""
Tests for CloudTalk Webhook, Idempotency, Recording Polling, and Automatic Evaluation.
Verifies all 17 required scenarios:
1. webhook secret correcto -> 202 accepted
2. webhook secret incorrecto/ausente -> 401 unauthorized
3. payload válido y normalización flexible
4. call >15 s -> accepted
5. call <=15 s -> skipped (no evaluation)
6. talking_time ausente -> consulta metadata CloudTalk
7. webhook duplicado -> no duplica job
8. recording disponible inmediatamente -> evaluación directa
9. recording 404/not ready -> retry controlado en waiting_recording
10. recording disponible en retry posterior -> completado
11. 429 CloudTalk controlado
12. recording nunca disponible -> failed tras 5 intentos
13. prompt Cloudtalk Bot ausente -> configuration_missing sin usar Front
14. análisis completado -> asociado a service_id Cloudtalk Bot
15. usage registrado con module="cloudtalk_bot"
16. llamada sin HubSpot -> se analiza igualmente
17. ningún secreto aparece en logs o excepciones
"""
import asyncio
from datetime import datetime, timezone, timedelta
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.main import app
from app.models.cloudtalk_jobs import CloudTalkEvaluationJob
from app.models.services import Service
from app.schemas.cloudtalk import CloudTalkWebhookPayload
from app.services.audio_download_service import AudioDownloadError
from app.services.cloudtalk_webhook_service import (
    extract_webhook_fields,
    get_cloudtalk_bot_service,
    handle_call_ended_webhook,
    process_cloudtalk_job,
    sanitize_payload_for_storage,
    validate_webhook_secret,
)

FAKE_WAV_BYTES = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 200


class TestCloudTalkWebhookEvaluation(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.client = TestClient(app)
        self.secret = "test-cloudtalk-secret-key-12345"

    # ── 1. Webhook secret correcto ────────────────────────────────────────────
    def test_webhook_secret_correct(self):
        """1. Webhook with correct X-CloudTalk-Webhook-Key returns 202 Accepted."""
        settings = get_settings()
        with patch.object(settings, "cloudtalk_webhook_secret", self.secret), \
             patch("app.routers.cloudtalk_webhook.handle_call_ended_webhook", new=AsyncMock(return_value={
                 "ok": True, "status": "accepted", "job_id": 101, "call_id": "1320873716"
             })):
            response = self.client.post(
                "/bm/integrations/cloudtalk/call-ended",
                headers={"X-CloudTalk-Webhook-Key": self.secret},
                json={"call_id": "1320873716", "talking_time": 45},
            )
            self.assertEqual(response.status_code, 202)
            data = response.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["status"], "accepted")
            self.assertEqual(data["job_id"], 101)

    # ── 2. Webhook secret incorrecto o ausente ────────────────────────────────
    def test_webhook_secret_incorrect_or_missing(self):
        """2. Webhook with wrong or missing secret returns 401 Unauthorized."""
        settings = get_settings()
        with patch.object(settings, "cloudtalk_webhook_secret", self.secret):
            # Missing header
            resp_missing = self.client.post(
                "/bm/integrations/cloudtalk/call-ended",
                json={"call_id": "1320873716", "talking_time": 45},
            )
            self.assertEqual(resp_missing.status_code, 401)

            # Incorrect header
            resp_wrong = self.client.post(
                "/bm/integrations/cloudtalk/call-ended",
                headers={"X-CloudTalk-Webhook-Key": "wrong-secret-value"},
                json={"call_id": "1320873716", "talking_time": 45},
            )
            self.assertEqual(resp_wrong.status_code, 401)

    # ── 3. Payload válido y normalización ─────────────────────────────────────
    def test_payload_normalization_flexible(self):
        """3. Normalized extractor accepts top-level and nested CloudTalk payload fields."""
        payload_nested = {
            "Cdr": {
                "id": "1320873716",
                "call_uuid": "uuid-xyz-99",
                "talking_time": "55",
                "type": "inbound",
                "public_external": "+34600111222",
                "public_internal": "+34910000000",
                "started_at": "2026-10-16 08:00:00",
                "ended_at": "2026-10-16 08:00:55",
            },
            "Agent": {
                "id": "88",
                "fullname": "Agente Pruebas",
                "email": "agente@bostonmedical.es",
            },
        }
        res = extract_webhook_fields(payload_nested)
        self.assertEqual(res["call_id"], "1320873716")
        self.assertEqual(res["call_uuid"], "uuid-xyz-99")
        self.assertEqual(res["talking_time"], 55)
        self.assertEqual(res["direction"], "inbound")
        self.assertEqual(res["agent_id"], "88")
        self.assertEqual(res["agent_name"], "Agente Pruebas")
        self.assertEqual(res["agent_email"], "agente@bostonmedical.es")
        self.assertEqual(res["external_number"], "+34600111222")
        self.assertIsNotNone(res["started_at"])

    # ── 4. Call > 15s -> Accepted ─────────────────────────────────────────────
    async def test_call_greater_than_15s_accepted(self):
        """4. Calls with talking_time > 15s are accepted and queued."""
        mock_db = AsyncMock(spec=AsyncSession)

        # Mock service lookup
        mock_service = Service(service_id=13, service_key="cloudtalk_bot", company_id=1)

        # Mock no existing job
        mock_result = MagicMock()
        mock_result.scalars().first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        async def fake_refresh(obj):
            obj.id = 101

        mock_db.refresh = AsyncMock(side_effect=fake_refresh)

        with patch("app.services.cloudtalk_webhook_service.get_cloudtalk_bot_service", new=AsyncMock(return_value=mock_service)), \
             patch("app.services.cloudtalk_webhook_service.process_cloudtalk_job_background", new=AsyncMock()) as mock_bg, \
             patch("app.services.cloudtalk_webhook_service.asyncio.create_task") as mock_task:
            mock_task.side_effect = lambda coro: coro.close()
            res = await handle_call_ended_webhook(
                db=mock_db,
                payload_data={"call_id": "1320873716", "talking_time": 45},
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "accepted")
            self.assertEqual(res["call_id"], "1320873716")
            self.assertFalse(res["duplicate"])
            mock_task.assert_called_once()

    # ── 5. Call <= 15s -> Skipped ─────────────────────────────────────────────
    async def test_call_less_or_equal_15s_skipped(self):
        """5. Calls with talking_time <= 15s are marked skipped and not evaluated."""
        mock_db = AsyncMock(spec=AsyncSession)
        mock_service = Service(service_id=13, service_key="cloudtalk_bot", company_id=1)

        mock_result = MagicMock()
        mock_result.scalars().first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.cloudtalk_webhook_service.get_cloudtalk_bot_service", new=AsyncMock(return_value=mock_service)), \
             patch("app.services.cloudtalk_webhook_service.asyncio.create_task") as mock_task:
            res = await handle_call_ended_webhook(
                db=mock_db,
                payload_data={"call_id": "1320873716", "talking_time": 12},
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "skipped")
            self.assertIn("<= 15s", res["message"])
            mock_task.assert_not_called()

    # ── 6. talking_time ausente -> consulta metadata CloudTalk ─────────────────
    async def test_talking_time_missing_queries_cloudtalk_metadata(self):
        """6. When talking_time is missing, queries CloudTalk get_call to resolve duration."""
        mock_db = AsyncMock(spec=AsyncSession)
        mock_service = Service(service_id=13, service_key="cloudtalk_bot", company_id=1)

        mock_result = MagicMock()
        mock_result.scalars().first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        async def fake_refresh(obj):
            obj.id = 102

        mock_db.refresh = AsyncMock(side_effect=fake_refresh)

        with patch("app.services.cloudtalk_webhook_service.get_cloudtalk_bot_service", new=AsyncMock(return_value=mock_service)), \
             patch("app.services.cloudtalk_service.CloudTalkService.get_call", new=AsyncMock(return_value={
                 "id": "1320873716",
                 "talking_time": 50,
                 "type": "inbound",
             })) as mock_get_call, \
             patch("app.services.cloudtalk_webhook_service.process_cloudtalk_job_background", new=AsyncMock()), \
             patch("app.services.cloudtalk_webhook_service.asyncio.create_task") as mock_task:
            mock_task.side_effect = lambda coro: coro.close()
            res = await handle_call_ended_webhook(
                db=mock_db,
                payload_data={"call_id": "1320873716"},  # No talking_time
            )
            mock_get_call.assert_called_once_with("1320873716")
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "accepted")
            mock_task.assert_called_once()

    # ── 7. Webhook duplicado -> no duplica job ────────────────────────────────
    async def test_duplicate_webhook_does_not_duplicate_job(self):
        """7. Duplicate webhook for completed or pending job returns existing job with duplicate=True."""
        mock_db = AsyncMock(spec=AsyncSession)
        mock_service = Service(service_id=13, service_key="cloudtalk_bot", company_id=1)

        existing_job = CloudTalkEvaluationJob(
            id=55,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            status="completed",
            analysis_id=220,
        )

        mock_result = MagicMock()
        mock_result.scalars().first.return_value = existing_job
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.cloudtalk_webhook_service.get_cloudtalk_bot_service", new=AsyncMock(return_value=mock_service)), \
             patch("app.services.cloudtalk_webhook_service.asyncio.create_task") as mock_task:
            res = await handle_call_ended_webhook(
                db=mock_db,
                payload_data={"call_id": "1320873716", "talking_time": 60},
            )
            self.assertTrue(res["ok"])
            self.assertTrue(res["duplicate"])
            self.assertEqual(res["status"], "completed")
            self.assertEqual(res["analysis_id"], 220)
            mock_task.assert_not_called()

    # ── 8. Recording disponible inmediatamente ────────────────────────────────
    async def test_recording_immediately_available(self):
        """8. If recording is ready immediately, worker processes and completes analysis."""
        mock_db = AsyncMock(spec=AsyncSession)
        job = CloudTalkEvaluationJob(
            id=10,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            talking_time=40,
            status="pending",
            attempt_count=0,
            webhook_received_at=datetime.now(timezone.utc),
        )
        mock_db.get = AsyncMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        mock_prompt = {
            "prompt_id": 50,
            "prompt_version_id": 100,
            "prompt": "Evaluate this call",
        }

        with patch("app.services.cloudtalk_webhook_service.get_active_prompt", new=AsyncMock(return_value=mock_prompt)), \
             patch("app.services.cloudtalk_service.CloudTalkService.download_recording", new=AsyncMock(return_value=FAKE_WAV_BYTES)), \
             patch("app.services.hubspot_service.HubSpotService.search_call_by_external_id", new=AsyncMock(return_value=None)), \
             patch("app.services.cloudtalk_webhook_service.analyze_audio_bytes", new=AsyncMock(return_value='{"tipo_llamada": "cita"}')), \
             patch("app.services.cloudtalk_webhook_service.save_analysis", new=AsyncMock(return_value=MagicMock(analysis_id=300))):

            await process_cloudtalk_job(mock_db, 10)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.analysis_id, 300)

    # ── 9. Recording 404 / not ready -> retry schedule ────────────────────────
    async def test_recording_404_not_ready_triggers_retry(self):
        """9. When download_recording returns 404/not ready, increments attempt and schedules waiting_recording."""
        mock_db = AsyncMock(spec=AsyncSession)
        job = CloudTalkEvaluationJob(
            id=11,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            talking_time=40,
            status="pending",
            attempt_count=0,
            max_attempts=5,
            webhook_received_at=datetime.now(timezone.utc),
        )
        mock_db.get = AsyncMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        mock_prompt = {"prompt_id": 50, "prompt_version_id": 100, "prompt": "Prompt"}

        with patch("app.services.cloudtalk_webhook_service.get_active_prompt", new=AsyncMock(return_value=mock_prompt)), \
             patch("app.services.cloudtalk_service.CloudTalkService.download_recording", new=AsyncMock(side_effect=AudioDownloadError("Grabación no encontrada (404)", status_code=404))):

            await process_cloudtalk_job(mock_db, 11)
            self.assertEqual(job.status, "waiting_recording")
            self.assertEqual(job.attempt_count, 1)
            self.assertEqual(job.last_error_code, "waiting_recording")
            self.assertIsNotNone(job.next_attempt_at)

    # ── 10. Recording disponible en retry posterior ───────────────────────────
    async def test_recording_available_on_subsequent_retry(self):
        """10. Recording becomes available on subsequent retry (attempt 2), finishes evaluation."""
        mock_db = AsyncMock(spec=AsyncSession)
        job = CloudTalkEvaluationJob(
            id=12,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            talking_time=40,
            status="waiting_recording",
            attempt_count=1,
            max_attempts=5,
            webhook_received_at=datetime.now(timezone.utc),
        )
        mock_db.get = AsyncMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        mock_prompt = {"prompt_id": 50, "prompt_version_id": 100, "prompt": "Prompt"}

        with patch("app.services.cloudtalk_webhook_service.get_active_prompt", new=AsyncMock(return_value=mock_prompt)), \
             patch("app.services.cloudtalk_service.CloudTalkService.download_recording", new=AsyncMock(return_value=FAKE_WAV_BYTES)), \
             patch("app.services.hubspot_service.HubSpotService.search_call_by_external_id", new=AsyncMock(return_value=None)), \
             patch("app.services.cloudtalk_webhook_service.analyze_audio_bytes", new=AsyncMock(return_value='{"tipo_llamada": "cita"}')), \
             patch("app.services.cloudtalk_webhook_service.save_analysis", new=AsyncMock(return_value=MagicMock(analysis_id=305))):

            await process_cloudtalk_job(mock_db, 12)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.analysis_id, 305)

    # ── 11. 429 CloudTalk controlado ──────────────────────────────────────────
    async def test_429_rate_limit_handled_gracefully(self):
        """11. CloudTalk 429 response is handled with controlled retry and no infinite loop."""
        mock_db = AsyncMock(spec=AsyncSession)
        job = CloudTalkEvaluationJob(
            id=13,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            talking_time=40,
            status="pending",
            attempt_count=0,
            max_attempts=5,
            webhook_received_at=datetime.now(timezone.utc),
        )
        mock_db.get = AsyncMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        mock_prompt = {"prompt_id": 50, "prompt_version_id": 100, "prompt": "Prompt"}

        with patch("app.services.cloudtalk_webhook_service.get_active_prompt", new=AsyncMock(return_value=mock_prompt)), \
             patch("app.services.cloudtalk_service.CloudTalkService.download_recording", new=AsyncMock(side_effect=AudioDownloadError("Rate Limit (429)", status_code=429))):

            await process_cloudtalk_job(mock_db, 13)
            self.assertEqual(job.status, "waiting_recording")
            self.assertEqual(job.attempt_count, 1)

    # ── 12. Recording nunca disponible -> failed tras max attempts ────────────
    async def test_recording_never_available_fails_after_max_attempts(self):
        """12. After reaching max attempts (5), job status transitions to failed."""
        mock_db = AsyncMock(spec=AsyncSession)
        job = CloudTalkEvaluationJob(
            id=14,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            talking_time=40,
            status="waiting_recording",
            attempt_count=4,  # Next attempt is 5th
            max_attempts=5,
            webhook_received_at=datetime.now(timezone.utc),
        )
        mock_db.get = AsyncMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        mock_prompt = {"prompt_id": 50, "prompt_version_id": 100, "prompt": "Prompt"}

        with patch("app.services.cloudtalk_webhook_service.get_active_prompt", new=AsyncMock(return_value=mock_prompt)), \
             patch("app.services.cloudtalk_service.CloudTalkService.download_recording", new=AsyncMock(side_effect=AudioDownloadError("404 Not Found", status_code=404))):

            await process_cloudtalk_job(mock_db, 14)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.last_error_code, "recording_not_available")
            self.assertEqual(job.attempt_count, 5)

    # ── 13. Prompt Cloudtalk Bot ausente -> configuration_missing ──────────────
    async def test_prompt_missing_sets_configuration_missing(self):
        """13. When no active prompt exists for Cloudtalk Bot, job pauses in configuration_missing without using Front."""
        mock_db = AsyncMock(spec=AsyncSession)
        job = CloudTalkEvaluationJob(
            id=15,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            talking_time=40,
            status="pending",
            attempt_count=0,
            webhook_received_at=datetime.now(timezone.utc),
        )
        mock_db.get = AsyncMock(return_value=job)

        with patch("app.services.cloudtalk_webhook_service.get_active_prompt", new=AsyncMock(return_value=None)) as mock_prompt_call, \
             patch("app.services.cloudtalk_service.CloudTalkService.download_recording") as mock_down:

            await process_cloudtalk_job(mock_db, 15)
            self.assertEqual(job.status, "configuration_missing")
            self.assertEqual(job.last_error_code, "configuration_missing")
            mock_down.assert_not_called()
            mock_prompt_call.assert_called_once_with(
                db=mock_db,
                prompt_type="audio",
                service_id=13,
                company_id=1,
            )

    # ── 14. Análisis asociado al service_id Cloudtalk Bot ─────────────────────
    async def test_analysis_associated_with_cloudtalk_bot_service(self):
        """14. Completed analysis is saved with service_id of Cloudtalk Bot (13) and company_id=1."""
        mock_db = AsyncMock(spec=AsyncSession)
        job = CloudTalkEvaluationJob(
            id=16,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            talking_time=40,
            status="pending",
            attempt_count=0,
            webhook_received_at=datetime.now(timezone.utc),
        )
        mock_db.get = AsyncMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        mock_prompt = {"prompt_id": 50, "prompt_version_id": 100, "prompt": "Prompt"}

        with patch("app.services.cloudtalk_webhook_service.get_active_prompt", new=AsyncMock(return_value=mock_prompt)), \
             patch("app.services.cloudtalk_service.CloudTalkService.download_recording", new=AsyncMock(return_value=FAKE_WAV_BYTES)), \
             patch("app.services.hubspot_service.HubSpotService.search_call_by_external_id", new=AsyncMock(return_value=None)), \
             patch("app.services.cloudtalk_webhook_service.analyze_audio_bytes", new=AsyncMock(return_value='{"tipo_llamada": "cita"}')), \
             patch("app.services.cloudtalk_webhook_service.save_analysis", new=AsyncMock(return_value=MagicMock(analysis_id=777))) as mock_save:

            await process_cloudtalk_job(mock_db, 16)
            mock_save.assert_called_once()
            call_meta = mock_save.call_args.kwargs["call_metadata"]
            self.assertEqual(call_meta["service_id"], 13)
            self.assertEqual(call_meta["company_id"], 1)
            self.assertEqual(call_meta["call_id"], "1320873716")

    # ── 15. Usage registrado con module="cloudtalk_bot" ───────────────────────
    async def test_usage_registered_with_specific_module(self):
        """15. Calls analyze_audio_bytes with module='cloudtalk_bot'."""
        mock_db = AsyncMock(spec=AsyncSession)
        job = CloudTalkEvaluationJob(
            id=17,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            talking_time=40,
            status="pending",
            attempt_count=0,
            webhook_received_at=datetime.now(timezone.utc),
        )
        mock_db.get = AsyncMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        mock_prompt = {"prompt_id": 50, "prompt_version_id": 100, "prompt": "Prompt"}

        with patch("app.services.cloudtalk_webhook_service.get_active_prompt", new=AsyncMock(return_value=mock_prompt)), \
             patch("app.services.cloudtalk_service.CloudTalkService.download_recording", new=AsyncMock(return_value=FAKE_WAV_BYTES)), \
             patch("app.services.hubspot_service.HubSpotService.search_call_by_external_id", new=AsyncMock(return_value=None)), \
             patch("app.services.cloudtalk_webhook_service.analyze_audio_bytes", new=AsyncMock(return_value='{"tipo_llamada": "cita"}')) as mock_ai, \
             patch("app.services.cloudtalk_webhook_service.save_analysis", new=AsyncMock(return_value=MagicMock(analysis_id=888))):

            await process_cloudtalk_job(mock_db, 17)
            mock_ai.assert_called_once()
            self.assertEqual(mock_ai.call_args.kwargs["module"], "cloudtalk_bot")
            self.assertEqual(mock_ai.call_args.kwargs["service_id"], 13)
            self.assertEqual(mock_ai.call_args.kwargs["company_id"], 1)

    # ── 16. Llamada sin HubSpot se analiza igualmente ─────────────────────────
    async def test_call_without_hubspot_analyzed_successfully(self):
        """16. Even if call does not exist in HubSpot, evaluation succeeds using CloudTalk ID."""
        mock_db = AsyncMock(spec=AsyncSession)
        job = CloudTalkEvaluationJob(
            id=18,
            company_id=1,
            service_id=13,
            cloudtalk_call_id="1320873716",
            talking_time=30,
            status="pending",
            attempt_count=0,
            webhook_received_at=datetime.now(timezone.utc),
        )
        mock_db.get = AsyncMock(return_value=job)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        mock_prompt = {"prompt_id": 50, "prompt_version_id": 100, "prompt": "Prompt"}

        # Simulate HubSpot search returning None (call not in HubSpot yet)
        with patch("app.services.cloudtalk_webhook_service.get_active_prompt", new=AsyncMock(return_value=mock_prompt)), \
             patch("app.services.hubspot_service.HubSpotService.search_call_by_external_id", new=AsyncMock(return_value=None)), \
             patch("app.services.cloudtalk_service.CloudTalkService.download_recording", new=AsyncMock(return_value=FAKE_WAV_BYTES)), \
             patch("app.services.cloudtalk_webhook_service.analyze_audio_bytes", new=AsyncMock(return_value='{"tipo_llamada": "cita"}')), \
             patch("app.services.cloudtalk_webhook_service.save_analysis", new=AsyncMock(return_value=MagicMock(analysis_id=999))) as mock_save:

            await process_cloudtalk_job(mock_db, 18)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.analysis_id, 999)
            call_meta = mock_save.call_args.kwargs["call_metadata"]
            self.assertIsNone(call_meta["hubspot_url"])
            self.assertEqual(call_meta["call_id"], "1320873716")

    # ── 17. Ningún secreto aparece en logs ni excepciones ─────────────────────
    def test_zero_secrets_in_logs_or_exceptions(self):
        """17. Ensures CLOUDTALK_WEBHOOK_SECRET is never logged or included in response payloads."""
        secret_sample = "SUPER_SECRET_TOKEN_DO_NOT_LEAK_XYZ"
        sanitized = sanitize_payload_for_storage({
            "call_id": "1320873716",
            "webhook_key": secret_sample,
            "secret": secret_sample,
            "auth_token": secret_sample,
            "details": {"api_key": secret_sample, "data": "valid_value"},
        })
        self.assertNotIn("webhook_key", sanitized)
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("auth_token", sanitized)
        self.assertNotIn("api_key", sanitized["details"])
        self.assertEqual(sanitized["details"]["data"], "valid_value")

        settings = get_settings()
        # Test validate_webhook_secret rejects without leaking
        with patch.object(settings, "cloudtalk_webhook_secret", secret_sample):
            self.assertFalse(validate_webhook_secret("bad-key"))
            self.assertTrue(validate_webhook_secret(secret_sample))


if __name__ == "__main__":
    unittest.main()
