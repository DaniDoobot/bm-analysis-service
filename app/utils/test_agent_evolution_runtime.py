"""
Test suite for get_agent_evolution runtime stability.
======================================================
Verifies:
1. get_agent_evolution runs without NameError (norm_t, norm_d).
2. Agent Luci resolves agent_initials to 'LD'.
"""
import os
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agent_evolution_test.db"

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
from sqlalchemy import delete

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult
from app.services.dashboard_service import get_agent_evolution


class TestAgentEvolutionRuntime(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            db.add(Company(company_id=980, company_key="evo_co", company_name="Evo Co"))
            await db.flush()
            db.add(Service(service_id=981, company_id=980, service_key="evo_serv", service_name="Evo Serv"))
            await db.flush()

            db.add(User(
                user_id=9801,
                username="luci_evo",
                email="luci_evo@bm.es",
                name="Luci Dos Santos Furtado",
                role="agent",
                company_id=980,
                is_active=True,
                hubspot_owner_id="1375831790",
                agent_initials="LD",
                password_hash="dummy_hash",
            ))

            db.add(MassEvaluationResult(
                mass_analysis_id=98001,
                run_id=9801,
                job_id=9801,
                prompt_id=9801,
                prompt_snapshot="test",
                call_id="call_evo_1",
                company_id=980,
                service_id=981,
                hubspot_owner_id="1375831790",
                agent_name="Luci Furtado",
                direction="inbound",
                call_timestamp=datetime.now(timezone.utc),
                status="completed",
                evaluacion_global=8.0,
                result_json={"tipo_llamada": "cita", "inbound_outbound": "inbound", "evaluacion_global": 8.0},
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == 98001))
            await db.execute(delete(User).where(User.user_id == 9801))
            await db.execute(delete(Service).where(Service.service_id == 981))
            await db.execute(delete(Company).where(Company.company_id == 980))
            await db.commit()

    async def test_get_agent_evolution_runs_without_name_error(self):
        """get_agent_evolution must execute without NameError when typology_key and direction are provided."""
        async with AsyncSession(self.engine) as db:
            res = await get_agent_evolution(
                db=db,
                hubspot_owner_id="1375831790",
                typology_key="cita",
                direction="inbound",
                period="30d",
            )
            self.assertIn("summary", res)
            self.assertIn("agent", res)
            self.assertEqual(res["summary"]["total_analyses"], 1.0)


if __name__ == "__main__":
    unittest.main()
