"""
Regression test for MissingGreenlet bug in _execute_background_run.

Tests that:
1. company_id/service_id are correctly saved in results (not lost due to ORM expiry).
2. No MissingGreenlet error occurs when accessing company_id after db.commit().
3. Per-call errors (except block) also save company_id/service_id correctly.
4. The background run function reads job fields before any commit.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

# Force test DB before any import
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_bg_run_test.db"

db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import BigInteger, event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


class TestBackgroundRunNoMissingGreenlet(unittest.IsolatedAsyncioTestCase):
    """Regression tests ensuring background run never lazy-loads expired ORM objects."""

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: DB points to production!"

        if os.path.exists("mass_bg_run_test.db"):
            try:
                os.remove("mass_bg_run_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=1, service_name="Front Boston", service_key="front", company_id=1)
            db.add(s1)
            await db.flush()

            u1 = User(user_id=1, username="super", email="super@test.com", role="admin", password_hash="dummy")
            db.add(u1)
            await db.flush()

            p1 = Prompt(prompt_id=1, prompt_name="Front Prompt", prompt_type="audio", service_id=1, company_id=1, is_active=True)
            v1 = PromptVersion(id=1, prompt_id=1, prompt="Analiza esta llamada.", version_label="v1", is_current=True)
            db.add_all([p1, v1])
            await db.flush()

            # Legacy prompt: service_id set but company_id=NULL
            p2 = Prompt(prompt_id=2, prompt_name="Legacy Front Prompt", prompt_type="audio", service_id=1, company_id=None, is_active=False)
            v2 = PromptVersion(id=2, prompt_id=2, prompt="Analiza esta llamada legacy.", version_label="v1", is_current=True)
            db.add_all([p2, v2])
            await db.flush()

            # Job that will be used in background runs
            self.job = MassEvaluationJob(
                job_id=1,
                job_name="Test Mass Job",
                company_id=1,
                service_id=1,
                prompt_id=1,
                prompt_name="Front Prompt",
                created_by=1,
                is_active=True,
                schedule_enabled=False,
            )
            db.add(self.job)
            await db.flush()

            # Run record
            self.run = MassEvaluationRun(
                run_id=1,
                job_id=1,
                company_id=1,
                service_id=1,
                trigger_type="manual",
                status="running",
                started_at=datetime.now(timezone.utc),
                effective_filters={"date_from": None, "date_to": None},
            )
            db.add(self.run)
            await db.commit()

    async def asyncTearDown(self):
        if os.path.exists("mass_bg_run_test.db"):
            try:
                os.remove("mass_bg_run_test.db")
            except Exception:
                pass

    async def _run_background_with_mocks(self, calls, analyze_result=None, analyze_side_effect=None):
        """Helper: run _execute_background_run with HubSpot/Twilio/Gemini fully mocked."""
        from app.services.mass_evaluation_service import MassEvaluationService

        fake_audio = b"FAKE_MP3_BYTES"
        filters_payload = {"date_from": None, "date_to": None, "max_calls": 5}

        with (
            patch("app.services.mass_evaluation_service.HubSpotService") as MockHS,
            patch("app.services.mass_evaluation_service.TwilioService") as MockTS,
            patch("app.services.mass_evaluation_service.analyze_audio_bytes") as MockGemini,
        ):
            hs_instance = MockHS.return_value
            hs_instance.search_calls_for_mass_evaluation = AsyncMock(return_value=calls)
            hs_instance.get_call = AsyncMock(return_value={})

            ts_instance = MockTS.return_value
            ts_instance.download_audio = AsyncMock(return_value=fake_audio)

            if analyze_side_effect:
                MockGemini.side_effect = analyze_side_effect
            else:
                MockGemini.return_value = analyze_result or '{"tipo_llamada": "front", "evaluacion_global": 8.5}'

            await MassEvaluationService._execute_background_run(
                job_id=1,
                run_id=1,
                filters_payload=filters_payload,
            )

    async def test_background_run_saves_company_id_in_results(self):
        """Regression test 17a: background run saves correct company_id=1 in result, not NULL.
        Previously crashed with MissingGreenlet when accessing job.company_id after db.commit()."""
        calls = [
            {
                "call_id": "call-001",
                "hs_object_id": "call-001",
                "recording_url": "http://twilio.test/recording.mp3",
                "hubspot_owner_id": "owner-1",
                "call_timestamp": "2024-01-01T10:00:00Z",
                "call_duration_seconds": 120,
                "direction": "inbound",
                "status": "completed",
            }
        ]

        await self._run_background_with_mocks(calls)

        async with AsyncSession(self.engine) as db:
            result_stmt = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call-001")
            res = await db.execute(result_stmt)
            result = res.scalars().first()

        self.assertIsNotNone(result, "Result should be saved in DB")
        self.assertEqual(result.company_id, 1, "company_id must be 1 — not NULL (MissingGreenlet regression)")
        self.assertEqual(result.service_id, 1, "service_id must be 1")
        self.assertIn(result.status, ("completed", "failed"), f"Unexpected status: {result.status}")

    async def test_background_run_skipped_call_saves_company_id(self):
        """Regression test 17b: skipped call (no recording URL) also saves correct company_id."""
        calls = [
            {
                "call_id": "call-002",
                "hs_object_id": "call-002",
                "recording_url": None,  # Will be skipped
                "hubspot_owner_id": "owner-1",
                "call_timestamp": "2024-01-01T10:00:00Z",
                "call_duration_seconds": 60,
                "direction": "inbound",
                "status": "completed",
            }
        ]

        await self._run_background_with_mocks(calls)

        async with AsyncSession(self.engine) as db:
            result_stmt = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call-002")
            res = await db.execute(result_stmt)
            result = res.scalars().first()

        self.assertIsNotNone(result, "Skipped result should be saved in DB")
        self.assertEqual(result.company_id, 1, "Skipped result must have company_id=1 (MissingGreenlet regression)")
        self.assertEqual(result.status, "skipped")

    async def test_background_run_failed_call_saves_company_id(self):
        """Regression test 17c: failed call (analyze error) also saves correct company_id.
        Previously the except block ALSO crashed with MissingGreenlet at job.company_id."""
        calls = [
            {
                "call_id": "call-003",
                "hs_object_id": "call-003",
                "recording_url": "http://twilio.test/bad.mp3",
                "hubspot_owner_id": "owner-1",
                "call_timestamp": "2024-01-01T10:00:00Z",
                "call_duration_seconds": 180,
                "direction": "inbound",
                "status": "completed",
            }
        ]

        # Force analyze_audio_bytes to raise, triggering the except block
        def fake_analyze_error(*args, **kwargs):
            raise ValueError("Simulated Gemini failure for regression test")

        await self._run_background_with_mocks(calls, analyze_side_effect=fake_analyze_error)

        async with AsyncSession(self.engine) as db:
            result_stmt = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call-003")
            res = await db.execute(result_stmt)
            result = res.scalars().first()

        self.assertIsNotNone(result, "Failed result should be saved in DB even on analyze error")
        self.assertEqual(result.company_id, 1, "Failed result must have company_id=1 — not lazy-loaded from expired ORM")
        self.assertEqual(result.status, "failed")

    async def test_background_run_completes_run_record(self):
        """Regression test 17d: background run completes and updates run status correctly."""
        calls = [
            {
                "call_id": "call-004",
                "hs_object_id": "call-004",
                "recording_url": "http://twilio.test/ok.mp3",
                "hubspot_owner_id": "owner-1",
                "call_timestamp": "2024-01-01T10:00:00Z",
                "call_duration_seconds": 90,
                "direction": "inbound",
                "status": "completed",
            }
        ]

        await self._run_background_with_mocks(calls)

        async with AsyncSession(self.engine) as db:
            run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == 1)
            run_res = await db.execute(run_stmt)
            run = run_res.scalars().first()

        self.assertIsNotNone(run, "Run must exist")
        self.assertIn(run.status, ("completed", "completed_with_errors", "failed"), f"Run status unexpected: {run.status}")
        self.assertIsNotNone(run.finished_at, "Run must have a finished_at timestamp")


if __name__ == "__main__":
    asyncio.run(unittest.main())
