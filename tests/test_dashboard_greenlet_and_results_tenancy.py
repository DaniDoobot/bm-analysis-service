import os
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_dashboard_tenancy.db"

from sqlalchemy import BigInteger, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import defer

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.db import get_engine, Base
from app.main import app
from app.models.users import User
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.mass_evaluations import MassEvaluationResult
from app.utils.security import create_access_token
from app.services.dashboard_service import (
    _get_loaded_attr,
    _get_duration_sec_mass,
    extract_score_from_mass_row,
    get_dashboard_summary,
)


class TestDashboardGreenletAndTenancy(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.call_id.like("test_gt_%")))
            await db.execute(delete(User).where(User.user_id.in_([8801, 8802, 8803])))
            await db.execute(delete(Team).where(Team.team_id.in_([881, 887])))
            await db.execute(delete(Service).where(Service.service_id.in_([881, 887])))
            await db.execute(delete(Company).where(Company.company_id.in_([1, 7])))
            await db.commit()

            c1 = Company(company_id=1, company_key="boston_medical", company_name="Boston Medical Group")
            c7 = Company(company_id=7, company_key="empresa_demo", company_name="Empresa Demo")
            db.add_all([c1, c7])
            await db.flush()

            s1 = Service(service_id=881, company_id=1, service_key="bm_front", service_name="BM Front")
            s7 = Service(service_id=887, company_id=7, service_key="demo_front", service_name="Demo Front")
            db.add_all([s1, s7])
            await db.flush()

            u_super = User(
                user_id=8801,
                username="superadmin_test",
                email="superadmin@test.com",
                password_hash="dummy",
                role="superadmin",
                company_id=None,
                is_active=True
            )
            u_demo = User(
                user_id=8802,
                username="demo_admin_test",
                email="demoadmin@test.com",
                password_hash="dummy",
                role="company_admin",
                company_id=7,
                is_active=True
            )
            u_bm = User(
                user_id=8803,
                username="bm_admin_test",
                email="bmadmin@test.com",
                password_hash="dummy",
                role="company_admin",
                company_id=1,
                is_active=True
            )
            db.add_all([u_super, u_demo, u_bm])
            await db.flush()

            now = datetime.now(timezone.utc)
            r_demo = MassEvaluationResult(
                call_id="test_gt_demo_01",
                run_id=88,
                job_id=88,
                prompt_id=88,
                prompt_snapshot="{}",
                company_id=7,
                service_id=887,
                service_key="demo_front",
                hubspot_owner_id="demo_agent_01",
                agent_name="Agente Demo 01",
                direction="inbound",
                call_timestamp=now,
                analysis_timestamp=now,
                call_duration_seconds=120,
                evaluacion_global=None,
                result_json={"tipo_llamada": "cita", "global_score": 8.5, "objeciones": ["precio"]},
                items_json=[{"key": "claridad", "value": 9.0}, {"key": "evaluacion_global", "value": 8.5}],
                status="completed",
                execution_source="on_demand"
            )
            r_bm1 = MassEvaluationResult(
                call_id="test_gt_bm_01",
                run_id=88,
                job_id=88,
                prompt_id=88,
                prompt_snapshot="{}",
                company_id=1,
                service_id=881,
                service_key="bm_front",
                hubspot_owner_id="bm_agent_01",
                agent_name="Agente BM 01",
                direction="outbound",
                call_timestamp=now,
                analysis_timestamp=now,
                call_duration_seconds=90,
                evaluacion_global=7.0,
                result_json={"tipo_llamada": "informativa"},
                items_json=[],
                status="completed",
                execution_source="on_demand"
            )
            r_bm_legacy = MassEvaluationResult(
                call_id="test_gt_bm_legacy_02",
                run_id=88,
                job_id=88,
                prompt_id=88,
                prompt_snapshot="{}",
                company_id=None,
                service_id=881,
                service_key="bm_front",
                hubspot_owner_id="bm_agent_02",
                agent_name="Agente BM 02",
                direction="inbound",
                call_timestamp=now,
                analysis_timestamp=now,
                call_duration_seconds=150,
                evaluacion_global=6.5,
                result_json={"tipo_llamada": "cita"},
                items_json=[],
                status="completed",
                execution_source="on_demand"
            )
            db.add_all([r_demo, r_bm1, r_bm_legacy])
            await db.commit()

        self.token_super = create_access_token({"user_id": 8801, "email": "superadmin@test.com"})
        self.token_demo = create_access_token({"user_id": 8802, "email": "demoadmin@test.com"})
        self.token_bm = create_access_token({"user_id": 8803, "email": "bmadmin@test.com"})

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.call_id.like("test_gt_%")))
            await db.execute(delete(User).where(User.user_id.in_([8801, 8802, 8803])))
            await db.execute(delete(Team).where(Team.team_id.in_([881, 887])))
            await db.execute(delete(Service).where(Service.service_id.in_([881, 887])))
            await db.execute(delete(Company).where(Company.company_id.in_([1, 7])))
            await db.commit()

    async def test_dashboard_orm_deferred_items_json_no_missing_greenlet(self):
        async with AsyncSession(self.engine) as db:
            stmt = select(MassEvaluationResult).where(
                MassEvaluationResult.call_id == "test_gt_demo_01"
            ).options(
                defer(MassEvaluationResult.prompt_snapshot),
                defer(MassEvaluationResult.items_json),
            )
            res = await db.execute(stmt)
            orm_row = res.scalar_one()

            self.assertNotIn("items_json", orm_row.__dict__)

            loaded_items = _get_loaded_attr(orm_row, "items_json")
            self.assertIsNone(loaded_items)

            score = extract_score_from_mass_row(orm_row, "evaluacion_global")
            self.assertEqual(score, 8.5)

            summary = await get_dashboard_summary(
                db,
                company_id=7,
                period="24h"
            )
            self.assertIsNotNone(summary)
            self.assertIn("kpis", summary)
            self.assertEqual(summary["kpis"]["total_analyses"], 1.0)
            self.assertEqual(summary["kpis"]["avg_evaluacion_global"], 8.5)

    async def test_dashboard_endpoint_http_returns_200_without_greenlet_error(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/dashboard/summary?company_id=7&period=24h&granularity=auto&status=completed",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("kpis", data)
            self.assertEqual(data["kpis"]["total_analyses"], 1.0)
            self.assertEqual(data["kpis"]["avg_evaluacion_global"], 8.5)

    def test_duration_sec_mass_safe_for_row_without_hubspot_metadata(self):
        class MockRow:
            def __init__(self, call_dur):
                self.call_duration_seconds = call_dur

        row_with_dur = MockRow(120)
        self.assertEqual(_get_duration_sec_mass(row_with_dur), 120.0)

        row_no_dur = MockRow(None)
        self.assertIsNone(_get_duration_sec_mass(row_no_dur))

    async def test_results_company_id_7_only_demo_records(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?company_id=7",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            test_items = [it for it in items if it["call_id"].startswith("test_gt_")]
            self.assertTrue(len(test_items) >= 1)
            for it in test_items:
                self.assertEqual(it["call_id"], "test_gt_demo_01")
                self.assertEqual(it["company_id"], 7)

    async def test_results_company_id_1_only_bm_and_legacy_records(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?company_id=1",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            call_ids = [it["call_id"] for it in items if it["call_id"].startswith("test_gt_")]
            self.assertIn("test_gt_bm_01", call_ids)
            self.assertIn("test_gt_bm_legacy_02", call_ids)
            self.assertNotIn("test_gt_demo_01", call_ids)

    async def test_results_superadmin_without_company_sees_all(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            call_ids = [it["call_id"] for it in items if it["call_id"].startswith("test_gt_")]
            self.assertIn("test_gt_demo_01", call_ids)
            self.assertIn("test_gt_bm_01", call_ids)
            self.assertIn("test_gt_bm_legacy_02", call_ids)

    async def test_results_company_key_resolution(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?company=empresa_demo",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            call_ids = [it["call_id"] for it in items if it["call_id"].startswith("test_gt_")]
            self.assertEqual(call_ids, ["test_gt_demo_01"])

    async def test_results_restricted_user_cannot_access_other_company(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?company_id=1",
                headers={"Authorization": f"Bearer {self.token_demo}"}
            )
            self.assertEqual(res.status_code, 403)
            self.assertIn("Acceso denegado a otra empresa", res.json()["detail"])

    async def test_results_incompatible_company_and_service_rejected(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?company_id=7&service_id=881",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 400)
            self.assertIn("El servicio seleccionado no pertenece a la empresa indicada", res.json()["detail"])
