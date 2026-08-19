"""
Unit tests for ERR-02 fixes:
1. Gemini transient retry & backoff.
2. Non-transient errors fast fail.
3. Defensive cleanup of abandoned/stale runs.
4. Error categorization helper.
"""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///err02_test.db"
os.environ["APP_ENV"] = "test"

import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import select, BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
)
from app.services.ai_provider import GeminiProvider
from app.services.mass_evaluation_service import MassEvaluationService
from app.utils.error_categorization import categorize_error, is_transient_llm_error


class TestERR02FailedStatesFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ["APP_ENV"] = "test"
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        from app.config import get_settings
        get_settings().gemini_api_key = "test-gemini-key"

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.async_session = lambda: AsyncSession(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    # -------------------------------------------------------------------------
    # 1. Gemini Transient Retry Tests
    # -------------------------------------------------------------------------
    async def test_gemini_audio_transient_retry_success(self):
        """When Gemini returns 503 on 1st attempt, retry succeeds on 2nd attempt."""
        provider = GeminiProvider()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"status": "ok", "transcription": "hola"}'

        # 1st call fails with 503, 2nd call succeeds
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=[
                Exception("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is temporarily overloaded.'}}"),
                mock_response,
            ]
        )
        provider._client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await provider.analyze_audio_bytes(b"dummy_audio", "prompt text", "mp3")

        self.assertEqual(result, '{"status": "ok", "transcription": "hola"}')
        self.assertEqual(mock_client.aio.models.generate_content.call_count, 2)
        mock_sleep.assert_awaited_once()

    async def test_gemini_audio_transient_retry_exhausted(self):
        """When all attempts fail with 500 INTERNAL, the exception is raised."""
        provider = GeminiProvider()
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=Exception("500 INTERNAL. {'error': {'code': 500, 'message': 'Internal error encountered.'}}")
        )
        provider._client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with self.assertRaises(Exception) as ctx:
                await provider.analyze_audio_bytes(b"dummy_audio", "prompt text", "mp3")

        self.assertIn("500 INTERNAL", str(ctx.exception))
        self.assertEqual(mock_client.aio.models.generate_content.call_count, 3)
        self.assertEqual(mock_sleep.await_count, 2)

    async def test_gemini_audio_non_transient_no_retry(self):
        """Non-transient errors (e.g. 400 Bad Request / Invalid Argument) fail immediately on attempt 1."""
        provider = GeminiProvider()
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=ValueError("400 INVALID_ARGUMENT. Invalid prompt syntax.")
        )
        provider._client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with self.assertRaises(ValueError) as ctx:
                await provider.analyze_audio_bytes(b"dummy_audio", "prompt text", "mp3")

        self.assertIn("INVALID_ARGUMENT", str(ctx.exception))
        self.assertEqual(mock_client.aio.models.generate_content.call_count, 1)
        mock_sleep.assert_not_called()

    # -------------------------------------------------------------------------
    # 2. Defensive Stale Runs Cleanup Tests
    # -------------------------------------------------------------------------
    async def test_cleanup_abandoned_with_completed_results(self):
        """Stale run with 100% completed results is resolved to 'completed' instead of 'failed'."""
        async with self.async_session() as db:
            old_time = datetime.now(timezone.utc) - timedelta(minutes=25)
            job = MassEvaluationJob(job_id=1, prompt_id=1, job_name="Job 1", selection_mode="filter")
            run = MassEvaluationRun(
                run_id=1001,
                job_id=1,
                trigger_type="manual",
                status="running",
                started_at=old_time,
                heartbeat_at=old_time,
            )
            res1 = MassEvaluationResult(
                run_id=1001,
                job_id=1,
                call_id="call_1",
                prompt_id=1,
                prompt_snapshot="Test prompt snapshot",
                status="completed",
                call_timestamp=old_time,
            )
            res2 = MassEvaluationResult(
                run_id=1001,
                job_id=1,
                call_id="call_2",
                prompt_id=1,
                prompt_snapshot="Test prompt snapshot",
                status="completed",
                call_timestamp=old_time,
            )
            db.add_all([job, run, res1, res2])
            await db.commit()

            cleaned = await MassEvaluationService.cleanup_stale_runs(db, threshold_minutes=10)
            self.assertEqual(cleaned, 1)

            refreshed = (await db.execute(select(MassEvaluationRun).where(MassEvaluationRun.run_id == 1001))).scalar()
            self.assertEqual(refreshed.status, "completed")
            self.assertIsNone(refreshed.error_message)
            self.assertEqual(refreshed.calls_analyzed, 2)
            self.assertEqual(refreshed.calls_failed, 0)

    async def test_cleanup_abandoned_with_completed_and_failed_results(self):
        """Stale run with mixed results is resolved to 'completed_with_errors' instead of 'failed'."""
        async with self.async_session() as db:
            old_time = datetime.now(timezone.utc) - timedelta(minutes=25)
            job = MassEvaluationJob(job_id=2, prompt_id=1, job_name="Job 2", selection_mode="filter")
            run = MassEvaluationRun(
                run_id=1002,
                job_id=2,
                trigger_type="manual",
                status="running",
                started_at=old_time,
                heartbeat_at=old_time,
            )
            res1 = MassEvaluationResult(
                run_id=1002,
                job_id=2,
                call_id="call_1",
                prompt_id=1,
                prompt_snapshot="Test prompt snapshot",
                status="completed",
                call_timestamp=old_time,
            )
            res2 = MassEvaluationResult(
                run_id=1002,
                job_id=2,
                call_id="call_2",
                prompt_id=1,
                prompt_snapshot="Test prompt snapshot",
                status="failed",
                error_message="503 UNAVAILABLE",
                call_timestamp=old_time,
            )
            db.add_all([job, run, res1, res2])
            await db.commit()

            cleaned = await MassEvaluationService.cleanup_stale_runs(db, threshold_minutes=10)
            self.assertEqual(cleaned, 1)

            refreshed = (await db.execute(select(MassEvaluationRun).where(MassEvaluationRun.run_id == 1002))).scalar()
            self.assertEqual(refreshed.status, "completed_with_errors")
            self.assertIn("Completed with 1 error(s)", refreshed.error_message)
            self.assertEqual(refreshed.calls_analyzed, 2)
            self.assertEqual(refreshed.calls_failed, 1)

    async def test_cleanup_abandoned_without_results(self):
        """Stale run with 0 results in DB is resolved to 'failed' with abandoned message."""
        async with self.async_session() as db:
            old_time = datetime.now(timezone.utc) - timedelta(minutes=25)
            job = MassEvaluationJob(job_id=3, prompt_id=1, job_name="Job 3", selection_mode="filter")
            run = MassEvaluationRun(
                run_id=1003,
                job_id=3,
                trigger_type="manual",
                status="running",
                started_at=old_time,
                heartbeat_at=old_time,
            )
            db.add_all([job, run])
            await db.commit()

            cleaned = await MassEvaluationService.cleanup_stale_runs(db, threshold_minutes=10)
            self.assertEqual(cleaned, 1)

            refreshed = (await db.execute(select(MassEvaluationRun).where(MassEvaluationRun.run_id == 1003))).scalar()
            self.assertEqual(refreshed.status, "failed")
            self.assertIn("Execution abandoned", refreshed.error_message)


    # -------------------------------------------------------------------------
    # 3. Error Categorization Helper Tests
    # -------------------------------------------------------------------------
    def test_error_categorization(self):
        """Validates proper classification of technical errors across providers."""
        # Gemini 500
        cat_500 = categorize_error("500 INTERNAL. {'error': {'code': 500, 'message': 'Internal error encountered.'}}")
        self.assertEqual(cat_500["category"], "gemini_transient_500")
        self.assertTrue(cat_500["is_transient"])
        self.assertEqual(cat_500["provider"], "gemini")

        # Gemini 503
        cat_503 = categorize_error("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is temporarily overloaded.'}}")
        self.assertEqual(cat_503["category"], "gemini_overloaded_503")
        self.assertTrue(cat_503["is_transient"])

        # Gemini 429
        cat_429 = categorize_error("429 RESOURCE_EXHAUSTED. Rate limit exceeded.")
        self.assertEqual(cat_429["category"], "gemini_rate_limit_429")
        self.assertTrue(cat_429["is_transient"])

        # Twilio 404
        cat_twilio = categorize_error("Client error '404 Not Found' for url 'https://api.twilio.com/2010-04-01/Accounts/.../Recordings/RE123.mp3'")
        self.assertEqual(cat_twilio["category"], "twilio_recording_not_found")
        self.assertFalse(cat_twilio["is_transient"])
        self.assertEqual(cat_twilio["provider"], "twilio")

        # Invalid JSON
        cat_json = categorize_error("El modelo no devolvió un JSON válido.")
        self.assertEqual(cat_json["category"], "gemini_invalid_json")
        self.assertFalse(cat_json["is_transient"])

        # Transport Disconnect
        cat_trans = categorize_error("Server disconnected without sending a response.")
        self.assertEqual(cat_trans["category"], "transport_error")
        self.assertTrue(cat_trans["is_transient"])


if __name__ == "__main__":
    unittest.main()
