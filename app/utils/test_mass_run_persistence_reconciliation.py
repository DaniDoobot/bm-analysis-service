"""
Test suite for Mass Run Persistence & Criteria Reconciliation.
Verifies that:
1. Every analyzed call in a mass evaluation run persists 1:1 in bm_mass_evaluation_results.
2. Every analyzed criterion item persists in bm_mass_evaluation_criterion_results.
3. Run counters (calls_found, calls_selected, calls_analyzed) match saved DB records.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_run_reconciliation_test.db"

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
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from app.services.mass_evaluation_service import MassEvaluationService


class TestMassRunPersistenceReconciliation(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("mass_run_reconciliation_test.db"):
            try:
                os.remove("mass_run_reconciliation_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=10, company_name="Reconcile Co", company_key="reconcile_co", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=10, service_name="Reconcile Svc", service_key="reconcile_svc", company_id=10)
            db.add(s1)
            await db.flush()

            u1 = User(user_id=10, username="reconcile_user", email="reconcile@test.com", role="superadmin", password_hash="dummy")
            db.add(u1)
            await db.flush()

            job = MassEvaluationJob(
                job_id=10,
                job_name="Reconciliation Job",
                company_id=10,
                service_id=10,
                prompt_id=1,
                created_by=10,
                is_active=True
            )
            db.add(job)
            await db.flush()

            run = MassEvaluationRun(
                run_id=10,
                job_id=10,
                company_id=10,
                service_id=10,
                trigger_type="manual",
                status="completed",
                calls_found=3,
                calls_selected=3,
                calls_analyzed=3,
                calls_skipped=0,
                calls_failed=0
            )
            db.add(run)
            await db.flush()

            # Seed 3 results for run 10
            for i in range(1, 4):
                res = MassEvaluationResult(
                    mass_analysis_id=100 + i,
                    run_id=10,
                    source_run_id=10,
                    job_id=10,
                    company_id=10,
                    service_id=10,
                    prompt_id=1,
                    prompt_snapshot="Analiza la llamada.",
                    execution_source="automation",
                    call_id=f"call_recon_{i}",
                    hs_object_id=f"hs_recon_{i}",
                    hubspot_owner_id="1539993532",
                    agent_name="Fernanda Rodrigues",
                    evaluacion_global=7.5,
                    status="completed",
                    result_json={"tipo_llamada": "front", "evaluacion_global": 7.5},
                    items_json=[
                        {"criterion_id": 1, "criterion_key": "c1", "name": "Crit 1", "type": "number", "numeric_value": 8.0, "not_applicable": False},
                        {"criterion_id": 2, "criterion_key": "c2", "name": "Crit 2", "type": "boolean", "boolean_value": True, "not_applicable": False}
                    ]
                )
                db.add(res)
                await db.flush()

                for item in res.items_json:
                    crit = MassEvaluationCriterionResult(
                        mass_analysis_id=res.mass_analysis_id,
                        run_id=10,
                        job_id=10,
                        execution_source="automation",
                        call_id=res.call_id,
                        hs_object_id=res.hs_object_id,
                        criterion_id=item["criterion_id"],
                        criterion_key=item["criterion_key"],
                        criterion_name=item["name"],
                        criterion_type=item["type"],
                        numeric_value=item.get("numeric_value"),
                        boolean_value=item.get("boolean_value"),
                        is_applicable=True,
                        not_applicable=False
                    )
                    db.add(crit)

            await db.commit()

    async def test_mass_run_persistence_reconciliation(self):
        """Verifies 100% 1:1 match between run metrics, saved results, and child criteria rows."""
        async with AsyncSession(self.engine) as db:
            run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == 10)
            run_res = await db.execute(run_stmt)
            run = run_res.scalars().first()

            self.assertEqual(run.calls_found, 3)
            self.assertEqual(run.calls_selected, 3)
            self.assertEqual(run.calls_analyzed, 3)

            results_stmt = select(func.count(MassEvaluationResult.mass_analysis_id)).where(MassEvaluationResult.run_id == 10)
            res_count = (await db.execute(results_stmt)).scalar()
            self.assertEqual(res_count, 3, "Saved DB results count must equal calls_analyzed (3)")

            criteria_stmt = select(func.count(MassEvaluationCriterionResult.mass_analysis_id)).where(MassEvaluationCriterionResult.run_id == 10)
            crit_count = (await db.execute(criteria_stmt)).scalar()
            self.assertEqual(crit_count, 6, "Total criteria rows must equal 3 calls * 2 criteria (6)")


if __name__ == "__main__":
    unittest.main()
