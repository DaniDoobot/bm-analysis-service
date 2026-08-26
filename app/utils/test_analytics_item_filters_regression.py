"""
Test Suite: test_analytics_item_filters_regression.py
Regression tests for item_filters in Analytics v2:
- GET /bm/analytics/agents-comparison supports item_filters (score ranges, multiple AND criteria, booleans)
- GET /bm/analytics/items-evolution supports item_filters
- Cache key differentiation ensures distinct results are not overwritten or improperly reused across item_filters variants
"""
import asyncio
import os
import sys
import json
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///bm_test_item_filters.db"

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.db import get_engine, Base
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.models.services import Service
from app.routers.analytics import get_agents_comparison, get_items_evolution
from app.utils.cache import analytics_cache
from sqlalchemy.ext.asyncio import AsyncSession


class TestAnalyticsItemFiltersRegression(unittest.IsolatedAsyncioTestCase):
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
            # Call 1: conexion_emocional=9, empatia=8, cierre_cita=True
            db.add(MassEvaluationResult(
                mass_analysis_id=1, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="Snapshot",
                call_id="c1", company_id=1, service_id=1, service_key="front",
                hubspot_owner_id="owner_1", agent_name="Agent One",
                call_timestamp=now, evaluacion_global=9.0, status="completed", is_evaluable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=1, mass_analysis_id=1, run_id=1, job_id=1, call_id="c1", criterion_key="conexion_emocional",
                numeric_value=9.0, is_applicable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=2, mass_analysis_id=1, run_id=1, job_id=1, call_id="c1", criterion_key="empatia",
                numeric_value=8.0, is_applicable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=3, mass_analysis_id=1, run_id=1, job_id=1, call_id="c1", criterion_key="cierre_cita",
                boolean_value=True, text_value="Sí", is_applicable=True
            ))

            # Call 2: conexion_emocional=6, empatia=5.5, cierre_cita=False
            db.add(MassEvaluationResult(
                mass_analysis_id=2, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="Snapshot",
                call_id="c2", company_id=1, service_id=1, service_key="front",
                hubspot_owner_id="owner_1", agent_name="Agent One",
                call_timestamp=now, evaluacion_global=6.0, status="completed", is_evaluable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=4, mass_analysis_id=2, run_id=1, job_id=1, call_id="c2", criterion_key="conexion_emocional",
                numeric_value=6.0, is_applicable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=5, mass_analysis_id=2, run_id=1, job_id=1, call_id="c2", criterion_key="empatia",
                numeric_value=5.5, is_applicable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=6, mass_analysis_id=2, run_id=1, job_id=1, call_id="c2", criterion_key="cierre_cita",
                boolean_value=False, text_value="No", is_applicable=True
            ))

            # Call 3: conexion_emocional=6, empatia=5.5, cierre_cita=True
            db.add(MassEvaluationResult(
                mass_analysis_id=3, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="Snapshot",
                call_id="c3", company_id=1, service_id=1, service_key="front",
                hubspot_owner_id="owner_1", agent_name="Agent One",
                call_timestamp=now, evaluacion_global=6.0, status="completed", is_evaluable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=7, mass_analysis_id=3, run_id=1, job_id=1, call_id="c3", criterion_key="conexion_emocional",
                numeric_value=6.0, is_applicable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=8, mass_analysis_id=3, run_id=1, job_id=1, call_id="c3", criterion_key="empatia",
                numeric_value=5.5, is_applicable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=9, mass_analysis_id=3, run_id=1, job_id=1, call_id="c3", criterion_key="cierre_cita",
                boolean_value=True, text_value="Sí", is_applicable=True
            ))

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
        if os.path.exists("bm_test_item_filters.db"):
            try:
                os.remove("bm_test_item_filters.db")
            except Exception:
                pass

    async def test_01_agents_comparison_item_filters_cases_a_to_e(self):
        """Verify that item_filters restricts calls correctly across cases A-E."""
        async with AsyncSession(self.engine) as db:
            # Caso A: sin item_filters -> 3 calls
            res_a = await get_agents_comparison(
                context=self.context, db=db, service_id=1, status="completed"
            )
            count_a = sum(r.count for r in res_a.comparison if r.item_key == "evaluacion_global")
            self.assertEqual(count_a, 3)

            # Caso B: conexion_emocional 5-10 -> 3 calls (c1=9, c2=6, c3=6)
            res_b = await get_agents_comparison(
                context=self.context, db=db, service_id=1, status="completed",
                item_filters=json.dumps([{"key": "conexion_emocional", "min": 5, "max": 10}])
            )
            count_b = sum(r.count for r in res_b.comparison if r.item_key == "evaluacion_global")
            self.assertEqual(count_b, 3)

            # Caso C: conexion_emocional 5-8 -> 2 calls (c2=6, c3=6)
            res_c = await get_agents_comparison(
                context=self.context, db=db, service_id=1, status="completed",
                item_filters=json.dumps([{"key": "conexion_emocional", "min": 5, "max": 8}])
            )
            count_c = sum(r.count for r in res_c.comparison if r.item_key == "evaluacion_global")
            self.assertEqual(count_c, 2)

            # Caso D: conexion_emocional 5-8 AND empatia 5-6 -> 2 calls (c2, c3)
            res_d = await get_agents_comparison(
                context=self.context, db=db, service_id=1, status="completed",
                item_filters=json.dumps([
                    {"key": "conexion_emocional", "min": 5, "max": 8},
                    {"key": "empatia", "min": 5, "max": 6}
                ])
            )
            count_d = sum(r.count for r in res_d.comparison if r.item_key == "evaluacion_global")
            self.assertEqual(count_d, 2)

            # Caso E: conexion_emocional 5-8 AND empatia 5-6 AND cierre_cita=false -> 1 call (c2)
            res_e = await get_agents_comparison(
                context=self.context, db=db, service_id=1, status="completed",
                item_filters=json.dumps([
                    {"key": "conexion_emocional", "min": 5, "max": 8},
                    {"key": "empatia", "min": 5, "max": 6},
                    {"key": "cierre_cita", "value": False}
                ])
            )
            count_e = sum(r.count for r in res_e.comparison if r.item_key == "evaluacion_global")
            self.assertEqual(count_e, 1)

    async def test_02_items_evolution_with_item_filters(self):
        """Verify items_evolution accepts and applies item_filters."""
        async with AsyncSession(self.engine) as db:
            res_evo = await get_items_evolution(
                context=self.context, db=db, service_id=1, status="completed",
                item_filters=json.dumps([{"key": "cierre_cita", "value": False}])
            )
            self.assertIsInstance(res_evo, list)
            for s in res_evo:
                if s.item_key == "evaluacion_global":
                    total_points_count = sum(p.count for p in s.points)
                    self.assertEqual(total_points_count, 1)

    async def test_03_cache_key_differentiation_for_item_filters(self):
        """Verify that sequential calls with different item_filters do NOT return stale cached data."""
        async with AsyncSession(self.engine) as db:
            # 1. First call with max=10
            filter_10 = json.dumps([{"key": "conexion_emocional", "min": 5, "max": 10}])
            res_10 = await get_agents_comparison(
                context=self.context, db=db, service_id=1, status="completed", item_filters=filter_10
            )
            count_10 = sum(r.count for r in res_10.comparison if r.item_key == "evaluacion_global")
            self.assertEqual(count_10, 3)

            # 2. Second call with max=8 (must miss cache and compute distinct count)
            filter_8 = json.dumps([{"key": "conexion_emocional", "min": 5, "max": 8}])
            res_8 = await get_agents_comparison(
                context=self.context, db=db, service_id=1, status="completed", item_filters=filter_8
            )
            count_8 = sum(r.count for r in res_8.comparison if r.item_key == "evaluacion_global")
            self.assertEqual(count_8, 2)

            # 3. Third call adding boolean filter
            filter_bool = json.dumps([
                {"key": "conexion_emocional", "min": 5, "max": 8},
                {"key": "cierre_cita", "value": False}
            ])
            res_bool = await get_agents_comparison(
                context=self.context, db=db, service_id=1, status="completed", item_filters=filter_bool
            )
            count_bool = sum(r.count for r in res_bool.comparison if r.item_key == "evaluacion_global")
            self.assertEqual(count_bool, 1)


if __name__ == "__main__":
    unittest.main()
