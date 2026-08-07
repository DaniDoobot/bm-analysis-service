"""
Test Suite: test_analytics_smoke_endpoints.py
Smoke test contract for all analytics endpoints.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///bm_perf_test.db"

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.db import get_engine, Base
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.mass_evaluations import MassEvaluationResult
from app.services.dashboard_service import get_dashboard_summary, get_objections_breakdown
from app.services.service_evolution_service import ServiceEvolutionService
from app.routers.analytics import get_agents_comparison, get_items_evolution
from sqlalchemy.ext.asyncio import AsyncSession

async def run_tests():
    engine = get_engine()
    if os.path.exists("bm_perf_test.db"):
        try:
            os.remove("bm_perf_test.db")
        except Exception:
            pass

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine) as db:
        now = datetime.now(timezone.utc)
        res = MassEvaluationResult(
            mass_analysis_id=1,
            run_id=1,
            job_id=1,
            prompt_id=1,
            prompt_snapshot="Test prompt snapshot",
            call_id="call_smoke_1",
            company_id=1,
            service_id=1,
            service_key="front",
            service_name="Front",
            hubspot_owner_id="owner_1",
            agent_name="Agent 1",
            call_timestamp=now,
            evaluacion_global=9.0,
            status="completed"
        )
        db.add(res)
        await db.commit()

    context = TenantContext(
        user_id=1,
        raw_role="superadmin",
        normalized_role=InternalRole.SUPER_ADMIN,
        is_super_admin=True,
        company_id=1,
        allowed_company_ids=[1],
        allowed_service_ids=None,
        allowed_agent_ids=None
    )

    async with AsyncSession(engine) as db:
        print("1. Smoke test Dashboard Summary...")
        summary = await get_dashboard_summary(db=db, context=context)
        assert summary is not None, "Summary should not be None"

        print("2. Smoke test Dashboard Objections...")
        obj = await get_objections_breakdown(db=db, context=context)
        assert obj is not None, "Objections should not be None"

        print("3. Smoke test Agents Comparison...")
        comp = await get_agents_comparison(context=context, db=db)
        assert comp is not None, "Agents comparison should not be None"

        print("4. Smoke test Items Evolution...")
        evo = await get_items_evolution(context=context, db=db)
        assert evo is not None, "Items evolution should not be None"

        print("5. Smoke test Service Evolution...")
        sevo = await ServiceEvolutionService.get_evolution(db=db, context=context, service_id=1)
        assert sevo is not None, "Service evolution should not be None"

    print("PASS: test_analytics_smoke_endpoints all 5 endpoints verified cleanly.")

if __name__ == "__main__":
    asyncio.run(run_tests())
