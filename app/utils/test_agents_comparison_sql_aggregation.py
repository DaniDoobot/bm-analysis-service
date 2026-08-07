"""
Test Suite: test_agents_comparison_sql_aggregation.py
Validates performance and correctness of get_agents_comparison endpoint.
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
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.routers.analytics import get_agents_comparison
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
        for i in range(10):
            agent_id = "agent_1" if i % 2 == 0 else "agent_2"
            res = MassEvaluationResult(
                mass_analysis_id=i + 1,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="Test prompt snapshot",
                call_id=f"call_{i}",
                company_id=1,
                service_id=1,
                hubspot_owner_id=agent_id,
                agent_name=f"Agent {agent_id}",
                call_timestamp=now,
                evaluacion_global=8.5 if i % 2 == 0 else 7.0,
                status="completed"
            )
            db.add(res)
            
            crit = MassEvaluationCriterionResult(
                id=i + 1,
                mass_analysis_id=i + 1,
                run_id=1,
                job_id=1,
                call_id=f"call_{i}",
                criterion_key="claridad",
                criterion_name="Claridad",
                criterion_type="score",
                numeric_value=8.0 if i % 2 == 0 else 6.0,
                is_applicable=True
            )
            db.add(crit)
        
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
        print("Testing get_agents_comparison query...")
        t0 = asyncio.get_event_loop().time()
        resp = await get_agents_comparison(
            context=context,
            db=db,
            date_from="2026-01-01",
            date_to="2026-12-31"
        )
        t1 = asyncio.get_event_loop().time()
        elapsed_ms = (t1 - t0) * 1000.0

        print(f"Executed get_agents_comparison in {elapsed_ms:.2f} ms")
        assert resp is not None, "Response must not be None"
        assert len(resp.agents) >= 2, f"Expected at least 2 agents, got {len(resp.agents)}"
        assert len(resp.comparison) > 0, "Comparison rows must not be empty"
        print("PASS: test_agents_comparison_sql_aggregation")

if __name__ == "__main__":
    asyncio.run(run_tests())
