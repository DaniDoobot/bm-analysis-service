"""
Regression test suite for GET /bm/me/analysis-results (Agent Results View).
Verifies:
1. No NameError on result_status or status
2. status filtering (None, completed, failed, all)
3. Date range filtering and Madrid timezone resolution
4. item_filters integration
5. Sorting normalization
6. Strict agent scope isolation (security check)
7. Empty results for agent with no calls (returns 200 with empty list, not error)
8. Backward-compatible aliases (eval_min, typology, etc.)
"""
import os
import sys
import json
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///bm_test_me_results.db"

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.db import Base, get_engine
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.models.services import Service
from app.routers.mass_evaluations import get_my_analysis_results, get_result
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.utils.cache import analytics_cache
from fastapi import HTTPException, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession


class TestMeAnalysisResultsRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        analytics_cache.clear()
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Seed data
        async with AsyncSession(self.engine) as db:
            await db.execute(MassEvaluationCriterionResult.__table__.delete())
            await db.execute(MassEvaluationResult.__table__.delete())
            await db.execute(Service.__table__.delete())
            await db.commit()

            service = Service(
                service_id=1,
                service_key="front",
                service_name="Front Service",
                company_id=1
            )
            db.add(service)

            now = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
            old_date = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

            # Agent 1 (owner_1) - 3 calls: 2 completed, 1 failed
            # Call 1: completed, recent
            db.add(MassEvaluationResult(
                mass_analysis_id=1, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="Snapshot",
                call_id="c1", company_id=1, service_id=1, service_key="front",
                hubspot_owner_id="owner_1", agent_name="Agent One",
                call_timestamp=now, evaluacion_global=9.0, status="completed", is_evaluable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=1, mass_analysis_id=1, run_id=1, job_id=1, call_id="c1", criterion_key="empatia",
                numeric_value=8.5, is_applicable=True
            ))

            # Call 2: completed, old date
            db.add(MassEvaluationResult(
                mass_analysis_id=2, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="Snapshot",
                call_id="c2", company_id=1, service_id=1, service_key="front",
                hubspot_owner_id="owner_1", agent_name="Agent One",
                call_timestamp=old_date, evaluacion_global=7.0, status="completed", is_evaluable=True
            ))
            db.add(MassEvaluationCriterionResult(
                id=2, mass_analysis_id=2, run_id=1, job_id=1, call_id="c2", criterion_key="empatia",
                numeric_value=6.0, is_applicable=True
            ))

            # Call 3: failed
            db.add(MassEvaluationResult(
                mass_analysis_id=3, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="Snapshot",
                call_id="c3", company_id=1, service_id=1, service_key="front",
                hubspot_owner_id="owner_1", agent_name="Agent One",
                call_timestamp=now, evaluacion_global=None, status="failed", is_evaluable=False
            ))

            # Agent 2 (owner_2) - 1 call
            db.add(MassEvaluationResult(
                mass_analysis_id=4, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="Snapshot",
                call_id="c4", company_id=1, service_id=1, service_key="front",
                hubspot_owner_id="owner_2", agent_name="Agent Two",
                call_timestamp=now, evaluacion_global=8.0, status="completed", is_evaluable=True
            ))

            # Company 2 - 1 call (owner_1 in another company)
            db.add(MassEvaluationResult(
                mass_analysis_id=5, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="Snapshot",
                call_id="c5", company_id=2, service_id=2, service_key="other",
                hubspot_owner_id="owner_1", agent_name="Agent One",
                call_timestamp=now, evaluacion_global=8.0, status="completed", is_evaluable=True
            ))
            await db.commit()

        self.admin_context = TenantContext(
            user_id=1,
            company_id=1,
            role="admin",
            raw_role="admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1, 2],
            allowed_service_ids=None,
            allowed_agent_ids=None,
        )

        self.agent1_context = TenantContext(
            user_id=101,
            company_id=1,
            role="agent",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=[],  # Typical for agent without explicit team/service assignments
            allowed_agent_ids=["owner_1"],
        )

        self.agent_no_data_context = TenantContext(
            user_id=102,
            company_id=1,
            role="agent",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=None,
            allowed_agent_ids=["owner_empty"],
        )

    async def test_01_no_status_does_not_throw_name_error(self):
        """Regression test for NameError: result_status is not defined when no status is provided."""
        async with AsyncSession(self.engine) as db:
            res = await get_my_analysis_results(
                context=self.agent1_context,
                db=db,
            )
            self.assertIsNotNone(res)
            # Default includes completed evaluations for owner_1
            self.assertGreaterEqual(res.total, 2)
            for item in res.items:
                self.assertEqual(item.hubspot_owner_id, "owner_1")

    async def test_02_status_completed(self):
        """Status completed returns only completed records for the agent."""
        async with AsyncSession(self.engine) as db:
            res = await get_my_analysis_results(
                context=self.agent1_context,
                db=db,
                status="completed"
            )
            self.assertEqual(res.total, 2)
            statuses = {it.status for it in res.items}
            self.assertEqual(statuses, {"completed"})

    async def test_03_status_failed(self):
        """Status failed returns only failed records for the agent."""
        async with AsyncSession(self.engine) as db:
            res = await get_my_analysis_results(
                context=self.agent1_context,
                db=db,
                status="failed"
            )
            self.assertEqual(res.total, 1)
            self.assertEqual(res.items[0].status, "failed")
            self.assertEqual(res.items[0].call_id, "c3")

    async def test_04_status_all(self):
        """Status all returns completed and failed records for the agent."""
        async with AsyncSession(self.engine) as db:
            res = await get_my_analysis_results(
                context=self.agent1_context,
                db=db,
                status="all"
            )
            self.assertEqual(res.total, 3)

    async def test_05_date_filtering(self):
        """Date filtering correctly restricts calls by date bounds."""
        async with AsyncSession(self.engine) as db:
            # Query only August 2026 -> excludes old_date (July)
            res = await get_my_analysis_results(
                context=self.agent1_context,
                db=db,
                date_from="2026-08-20",
                date_to="2026-08-26",
                status="completed"
            )
            self.assertEqual(res.total, 1)
            self.assertEqual(res.items[0].call_id, "c1")

    async def test_06_item_filters_applied(self):
        """item_filters restricts agent results by score criterion."""
        async with AsyncSession(self.engine) as db:
            # Filter empatia >= 8.0 -> matches c1 (8.5), excludes c2 (6.0)
            res = await get_my_analysis_results(
                context=self.agent1_context,
                db=db,
                status="completed",
                item_filters=json.dumps([{"key": "empatia", "min": 8.0, "max": 10.0}])
            )
            self.assertEqual(res.total, 1)
            self.assertEqual(res.items[0].call_id, "c1")

    async def test_07_sorting_and_aliases(self):
        """Sort by date asc and desc works properly with aliases."""
        async with AsyncSession(self.engine) as db:
            res_asc = await get_my_analysis_results(
                context=self.agent1_context,
                db=db,
                status="completed",
                sort_by="date",
                sort_order="asc"
            )
            self.assertEqual(res_asc.items[0].call_id, "c2")
            self.assertEqual(res_asc.items[1].call_id, "c1")

    async def test_08_agent_scope_security(self):
        """Agent cannot access another agent's data even if passing agent_owner_id."""
        async with AsyncSession(self.engine) as db:
            # Attempting to pass owner_2 when logged in as owner_1 raises 403
            with self.assertRaises(HTTPException) as ctx:
                await get_my_analysis_results(
                    context=self.agent1_context,
                    db=db,
                    agent_owner_id="owner_2"
                )
            self.assertEqual(ctx.exception.status_code, http_status.HTTP_403_FORBIDDEN)

    async def test_09_agent_with_no_data_returns_empty_200(self):
        """Agent with no evaluations receives 200 with empty list, NOT 500 error."""
        async with AsyncSession(self.engine) as db:
            res = await get_my_analysis_results(
                context=self.agent_no_data_context,
                db=db,
            )
            self.assertEqual(res.total, 0)
            self.assertEqual(len(res.items), 0)

    async def test_10_frontend_aliases_compatibility(self):
        """Aliases like eval_min, result_status, order_by, order don't raise 422."""
        async with AsyncSession(self.engine) as db:
            res = await get_my_analysis_results(
                context=self.agent1_context,
                db=db,
                eval_min=8.0,
                result_status="completed",
                order_by="date",
                order="desc"
            )
            self.assertEqual(res.total, 1)
            self.assertEqual(res.items[0].call_id, "c1")

    async def test_11_admin_opens_any_evaluation_returns_200(self):
        """Admin can open any evaluation detail in their allowed companies."""
        async with AsyncSession(self.engine) as db:
            detail = await get_result(mass_analysis_id=1, context=self.admin_context, db=db)
            self.assertEqual(detail.mass_analysis_id, 1)
            self.assertEqual(detail.call_id, "c1")
            self.assertEqual(detail.prompt_snapshot, "Snapshot")

    async def test_12_agent_opens_own_evaluation_returns_200(self):
        """Agent can open their own evaluation detail (even with allowed_service_ids=[])."""
        async with AsyncSession(self.engine) as db:
            detail = await get_result(mass_analysis_id=1, context=self.agent1_context, db=db)
            self.assertEqual(detail.mass_analysis_id, 1)
            self.assertEqual(detail.call_id, "c1")
            self.assertEqual(detail.hubspot_owner_id, "owner_1")
            self.assertEqual(detail.prompt_snapshot, "Snapshot")

    async def test_13_agent_opens_other_agent_evaluation_returns_403(self):
        """Agent receives 403 when trying to open another agent's evaluation."""
        async with AsyncSession(self.engine) as db:
            with self.assertRaises(HTTPException) as ctx:
                await get_result(mass_analysis_id=4, context=self.agent1_context, db=db)
            self.assertEqual(ctx.exception.status_code, http_status.HTTP_403_FORBIDDEN)
            self.assertIn("No tienes permiso para consultar este análisis", ctx.exception.detail)

    async def test_14_agent_opens_other_company_evaluation_returns_403(self):
        """Agent receives 403 when trying to open evaluation belonging to another company."""
        async with AsyncSession(self.engine) as db:
            with self.assertRaises(HTTPException) as ctx:
                await get_result(mass_analysis_id=5, context=self.agent1_context, db=db)
            self.assertEqual(ctx.exception.status_code, http_status.HTTP_403_FORBIDDEN)
            self.assertIn("otra empresa", ctx.exception.detail)

    async def test_15_nonexistent_evaluation_returns_404(self):
        """Requesting non-existent mass_analysis_id returns 404."""
        async with AsyncSession(self.engine) as db:
            with self.assertRaises(HTTPException) as ctx:
                await get_result(mass_analysis_id=99999, context=self.agent1_context, db=db)
            self.assertEqual(ctx.exception.status_code, http_status.HTTP_404_NOT_FOUND)

    async def test_16_payload_integrity_and_data_protection(self):
        """Detail payload contains all UI-required fields and url tampering is prevented."""
        async with AsyncSession(self.engine) as db:
            detail = await get_result(mass_analysis_id=1, context=self.agent1_context, db=db)
            # Verify complete UI-required payload fields
            self.assertEqual(detail.mass_analysis_id, 1)
            self.assertEqual(detail.call_id, "c1")
            self.assertEqual(detail.agent_name, "Agent One")
            self.assertEqual(detail.hubspot_owner_id, "owner_1")
            self.assertEqual(detail.company_id, 1)
            self.assertEqual(detail.service_key, "front")
            self.assertEqual(detail.status, "completed")
            self.assertEqual(detail.global_score, 9.0)
            self.assertIsNotNone(detail.items_visual)
            self.assertEqual(detail.prompt_snapshot, "Snapshot")

            # URL tampering attempt: agent modifying ID in request URL to access ID 4 (other agent)
            with self.assertRaises(HTTPException) as ctx_other_agent:
                await get_result(mass_analysis_id=4, context=self.agent1_context, db=db)
            self.assertEqual(ctx_other_agent.exception.status_code, http_status.HTTP_403_FORBIDDEN)

            # URL tampering attempt: agent modifying ID in request URL to access ID 5 (other company)
            with self.assertRaises(HTTPException) as ctx_other_company:
                await get_result(mass_analysis_id=5, context=self.agent1_context, db=db)
            self.assertEqual(ctx_other_company.exception.status_code, http_status.HTTP_403_FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
