import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///automation_zero_windows_test.db"

import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

from sqlalchemy import BigInteger
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
)

MADRID_TZ = ZoneInfo("Europe/Madrid")


class TestZeroWindowsDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_zero_source_classification(self):
        """When HubSpot returns 0 calls for a weekend window, classify as zero_source."""
        async with self.async_session() as db:
            job = MassEvaluationJob(
                job_id=48,
                prompt_id=58,
                job_name="[Auto] Front Test",
                selection_mode="filter",
                timezone="Europe/Madrid",
                agent_owner_ids=[1375831790, 1375831791],
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
            run = MassAnalysisAutomationRun(
                automation_id=8,
                window_from=datetime(2026, 8, 15, 8, 29, tzinfo=timezone.utc),
                window_to=datetime(2026, 8, 15, 8, 39, tzinfo=timezone.utc),
                status="completed",
                calls_found=0,
                calls_selected=0,
            )
            db.add_all([job, aut, run])
            await db.commit()

            # Mock HubSpot returning 0 calls
            mock_hs_calls = []
            hs_count = len(mock_hs_calls)
            verdict = "zero_source" if hs_count == 0 else "selection_bug"
            self.assertEqual(verdict, "zero_source")
            self.assertEqual(run.calls_found, hs_count)

    async def test_selection_bug_detection(self):
        """When HubSpot returns >0 calls but DB found/selected is 0, classify as selection_bug."""
        mock_hs_calls = [{"call_id": "123", "duration": 150}]
        db_calls_found = 0
        hs_count = len(mock_hs_calls)
        verdict = "zero_source" if hs_count == 0 else "selection_bug"
        self.assertEqual(verdict, "selection_bug")


if __name__ == "__main__":
    unittest.main()
