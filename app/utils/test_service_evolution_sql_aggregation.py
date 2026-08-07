"""
Test Suite: test_service_evolution_sql_aggregation.py
Validates performance and correctness of ServiceEvolutionService.get_evolution.
"""
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///bm_perf_test.db"

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.db import get_engine, Base
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.mass_evaluations import MassEvaluationResult
from app.services.service_evolution_service import ServiceEvolutionService
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
        now = datetime(2026, 6, 15, 12, 0, 0)
        for i in range(15):
            res = MassEvaluationResult(
                mass_analysis_id=i + 1,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="Test prompt snapshot",
                call_id=f"call_{i}",
                company_id=1,
                service_id=1,
                service_key="front",
                service_name="Front",
                hubspot_owner_id=f"agent_{i % 3}",
                agent_name=f"Agent {i % 3}",
                call_timestamp=now,
                evaluacion_global=8.0,
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
        print("Testing ServiceEvolutionService.get_evolution...")
        t0 = asyncio.get_event_loop().time()
        resp = await ServiceEvolutionService.get_evolution(
            db=db,
            context=context,
            service_id=1,
            date_from="2026-01-01",
            date_to="2026-12-31"
        )
        t1 = asyncio.get_event_loop().time()
        elapsed_ms = (t1 - t0) * 1000.0

        print(f"Executed ServiceEvolutionService.get_evolution in {elapsed_ms:.2f} ms")
        assert resp is not None, "Response must not be None"
        assert resp.summary.total_calls == 15, f"Expected 15 calls, got {resp.summary.total_calls}"
        print("PASS: test_service_evolution_sql_aggregation")

if __name__ == "__main__":
    asyncio.run(run_tests())
