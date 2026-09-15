"""
Unit tests for Analytics Cache Refresh (Bypass Cache).
Tests:
1. AnalyticsCache unit tests:
   - Default: repeated calls return cached entry (cache hit).
   - refresh=True (bypass_cache=True): calls compute_fn again and updates cache with fresh data.
2. Endpoint integration tests for get_agents_comparison and get_items_evolution:
   - refresh=False (default) uses cached result.
   - refresh=True bypasses cached result and returns freshly computed data.
   - Schema and contract remain identical.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///bm_test_analytics_refresh.db"

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.db import get_engine, Base
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.mass_evaluations import MassEvaluationResult
from app.models.services import Service
from app.routers.analytics import get_agents_comparison, get_items_evolution
from app.schemas.analytics import AgentComparisonResponse
from app.utils.cache import AnalyticsCache, analytics_cache
from sqlalchemy.ext.asyncio import AsyncSession


class TestAnalyticsCacheUnit(unittest.IsolatedAsyncioTestCase):
    async def test_cache_hit_and_refresh_bypass(self):
        cache = AnalyticsCache(default_ttl=30, max_entries=10)
        call_count = 0

        async def compute():
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        # 1. First call computes
        val1, hit1 = await cache.get_or_compute("test_key", compute, ttl=30, bypass_cache=False)
        self.assertFalse(hit1)
        self.assertEqual(val1["count"], 1)
        self.assertEqual(call_count, 1)

        # 2. Second call with bypass_cache=False -> returns cached entry
        val2, hit2 = await cache.get_or_compute("test_key", compute, ttl=30, bypass_cache=False)
        self.assertTrue(hit2)
        self.assertEqual(val2["count"], 1)
        self.assertEqual(call_count, 1)

        # 3. Third call with bypass_cache=True -> recomputes and returns fresh entry
        val3, hit3 = await cache.get_or_compute("test_key", compute, ttl=30, bypass_cache=True)
        self.assertFalse(hit3)
        self.assertEqual(val3["count"], 2)
        self.assertEqual(call_count, 2)

        # 4. Subsequent normal call uses newly cached value
        val4, hit4 = await cache.get_or_compute("test_key", compute, ttl=30, bypass_cache=False)
        self.assertTrue(hit4)
        self.assertEqual(val4["count"], 2)
        self.assertEqual(call_count, 2)


class TestAnalyticsEndpointsRefresh(unittest.IsolatedAsyncioTestCase):
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
                call_id="call_refresh_test_1",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="101",
                agent_name="Agent 101",
                call_timestamp=now,
                status="completed",
                evaluacion_global=8.5,
                result_json={"evaluacion_global": 8.5}
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
        if os.path.exists("bm_test_analytics_refresh.db"):
            try:
                os.remove("bm_test_analytics_refresh.db")
            except Exception:
                pass

    async def test_agents_comparison_refresh_behavior(self):
        async with AsyncSession(self.engine) as db:
            # First call: populates cache
            res1 = await get_agents_comparison(
                context=self.context,
                db=db,
                service_id=1,
                refresh=False,
            )
            self.assertIsInstance(res1, AgentComparisonResponse)

            # Mutate cached object to test whether cache is returned
            cache_keys = list(analytics_cache._store.keys())
            self.assertTrue(len(cache_keys) > 0)
            target_key = next(k for k in cache_keys if k.startswith("agents_comp:"))
            exp, (cached_resp, sc, ret, db_ms) = analytics_cache._store[target_key]

            # Normal call (refresh=False) must return cached object
            res2 = await get_agents_comparison(
                context=self.context,
                db=db,
                service_id=1,
                refresh=False,
            )
            self.assertIs(res2, cached_resp)

            # Refresh call (refresh=True) must bypass cache and compute fresh response
            res3 = await get_agents_comparison(
                context=self.context,
                db=db,
                service_id=1,
                refresh=True,
            )
            self.assertIsNot(res3, cached_resp)
            self.assertIsInstance(res3, AgentComparisonResponse)
            self.assertEqual(res3.selected_agents_count, res1.selected_agents_count)

    async def test_items_evolution_refresh_behavior(self):
        async with AsyncSession(self.engine) as db:
            res1 = await get_items_evolution(
                context=self.context,
                db=db,
                service_id=1,
                refresh=False,
            )
            self.assertIsInstance(res1, list)

            cache_keys = list(analytics_cache._store.keys())
            target_key = next(k for k in cache_keys if k.startswith("items_evo:"))
            exp, cached_series = analytics_cache._store[target_key]

            # Normal call returns cached object
            res2 = await get_items_evolution(
                context=self.context,
                db=db,
                service_id=1,
                refresh=False,
            )
            self.assertIs(res2, cached_series)

            # Refresh call bypasses cache
            res3 = await get_items_evolution(
                context=self.context,
                db=db,
                service_id=1,
                refresh=True,
            )
            self.assertIsNot(res3, cached_series)
            self.assertIsInstance(res3, list)


def tearDownModule():
    if os.path.exists("bm_test_analytics_refresh.db"):
        try:
            os.remove("bm_test_analytics_refresh.db")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
