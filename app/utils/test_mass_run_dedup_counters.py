"""
Test suite for Mass Run Deduplication & Counter Integrity.
Verifies that:
1. Re-evaluating a call updates the existing MassEvaluationResult via upsert.
2. The row count in bm_mass_evaluation_results does NOT duplicate.
3. Source_run_id retains initial run history while run_id points to the latest run.
4. Queries by initial run_id (source_run_id) still find the re-evaluated call.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_dedup_counters_test.db"

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

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult
from app.services.mass_evaluation_service import MassEvaluationService


class TestMassRunDedupCounters(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("mass_dedup_counters_test.db"):
            try:
                os.remove("mass_dedup_counters_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=40, company_name="Dedup Co", company_key="dedup_co", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=40, service_name="Dedup Service", service_key="dedup_svc", company_id=40)
            db.add(s1)
            await db.flush()

            # First run (run_id=80): insert initial result
            row1 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=80,
                job_id=40,
                execution_source="automation",
                call_id="call_dedup_100",
                prompt_id=1,
                defaults={
                    "hs_object_id": "hs_dedup_100",
                    "hubspot_owner_id": "owner_40",
                    "agent_name": "Agent 40",
                    "evaluacion_global": 7.0,
                    "prompt_snapshot": "Analiza la llamada.",
                    "status": "completed",
                    "company_id": 40,
                    "service_id": 40,
                    "result_json": {"tipo_llamada": "front", "evaluacion_global": 7.0},
                    "items_json": []
                }
            )
            await db.commit()

            # Second run (run_id=81): re-evaluates the same call_id="call_dedup_100"
            row2 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=81,
                job_id=40,
                execution_source="automation",
                call_id="call_dedup_100",
                prompt_id=1,
                defaults={
                    "hs_object_id": "hs_dedup_100",
                    "hubspot_owner_id": "owner_40",
                    "agent_name": "Agent 40",
                    "evaluacion_global": 8.5,
                    "status": "completed",
                    "company_id": 40,
                    "service_id": 40,
                    "result_json": {"tipo_llamada": "front", "evaluacion_global": 8.5},
                    "items_json": []
                }
            )
            await db.commit()

    async def test_upsert_deduplication_and_source_run_id(self):
        """Verifies that re-evaluation updates existing record, preserves source_run_id=80, and updates run_id=81."""
        async with AsyncSession(self.engine) as db:
            count_stmt = select(func.count(MassEvaluationResult.mass_analysis_id)).where(MassEvaluationResult.call_id == "call_dedup_100")
            total_count = (await db.execute(count_stmt)).scalar()
            self.assertEqual(total_count, 1, "There must be exactly 1 row for call_dedup_100 after re-evaluation")

            res_stmt = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_dedup_100")
            res = (await db.execute(res_stmt)).scalars().first()

            self.assertEqual(res.run_id, 81, "Latest run_id must be 81")
            self.assertEqual(res.source_run_id, 80, "Initial source_run_id must be 80")
            self.assertEqual(float(res.evaluacion_global), 8.5, "Evaluacion_global must be updated to 8.5")

            # Query by initial run_id=80 using list_results (should still match via source_run_id)
            results_for_80 = await MassEvaluationService.list_results(db, run_id=80)
            self.assertEqual(len(results_for_80), 1, "Search by initial run_id=80 must find the re-evaluated call")

            # Query by latest run_id=81 using list_results
            results_for_81 = await MassEvaluationService.list_results(db, run_id=81)
            self.assertEqual(len(results_for_81), 1, "Search by latest run_id=81 must find the re-evaluated call")


if __name__ == "__main__":
    unittest.main()
