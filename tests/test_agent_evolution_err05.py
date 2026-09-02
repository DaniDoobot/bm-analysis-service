"""
Unit test suite for Agent Evolution ERR-05 fixes:
- Evaluacion global precedence in latest_analyses (column > fallback, preserving 0)
- Direction precedence (column > result_json, safe fallback)
- Typology precedence (column > result_json, safe fallback)
- Item score and boolean filters consistency across summary and latest_analyses
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

TEST_DB_NAME = "err05_test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_NAME}"
os.environ["APP_ENV"] = "test"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base, get_engine
from app.core.tenant_context import TenantContext, InternalRole
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.services.dashboard_service import get_agent_evolution


class TestAgentEvolutionERR05(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_NAME):
            try:
                os.remove(TEST_DB_NAME)
            except Exception:
                pass

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_NAME):
            try:
                os.remove(TEST_DB_NAME)
            except Exception:
                pass

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.context = TenantContext(
            user_id=1,
            email="admin@test.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
        )

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            c = Company(company_id=1, company_name="Test Co", company_key="test_co", is_active=True)
            s = Service(service_id=1, service_name="Front", service_key="front", company_id=1)
            u = User(user_id=10, username="bryan", email="bryan@test.com", name="Bryan Herrera", role="agent", company_id=1, hubspot_owner_id="33013277", is_active=True, password_hash="dummy")
            job = MassEvaluationJob(job_id=1, job_name="Test Job", prompt_id=1, is_active=True, schedule_enabled=False, created_by="Tester")
            run = MassEvaluationRun(run_id=1, job_id=1, trigger_type="manual", status="completed")
            db.add_all([c, s, u, job, run])
            await db.commit()

    async def test_a_score_modern(self):
        """Case A: Modern evaluation with evaluacion_global column set, result_json empty."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            call_time = datetime.now(timezone.utc) - timedelta(days=1)
            r = MassEvaluationResult(
                mass_analysis_id=1001,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_1001",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="33013277",
                agent_name="Bryan Herrera",
                call_timestamp=call_time,
                analysis_timestamp=call_time,
                status="completed",
                evaluacion_global=Decimal("7.75"),
                result_json={},
                items_json=[]
            )
            db.add(r)
            await db.commit()

            evo = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", context=self.context)
            latest = evo["latest_analyses"]
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest[0]["evaluacion_global"], 7.75)

    async def test_b_score_zero(self):
        """Case B: evaluacion_global is 0.0, must return 0.0 and not use fallback."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            call_time = datetime.now(timezone.utc) - timedelta(days=1)
            r = MassEvaluationResult(
                mass_analysis_id=1002,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_1002",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="33013277",
                agent_name="Bryan Herrera",
                call_timestamp=call_time,
                analysis_timestamp=call_time,
                status="completed",
                evaluacion_global=Decimal("0.0"),
                result_json={"evaluacion_global": 8.5},
                items_json=[]
            )
            db.add(r)
            await db.commit()

            evo = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", context=self.context)
            latest = evo["latest_analyses"]
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest[0]["evaluacion_global"], 0.0)

    async def test_c_score_historical_fallback(self):
        """Case C: evaluacion_global column is None, uses legacy fallback from result_json/items_json."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            call_time = datetime.now(timezone.utc) - timedelta(days=1)
            r = MassEvaluationResult(
                mass_analysis_id=1003,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_1003",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="33013277",
                agent_name="Bryan Herrera",
                call_timestamp=call_time,
                analysis_timestamp=call_time,
                status="completed",
                evaluacion_global=None,
                result_json={"evaluacion_global": 6.5},
                items_json=[]
            )
            db.add(r)
            await db.commit()

            evo = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", context=self.context)
            latest = evo["latest_analyses"]
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest[0]["evaluacion_global"], 6.5)

    async def test_d_direction_conflict(self):
        """Case D: direction is OUTBOUND, but result_json says inbound -> direction filter follows column."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            call_time = datetime.now(timezone.utc) - timedelta(days=1)
            r = MassEvaluationResult(
                mass_analysis_id=1004,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_1004",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="33013277",
                agent_name="Bryan Herrera",
                call_timestamp=call_time,
                analysis_timestamp=call_time,
                status="completed",
                direction="OUTBOUND",
                result_json={"inbound_outbound": "inbound"},
                items_json=[]
            )
            db.add(r)
            await db.commit()

            # Filter direction=inbound -> should be excluded
            evo_in = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", direction="inbound", context=self.context)
            self.assertEqual(evo_in["summary"]["total_analyses"], 0)
            self.assertEqual(len(evo_in["latest_analyses"]), 0)

            # Filter direction=outbound -> should be included
            evo_out = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", direction="outbound", context=self.context)
            self.assertEqual(evo_out["summary"]["total_analyses"], 1)
            self.assertEqual(len(evo_out["latest_analyses"]), 1)
            self.assertEqual(evo_out["latest_analyses"][0]["mass_analysis_id"], 1004)

    async def test_e_direction_fallback(self):
        """Case E: direction column is None -> falls back to result_json['inbound_outbound']."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            call_time = datetime.now(timezone.utc) - timedelta(days=1)
            r = MassEvaluationResult(
                mass_analysis_id=1005,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_1005",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="33013277",
                agent_name="Bryan Herrera",
                call_timestamp=call_time,
                analysis_timestamp=call_time,
                status="completed",
                direction=None,
                result_json={"inbound_outbound": "inbound"},
                items_json=[]
            )
            db.add(r)
            await db.commit()

            # Filter direction=inbound -> should be included via fallback
            evo_in = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", direction="inbound", context=self.context)
            self.assertEqual(evo_in["summary"]["total_analyses"], 1)
            self.assertEqual(len(evo_in["latest_analyses"]), 1)

    async def test_f_typology_conflict(self):
        """Case F: typology_key is 'confirmacion', but result_json says 'cita' -> follows column."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            call_time = datetime.now(timezone.utc) - timedelta(days=1)
            r = MassEvaluationResult(
                mass_analysis_id=1006,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_1006",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="33013277",
                agent_name="Bryan Herrera",
                call_timestamp=call_time,
                analysis_timestamp=call_time,
                status="completed",
                typology_key="confirmacion",
                typology_name="Confirmación",
                result_json={"tipo_llamada": "cita"},
                items_json=[]
            )
            db.add(r)
            await db.commit()

            # Filter typology_key='cita' -> excluded
            evo_cita = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", typology_key="cita", context=self.context)
            self.assertEqual(evo_cita["summary"]["total_analyses"], 0)
            self.assertEqual(len(evo_cita["latest_analyses"]), 0)

            # Filter typology_key='confirmacion' -> included and serializes typology_name
            evo_conf = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", typology_key="confirmacion", context=self.context)
            self.assertEqual(evo_conf["summary"]["total_analyses"], 1)
            self.assertEqual(len(evo_conf["latest_analyses"]), 1)
            self.assertEqual(evo_conf["latest_analyses"][0]["tipo_llamada"], "Confirmación")

    async def test_g_typology_fallback(self):
        """Case G: typology_key is None -> falls back to result_json['tipo_llamada']."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            call_time = datetime.now(timezone.utc) - timedelta(days=1)
            r = MassEvaluationResult(
                mass_analysis_id=1007,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_1007",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="33013277",
                agent_name="Bryan Herrera",
                call_timestamp=call_time,
                analysis_timestamp=call_time,
                status="completed",
                typology_key=None,
                typology_name=None,
                result_json={"tipo_llamada": "cita"},
                items_json=[]
            )
            db.add(r)
            await db.commit()

            evo = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", typology_key="cita", context=self.context)
            self.assertEqual(evo["summary"]["total_analyses"], 1)
            self.assertEqual(len(evo["latest_analyses"]), 1)
            self.assertEqual(evo["latest_analyses"][0]["tipo_llamada"], "cita")

    async def test_h_item_filters_consistency(self):
        """Case H: item_filters numeric & boolean affect both summary.total_analyses and latest_analyses."""
        import json
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            call_time = datetime.now(timezone.utc) - timedelta(days=1)
            # R1: Empatia = 8.0, Alarma = True
            r1 = MassEvaluationResult(
                mass_analysis_id=1008,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_1008",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="33013277",
                agent_name="Bryan Herrera",
                call_timestamp=call_time,
                analysis_timestamp=call_time,
                status="completed",
                evaluacion_global=Decimal("8.0"),
                result_json={"evaluacion_global": 8.0, "empatia": 8.0, "alarma": True},
                items_json=[{"key": "empatia", "value": 8.0}, {"key": "alarma", "value": True}]
            )
            # R2: Empatia = 4.0, Alarma = False
            r2 = MassEvaluationResult(
                mass_analysis_id=1009,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_1009",
                company_id=1,
                service_id=1,
                service_key="front",
                hubspot_owner_id="33013277",
                agent_name="Bryan Herrera",
                call_timestamp=call_time - timedelta(hours=1),
                analysis_timestamp=call_time - timedelta(hours=1),
                status="completed",
                evaluacion_global=Decimal("4.0"),
                result_json={"evaluacion_global": 4.0, "empatia": 4.0, "alarma": False},
                items_json=[{"key": "empatia", "value": 4.0}, {"key": "alarma", "value": False}]
            )
            db.add_all([r1, r2])
            await db.commit()

            # Numeric filter Empatia [7, 10]
            filt_emp = json.dumps([{"key": "empatia", "min": 7.0, "max": 10.0}])
            evo_emp = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", item_filters=filt_emp, context=self.context)
            self.assertEqual(evo_emp["summary"]["total_analyses"], 1)
            self.assertEqual(len(evo_emp["latest_analyses"]), 1)
            self.assertEqual(evo_emp["latest_analyses"][0]["mass_analysis_id"], 1008)

            # Boolean filter Alarma = True
            filt_al = json.dumps([{"key": "alarma", "value": True}])
            evo_al = await get_agent_evolution(db, hubspot_owner_id="33013277", service_id=1, period="30d", item_filters=filt_al, context=self.context)
            self.assertEqual(evo_al["summary"]["total_analyses"], 1)
            self.assertEqual(len(evo_al["latest_analyses"]), 1)
            self.assertEqual(evo_al["latest_analyses"][0]["mass_analysis_id"], 1008)


if __name__ == "__main__":
    unittest.main()
