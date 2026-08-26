"""
Test Suite: test_analytics_normalize_status_regression.py
Regression tests for:
- normalize_status import in analytics router
- http_status alias avoiding status query param shadowing
- get_items_evolution and get_agents_comparison with and without status parameter
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///bm_test_analytics_status.db"

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.db import get_engine, Base
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.mass_evaluations import MassEvaluationResult
from app.models.services import Service
from app.routers.analytics import get_agents_comparison, get_items_evolution
from fastapi import HTTPException, status as http_status
from app.utils.cache import analytics_cache
from sqlalchemy.ext.asyncio import AsyncSession


class TestAnalyticsStatusRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        analytics_cache.clear()
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            svc = Service(
                service_id=1,
                company_id=1,
                service_key="front",
                service_name="Front",
                is_active=True
            )
            db.add(svc)

            now = datetime.now(timezone.utc)
            res = MassEvaluationResult(
                mass_analysis_id=1,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="Test prompt snapshot",
                call_id="call_status_test_1",
                company_id=1,
                service_id=1,
                service_key="front",
                service_name="Front",
                hubspot_owner_id="owner_test_1",
                agent_name="Agent Test 1",
                call_timestamp=now,
                evaluacion_global=8.5,
                status="completed"
            )
            db.add(res)
            await db.commit()

        self.context = TenantContext(
            user_id=1,
            company_id=1,
            role="admin",
            raw_role="admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1],
            allowed_service_ids=None,
            allowed_agent_ids=None,
        )

    async def asyncTearDown(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        if os.path.exists("bm_test_analytics_status.db"):
            try:
                os.remove("bm_test_analytics_status.db")
            except Exception:
                pass

    async def test_01_items_evolution_without_status(self):
        """1. items-evolution sin status -> no 500"""
        async with AsyncSession(self.engine) as db:
            res = await get_items_evolution(
                context=self.context,
                db=db,
                service_id=1,
                status=None,
            )
            self.assertIsInstance(res, list)

    async def test_02_items_evolution_status_completed(self):
        """2. items-evolution status=completed -> no 500"""
        async with AsyncSession(self.engine) as db:
            res = await get_items_evolution(
                context=self.context,
                db=db,
                service_id=1,
                status="completed",
            )
            self.assertIsInstance(res, list)

    async def test_03_agents_comparison_without_status(self):
        """3. agents-comparison sin status -> no 500"""
        async with AsyncSession(self.engine) as db:
            res = await get_agents_comparison(
                context=self.context,
                db=db,
                service_id=1,
                status=None,
            )
            self.assertTrue(hasattr(res, "agents"))
            self.assertTrue(hasattr(res, "comparison"))

    async def test_04_agents_comparison_status_completed(self):
        """4. agents-comparison status=completed -> no 500"""
        async with AsyncSession(self.engine) as db:
            res = await get_agents_comparison(
                context=self.context,
                db=db,
                service_id=1,
                status="completed",
            )
            self.assertTrue(hasattr(res, "agents"))
            self.assertTrue(hasattr(res, "comparison"))

    async def test_05_exception_handler_uses_http_status_without_shadowing(self):
        """5. Exception handler in get_agents_comparison and get_items_evolution raises HTTPException(500) cleanly."""
        async with AsyncSession(self.engine) as db:
            # Force an internal unexpected error inside get_agents_comparison
            with patch("app.routers.analytics.resolve_service_id", side_effect=RuntimeError("Simulated unexpected failure")):
                with self.assertRaises(HTTPException) as cm:
                    await get_agents_comparison(
                        context=self.context,
                        db=db,
                        service_id=1,
                        status="completed",  # param status passed as string, would shadow if not aliased
                    )
                self.assertEqual(cm.exception.status_code, 500)
                self.assertIn("Simulated unexpected failure", cm.exception.detail)

            # Force an internal unexpected error inside get_items_evolution
            with patch("app.routers.analytics.resolve_service_id", side_effect=RuntimeError("Simulated evolution failure")):
                with self.assertRaises(HTTPException) as cm:
                    await get_items_evolution(
                        context=self.context,
                        db=db,
                        service_id=1,
                        status="completed",
                    )
                self.assertEqual(cm.exception.status_code, 500)
                self.assertIn("Simulated evolution failure", cm.exception.detail)


if __name__ == "__main__":
    unittest.main()
