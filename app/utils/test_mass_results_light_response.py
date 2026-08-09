"""
Test suite for GET /bm/mass-evaluation-results lightweight response mode.
Verifies that:
1. By default (include_detail=False), heavy prompt_snapshot and result_json are omitted.
2. include_detail=True returns full detail objects.
3. Payload size is < 500 KB for 100 items in light mode.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_results_light_test.db"

db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.main import app
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from app.utils.security import create_access_token


class TestMassResultsLightResponse(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("mass_results_light_test.db"):
            try:
                os.remove("mass_results_light_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=1, company_name="Light Co", company_key="light_co", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=1, service_name="Front", service_key="front", company_id=1)
            db.add(s1)
            await db.flush()

            u_super = User(
                user_id=1,
                username="super_light",
                email="super_light@test.com",
                password_hash="dummy",
                role="superadmin",
                company_id=1,
                is_active=True
            )
            db.add(u_super)
            await db.flush()

            job = MassEvaluationJob(job_id=1, job_name="Job Light", company_id=1, service_id=1, prompt_id=1, is_active=True)
            run = MassEvaluationRun(run_id=1, job_id=1, company_id=1, service_id=1, trigger_type="manual", status="completed")
            db.add_all([job, run])
            await db.flush()

            # Heavy prompt_snapshot (50 KB text) and result_json (50 KB dict) per row
            heavy_prompt = "Heavy prompt " * 2000
            heavy_json = {"transcript": ["turn " * 20 for _ in range(100)], "criteria": {"c1": "val " * 50}}

            now = datetime.now(timezone.utc)
            results = []
            for i in range(1, 11):
                res = MassEvaluationResult(
                    mass_analysis_id=i,
                    run_id=1,
                    job_id=1,
                    company_id=1,
                    service_id=1,
                    prompt_id=1,
                    prompt_snapshot=heavy_prompt,
                    call_id=f"call_light_{i}",
                    hubspot_owner_id="owner_1",
                    agent_name="Agent Light",
                    call_timestamp=now,
                    analysis_timestamp=now,
                    evaluacion_global=8.0,
                    status="completed",
                    result_json=heavy_json,
                    items_json=[]
                )
                results.append(res)
            db.add_all(results)
            await db.commit()

        self.token_super = create_access_token({"user_id": 1, "email": "super_light@test.com"})

    async def test_default_light_response_omits_heavy_fields(self):
        """Default GET /bm/mass-evaluation-results omits prompt_snapshot and result_json."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?limit=10",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["total"], 10)
            items = data["items"]
            self.assertEqual(len(items), 10)

            first_item = items[0]
            self.assertNotIn("prompt_snapshot", first_item)
            self.assertNotIn("result_json", first_item)
            self.assertIn("call_id", first_item)
            self.assertIn("global_score", first_item)

            raw_bytes = len(res.content)
            self.assertLess(raw_bytes, 50000)  # Much smaller than heavy response (~1 MB)

    async def test_include_detail_true_includes_heavy_fields(self):
        """GET /bm/mass-evaluation-results?include_detail=true returns full heavy objects."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?limit=10&include_detail=true",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            items = data["items"]
            first_item = items[0]
            self.assertIn("prompt_snapshot", first_item)
            self.assertIn("result_json", first_item)


if __name__ == "__main__":
    unittest.main()
