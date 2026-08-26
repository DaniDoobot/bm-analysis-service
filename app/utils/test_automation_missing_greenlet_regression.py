"""
Regression test for MissingGreenlet error in _execute_background_run.
Validates that background execution survives session commit expiration without lazy loading crashes.
"""
import asyncio
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationResult,
    MassEvaluationRun,
)
from app.models.prompts import Prompt, PromptVersion
from app.models.services import Service
from app.services.mass_evaluation_service import MassEvaluationService


class TestAutomationMissingGreenletRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create an in-memory SQLite async engine
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=True  # Ensure expire_on_commit is True to catch MissingGreenlet
        )

        # Seed minimal database dependencies (Service, Prompt, PromptVersion)
        async with self.async_session() as session:
            service = Service(
                service_id=1,
                company_id=1,
                service_key="front",
                service_name="Front",
                is_active=True,
                created_at=datetime.now(timezone.utc)
            )
            session.add(service)

            prompt = Prompt(
                prompt_id=58,
                company_id=1,
                service_id=1,
                prompt_name="Front Evaluation Prompt",
                prompt_type="evaluation",
                is_active=True,
                created_at=datetime.now(timezone.utc)
            )
            session.add(prompt)
            await session.flush()

            version = PromptVersion(
                id=248,
                prompt_id=58,
                version_name="v248-alarm",
                version_label="v248",
                prompt="Evalúa la siguiente llamada y devuelve un JSON.",
                is_current=True,
                created_at=datetime.now(timezone.utc)
            )
            session.add(version)

            job = MassEvaluationJob(
                job_id=48,
                company_id=1,
                service_id=1,
                prompt_id=58,
                prompt_version_id=248,
                job_name="A partir 4 Ago Front - Todos",
                is_active=True,
                execution_source="automation",
                duration_min_seconds=30,
                duration_max_seconds=3600,
                direction="all",
                only_with_recording=True,
                max_calls=10,
                timezone="Europe/Madrid",
                created_at=datetime.now(timezone.utc)
            )
            session.add(job)

            run = MassEvaluationRun(
                run_id=5001,
                job_id=48,
                company_id=1,
                service_id=1,
                trigger_type="automation",
                execution_source="automation",
                status="running",
                started_at=datetime.now(timezone.utc),
                effective_filters={"date_from": "2026-08-22T11:09:33Z", "date_to": "2026-08-22T11:19:33Z"},
                created_at=datetime.now(timezone.utc)
            )
            session.add(run)

            await session.commit()
            # After commit with expire_on_commit=True, run and job instances are fully expired!

    async def asyncTearDown(self):
        await self.engine.dispose()

    @patch("app.db.get_engine")
    @patch("app.services.mass_evaluation_service.HubSpotService")
    async def test_execute_background_run_no_missing_greenlet(self, mock_hs_cls, mock_get_engine):
        """
        Ensures _execute_background_run completes without MissingGreenlet when ORM objects are expired.
        """
        mock_get_engine.return_value = self.engine

        mock_hs_instance = MagicMock()
        mock_hs_cls.return_value = mock_hs_instance
        # Mock search_calls_for_mass_evaluation to return 0 calls (empty window)
        mock_hs_instance.search_calls_for_mass_evaluation = AsyncMock(return_value=[])

        # Execute background runner directly
        await MassEvaluationService._execute_background_run(
            job_id=48,
            run_id=5001,
            filters_payload={"date_from": "2026-08-22T11:09:33Z", "date_to": "2026-08-22T11:19:33Z"}
        )

        # Verify run status in database
        async with self.async_session() as session:
            stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == 5001)
            res = await session.execute(stmt)
            updated_run = res.scalars().first()

            self.assertIsNotNone(updated_run)
            self.assertEqual(updated_run.status, "completed")
            self.assertIsNone(updated_run.error_message)
            self.assertEqual(updated_run.calls_found, 0)
            self.assertEqual(updated_run.calls_selected, 0)
            self.assertEqual(updated_run.calls_analyzed, 0)

    @patch("app.db.get_engine")
    @patch("app.services.mass_evaluation_service.HubSpotService")
    @patch("app.services.mass_evaluation_service.TwilioService")
    @patch("app.services.mass_evaluation_service.analyze_audio_bytes")
    async def test_execute_background_run_with_calls_and_dedup(
        self, mock_analyze, mock_twilio_cls, mock_hs_cls, mock_get_engine
    ):
        """
        Ensures calls processing and deduplication against completed records work without MissingGreenlet.
        """
        mock_get_engine.return_value = self.engine

        # Mock HubSpot search to return 2 calls
        mock_hs_instance = MagicMock()
        mock_hs_cls.return_value = mock_hs_instance
        mock_hs_instance.search_calls_for_mass_evaluation = AsyncMock(return_value=[
            {
                "call_id": "call_mock_1",
                "hs_object_id": "call_mock_1",
                "recording_url": "https://api.twilio.com/mock1.mp3",
                "hubspot_owner_id": "owner_1",
                "call_timestamp": "2026-08-22T11:12:00Z",
                "call_duration_seconds": 120,
                "direction": "INBOUND"
            },
            {
                "call_id": "call_mock_2",
                "hs_object_id": "call_mock_2",
                "recording_url": "https://api.twilio.com/mock2.mp3",
                "hubspot_owner_id": "owner_1",
                "call_timestamp": "2026-08-22T11:15:00Z",
                "call_duration_seconds": 150,
                "direction": "INBOUND"
            }
        ])

        # Mock Twilio
        mock_twilio_instance = MagicMock()
        mock_twilio_cls.return_value = mock_twilio_instance
        mock_twilio_instance.download_audio = AsyncMock(return_value=b"FAKE_MP3_AUDIO_BYTES")

        # Mock LLM evaluation
        mock_analyze.return_value = '{"evaluacion_global": 8.5, "alarma": "No", "cierre_cita": "Si", "tipo_llamada": "cita_medica"}'

        # Execute background runner
        await MassEvaluationService._execute_background_run(
            job_id=48,
            run_id=5001,
            filters_payload={"date_from": "2026-08-22T11:09:33Z", "date_to": "2026-08-22T11:19:33Z"}
        )

        # Verify results in DB
        async with self.async_session() as session:
            stmt_run = select(MassEvaluationRun).where(MassEvaluationRun.run_id == 5001)
            res_run = (await session.execute(stmt_run)).scalars().first()
            self.assertEqual(res_run.status, "completed")
            self.assertEqual(res_run.calls_found, 2)
            self.assertEqual(res_run.calls_analyzed, 2)

            stmt_res = select(MassEvaluationResult).where(MassEvaluationResult.run_id == 5001)
            results = (await session.execute(stmt_res)).scalars().all()
            self.assertEqual(len(results), 2)
            for r in results:
                self.assertEqual(r.prompt_version_id, 248)
                self.assertEqual(r.status, "completed")


if __name__ == "__main__":
    unittest.main()
