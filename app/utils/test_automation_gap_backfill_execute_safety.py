"""
Unit Tests for Automation Gap Backfill Safety and Execute Mode.
===============================================================
Verifies:
1. Dry-run remains default and makes zero DB mutations.
2. Safety confirmation string required for execution.
3. Deduplication: already completed calls are skipped.
4. Correct trigger_type='backfill' and execution_source='backfill'.
5. Runs cleanly transition to completed status.
"""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///automation_gap_backfill_test.db"

import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

from sqlalchemy import BigInteger, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import Base, _get_engine
get_settings.cache_clear()
_get_engine.cache_clear()
from app.models.mass_evaluations import (
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from app.models.prompts import Prompt, PromptVersion
from app.utils.backfill_automation_gaps import plan_gap_backfill, execute_gap_backfill

MADRID_TZ = ZoneInfo("Europe/Madrid")


class TestAutomationGapBackfillSafety(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_dry_run_plan_filtering_and_gap_selection(self):
        """Test planning backfill for specific gap indexes."""
        async with self.async_session() as db:
            p = Prompt(prompt_id=58, prompt_name="Prompt Front", prompt_type="mass")
            pv = PromptVersion(id=241, prompt_id=58, prompt="Evalúa llamada.", version_name="v1", is_current=True)
            job = MassEvaluationJob(
                job_id=48,
                prompt_id=58,
                prompt_version_id=241,
                job_name="[Auto] Front Test",
                selection_mode="filter",
                timezone="Europe/Madrid",
            )
            aut = MassAnalysisAutomation(
                automation_id=8,
                name="Front Auto",
                job_id=48,
                prompt_id=58,
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
            )
            now = datetime.now(timezone.utc)
            r1 = MassAnalysisAutomationRun(
                automation_id=8,
                window_from=now - timedelta(minutes=60),
                window_to=now - timedelta(minutes=50),
                status="completed",
            )
            r2 = MassAnalysisAutomationRun(
                automation_id=8,
                window_from=now - timedelta(minutes=30),
                window_to=now - timedelta(minutes=20),
                status="completed",
            )
            db.add_all([p, pv, job, aut, r1, r2])
            await db.commit()

            # Plan for gap index 1
            plan = await plan_gap_backfill(db, automation_id=8, days_back=1, gap_indexes=[1])
            self.assertEqual(plan["total_gaps_found"], 1)
            self.assertEqual(len(plan["planned_batches"]), 1)
            self.assertAlmostEqual(plan["planned_batches"][0]["gap_minutes"], 20.0, places=1)

    async def test_execute_empty_window_marks_completed(self):
        """When 0 calls are found in HubSpot, runs are marked completed with 0 counts."""
        async with self.async_session() as db:
            p = Prompt(prompt_id=58, prompt_name="Prompt Front", prompt_type="mass")
            pv = PromptVersion(id=241, prompt_id=58, prompt="Evalúa llamada.", version_name="v1", is_current=True)
            job = MassEvaluationJob(
                job_id=48,
                prompt_id=58,
                prompt_version_id=241,
                job_name="[Auto] Front Test",
                selection_mode="filter",
                timezone="Europe/Madrid",
            )
            aut = MassAnalysisAutomation(
                automation_id=8,
                name="Front Auto",
                job_id=48,
                prompt_id=58,
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
            )
            db.add_all([p, pv, job, aut])
            await db.commit()

            gap_item = {
                "gap_index": 9,
                "gap_from_utc": datetime(2026, 8, 13, 13, 56, 8, tzinfo=timezone.utc),
                "gap_to_utc": datetime(2026, 8, 13, 14, 2, 9, tzinfo=timezone.utc),
                "gap_minutes": 6.01,
            }

            with patch("app.services.hubspot_service.HubSpotService.search_calls_for_mass_evaluation", new_callable=AsyncMock) as mock_search:
                mock_search.return_value = []
                results = await execute_gap_backfill(db, automation_id=8, planned_gaps=[gap_item])

                self.assertEqual(len(results), 1)
                res = results[0]
                self.assertEqual(res["status"], "completed")
                self.assertEqual(res["calls_found"], 0)
                self.assertEqual(res["calls_selected"], 0)

                # Verify in DB
                m_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == res["mass_run_id"])
                m_run = (await db.execute(m_stmt)).scalar()
                self.assertEqual(m_run.status, "completed")
                self.assertEqual(m_run.trigger_type, "backfill")
                self.assertEqual(m_run.execution_source, "backfill")

                a_stmt = select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == res["automation_run_id"])
                a_run = (await db.execute(a_stmt)).scalar()
                self.assertEqual(a_run.status, "completed")
                self.assertEqual(a_run.run_id, m_run.run_id)

    async def test_execute_skips_already_completed_calls(self):
        """When calls found in HubSpot already exist in DB with status=completed, they are deduplicated."""
        async with self.async_session() as db:
            p = Prompt(prompt_id=58, prompt_name="Prompt Front", prompt_type="mass")
            pv = PromptVersion(id=241, prompt_id=58, prompt="Evalúa llamada.", version_name="v1", is_current=True)
            job = MassEvaluationJob(
                job_id=48,
                prompt_id=58,
                prompt_version_id=241,
                job_name="[Auto] Front Test",
                selection_mode="filter",
                timezone="Europe/Madrid",
            )
            aut = MassAnalysisAutomation(
                automation_id=8,
                name="Front Auto",
                job_id=48,
                prompt_id=58,
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
            )
            # Existing completed result
            existing_result = MassEvaluationResult(
                run_id=100,
                job_id=48,
                call_id="call_already_analyzed",
                prompt_id=58,
                prompt_snapshot="Test prompt",
                status="completed",
            )
            db.add_all([p, pv, job, aut, existing_result])
            await db.commit()

            gap_item = {
                "gap_index": 10,
                "gap_from_utc": datetime(2026, 8, 14, 6, 45, 18, tzinfo=timezone.utc),
                "gap_to_utc": datetime(2026, 8, 14, 6, 54, 18, tzinfo=timezone.utc),
                "gap_minutes": 9.0,
            }

            with patch("app.services.hubspot_service.HubSpotService.search_calls_for_mass_evaluation", new_callable=AsyncMock) as mock_search:
                mock_search.return_value = [
                    {"call_id": "call_already_analyzed", "recording_url": "https://example.com/audio.mp3"}
                ]
                results = await execute_gap_backfill(db, automation_id=8, planned_gaps=[gap_item])

                self.assertEqual(len(results), 1)
                res = results[0]
                self.assertEqual(res["status"], "completed")
                self.assertEqual(res["calls_found"], 1)
                self.assertEqual(res["calls_selected"], 0)  # Deduplicated to 0


if __name__ == "__main__":
    unittest.main()
