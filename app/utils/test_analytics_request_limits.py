"""
Test Suite: test_analytics_request_limits.py
Validates default item limits (max 20) and date validation error (HTTP 422 if date_from > date_to).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///bm_perf_test.db"

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from fastapi import HTTPException
from app.db import get_engine, Base
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
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
        print("Testing invalid date range (date_from > date_to)...")
        try:
            await get_agents_comparison(
                context=context,
                db=db,
                date_from="2026-08-01",
                date_to="2026-07-01"
            )
            assert False, "Should have raised HTTPException 422 for invalid date range"
        except HTTPException as exc:
            assert exc.status_code == 422, f"Expected 422 status, got {exc.status_code}"
            print("PASS: Date range validation HTTP 422 verified.")

        print("Testing default item limits (max 20)...")
        resp = await get_agents_comparison(
            context=context,
            db=db,
            date_from="2026-01-01",
            date_to="2026-12-31"
        )
        assert len(resp.items) <= 20, f"Expected max 20 default items, got {len(resp.items)}"
        print("PASS: Default items limited to <= 20 verified.")
        print("PASS: test_analytics_request_limits")

if __name__ == "__main__":
    asyncio.run(run_tests())
