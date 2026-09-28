import os
import unittest
from datetime import datetime, timezone, timedelta

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///top_criteria_unit_test.db"

from sqlalchemy import BigInteger, select, delete
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
from app.models.users import User
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.utils.security import create_access_token
from app.services.dashboard_service import get_dashboard_summary, get_dashboard_top_criteria


class TestDashboardTopCriteria(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassEvaluationCriterionResult))
            await db.execute(delete(MassEvaluationResult))
            await db.execute(delete(User).where(User.user_id.in_([9901, 9902])))
            await db.execute(delete(Team).where(Team.team_id.in_([991, 992])))
            await db.execute(delete(Service).where(Service.service_id.in_([991, 992])))
            await db.execute(delete(Company).where(Company.company_id.in_([1, 7])))
            await db.commit()

            c1 = Company(company_id=1, company_key="boston_medical", company_name="Boston Medical Group")
            c7 = Company(company_id=7, company_key="empresa_demo", company_name="Empresa Demo")
            db.add_all([c1, c7])
            await db.flush()

            s1 = Service(service_id=991, company_id=1, service_key="bm_front", service_name="BM Front")
            s7 = Service(service_id=992, company_id=7, service_key="demo_aten", service_name="Demo Atencion")
            db.add_all([s1, s7])
            await db.flush()

            t1 = Team(team_id=991, service_id=991, company_id=1, team_name="Equipo BM 1")
            t7 = Team(team_id=992, service_id=992, company_id=7, team_name="Equipo Demo 1")
            db.add_all([t1, t7])
            await db.flush()

            u_super = User(
                user_id=9901,
                username="admin_test",
                email="admin_test@test.com",
                password_hash="dummy",
                role="superadmin",
                company_id=None,
                is_active=True
            )
            db.add(u_super)
            await db.flush()

            now = datetime.now(timezone.utc)
            # Create call analyses for Empresa Demo (company_id=7)
            r1 = MassEvaluationResult(
                mass_analysis_id=901, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                company_id=7, service_id=992, call_id="demo_c1", hubspot_owner_id="demo_owner_01",
                call_timestamp=now - timedelta(hours=2), status="completed", evaluacion_global=8.0
            )
            r2 = MassEvaluationResult(
                mass_analysis_id=902, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                company_id=7, service_id=992, call_id="demo_c2", hubspot_owner_id="demo_owner_01",
                call_timestamp=now - timedelta(hours=4), status="completed", evaluacion_global=6.0
            )
            r3 = MassEvaluationResult(
                mass_analysis_id=903, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                company_id=7, service_id=992, call_id="demo_c3", hubspot_owner_id="demo_owner_02",
                call_timestamp=now - timedelta(hours=6), status="completed", evaluacion_global=7.0
            )
            # Call for Boston Medical (company_id=1)
            rbm = MassEvaluationResult(
                mass_analysis_id=904, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                company_id=1, service_id=991, call_id="bm_c1", hubspot_owner_id="bm_owner_01",
                call_timestamp=now - timedelta(hours=3), status="completed", evaluacion_global=9.0
            )
            db.add_all([r1, r2, r3, rbm])
            await db.flush()

            # Add criteria: 6 different criteria
            # 1. acogida: 3 calls (8, 6, 7) -> avg = 7.0
            # 2. agilidad: 3 calls (9, 9, 6) -> avg = 8.0
            # 3. empatia: 2 calls (8, 6) -> avg = 7.0
            # 4. claridad: 2 calls (10, 8) -> avg = 9.0
            # 5. despedida: 1 call (5) -> avg = 5.0 (should be excluded by LIMIT 4)
            # 6. comentario (text): 3 calls -> MUST be excluded because it's text
            # 7. not_applicable criterion -> MUST be excluded
            crits = [
                MassEvaluationCriterionResult(id=9101, mass_analysis_id=901, call_id="demo_c1", job_id=1, run_id=1, criterion_key="acogida", criterion_name="Acogida y Contención", criterion_type="score_1_10", numeric_value=8.0, is_applicable=True, not_applicable=False),
                MassEvaluationCriterionResult(id=9102, mass_analysis_id=902, call_id="demo_c2", job_id=1, run_id=1, criterion_key="acogida", criterion_name="Acogida y Contención", criterion_type="score_1_10", numeric_value=6.0, is_applicable=True, not_applicable=False),
                MassEvaluationCriterionResult(id=9103, mass_analysis_id=903, call_id="demo_c3", job_id=1, run_id=1, criterion_key="acogida", criterion_name="Acogida y Contención", criterion_type="score_1_10", numeric_value=7.0, is_applicable=True, not_applicable=False),

                MassEvaluationCriterionResult(id=9104, mass_analysis_id=901, call_id="demo_c1", job_id=1, run_id=1, criterion_key="agilidad", criterion_name="Agilidad en Gestión", criterion_type="score_1_10", numeric_value=9.0, is_applicable=True, not_applicable=False),
                MassEvaluationCriterionResult(id=9105, mass_analysis_id=902, call_id="demo_c2", job_id=1, run_id=1, criterion_key="agilidad", criterion_name="Agilidad en Gestión", criterion_type="score_1_10", numeric_value=9.0, is_applicable=True, not_applicable=False),
                MassEvaluationCriterionResult(id=9106, mass_analysis_id=903, call_id="demo_c3", job_id=1, run_id=1, criterion_key="agilidad", criterion_name="Agilidad en Gestión", criterion_type="score_1_10", numeric_value=6.0, is_applicable=True, not_applicable=False),

                MassEvaluationCriterionResult(id=9107, mass_analysis_id=901, call_id="demo_c1", job_id=1, run_id=1, criterion_key="empatia", criterion_name="Empatía", criterion_type="score_1_10", numeric_value=8.0, is_applicable=True, not_applicable=False),
                MassEvaluationCriterionResult(id=9108, mass_analysis_id=902, call_id="demo_c2", job_id=1, run_id=1, criterion_key="empatia", criterion_name="Empatía", criterion_type="score_1_10", numeric_value=6.0, is_applicable=True, not_applicable=False),

                MassEvaluationCriterionResult(id=9109, mass_analysis_id=901, call_id="demo_c1", job_id=1, run_id=1, criterion_key="claridad", criterion_name="Claridad", criterion_type="score_1_10", numeric_value=10.0, is_applicable=True, not_applicable=False),
                MassEvaluationCriterionResult(id=9110, mass_analysis_id=903, call_id="demo_c3", job_id=1, run_id=1, criterion_key="claridad", criterion_name="Claridad", criterion_type="score_1_10", numeric_value=8.0, is_applicable=True, not_applicable=False),

                MassEvaluationCriterionResult(id=9111, mass_analysis_id=901, call_id="demo_c1", job_id=1, run_id=1, criterion_key="despedida", criterion_name="Despedida", criterion_type="score_1_10", numeric_value=5.0, is_applicable=True, not_applicable=False),

                # Text criterion -> Must be excluded
                MassEvaluationCriterionResult(id=9112, mass_analysis_id=901, call_id="demo_c1", job_id=1, run_id=1, criterion_key="comentario_libre", criterion_name="Comentario Libre", criterion_type="text", text_value="Texto libre", is_applicable=True, not_applicable=False),

                # Not applicable -> Must be excluded
                MassEvaluationCriterionResult(id=9113, mass_analysis_id=902, call_id="demo_c2", job_id=1, run_id=1, criterion_key="objeciones", criterion_name="Objeciones", criterion_type="score_1_10", numeric_value=4.0, is_applicable=False, not_applicable=True),
            ]
            db.add_all(crits)
            await db.commit()

        self.token = create_access_token({"user_id": 9901, "username": "admin", "role": "super_admin", "is_super_admin": True})
        self.headers = {"Authorization": f"Bearer {self.token}", "X-Company-ID": "7"}

    async def test_top_criteria_max_4_and_order_by_coverage(self):
        """1. Máximo 4 criterios & 3. Orden por cobertura (total_applicable DESC)."""
        async with AsyncSession(self.engine) as db:
            res = await get_dashboard_summary(db, company_id=7, period="24h")
            top = res.get("top_criteria", [])
            self.assertLessEqual(len(top), 4)
            self.assertEqual(len(top), 4)  # Out of 5 numeric criteria, exactly 4 returned

            # Check coverage ordering
            coverages = [c["total_applicable"] for c in top]
            self.assertEqual(coverages, sorted(coverages, reverse=True))

            # The top 2 by coverage should have 3 calls each (acogida, agilidad)
            self.assertEqual(top[0]["total_applicable"], 3)
            self.assertEqual(top[1]["total_applicable"], 3)
            # The next 2 by coverage should have 2 calls each (claridad, empatia)
            self.assertEqual(top[2]["total_applicable"], 2)
            self.assertEqual(top[3]["total_applicable"], 2)

    async def test_top_criteria_only_numeric(self):
        """2. Solo numéricos: texto y categorías quedan excluidos."""
        async with AsyncSession(self.engine) as db:
            res = await get_dashboard_summary(db, company_id=7, period="24h")
            top = res.get("top_criteria", [])
            keys = [c["criterion_key"] for c in top]
            self.assertNotIn("comentario_libre", keys)
            for c in top:
                self.assertIn(c["criterion_type"], ["score_1_10", "percentage", "score", "numeric"])
                self.assertIsInstance(c["avg_value"], (int, float))

    async def test_respects_company_and_service(self):
        """4. Respeta company y service filter."""
        async with AsyncSession(self.engine) as db:
            # Query for non-existent service -> should return empty
            res = await get_dashboard_summary(db, company_id=7, service_id=99999, period="24h")
            self.assertEqual(res.get("top_criteria", []), [])

            # Query for service 992 -> returns the 4 criteria
            res = await get_dashboard_summary(db, company_id=7, service_id=992, period="24h")
            self.assertEqual(len(res.get("top_criteria", [])), 4)

    async def test_respects_date_range(self):
        """5. Respeta date range (filtro temporal)."""
        async with AsyncSession(self.engine) as db:
            # Range in future -> empty
            res = await get_dashboard_summary(db, company_id=7, date_from="2028-01-01", date_to="2028-01-02")
            self.assertEqual(res.get("top_criteria", []), [])

    async def test_boston_medical_unchanged(self):
        """7. Boston Medical (company_id=1) no cambia: top_criteria no altera sus métricas."""
        async with AsyncSession(self.engine) as db:
            res = await get_dashboard_summary(db, company_id=1, period="24h")
            self.assertEqual(res.get("top_criteria", []), [])
            # Existing summary structure intact
            self.assertIn("summary", res)
            self.assertIn("kpis", res)
            self.assertIn("comparisons", res)
            self.assertIn("agent_ranking", res)

    async def test_http_endpoint_top_criteria(self):
        """Dedicated endpoint GET /bm/dashboard/top-criteria returns correct format."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/bm/dashboard/top-criteria?company_id=7&period=24h", headers=self.headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIsInstance(data, list)
            self.assertLessEqual(len(data), 4)
            if data:
                item = data[0]
                self.assertIn("criterion_key", item)
                self.assertIn("criterion_name", item)
                self.assertIn("criterion_type", item)
                self.assertIn("avg_value", item)
                self.assertIn("total_applicable", item)

    async def test_http_summary_includes_top_criteria(self):
        """GET /bm/dashboard/summary for company_id=7 includes top_criteria block."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/bm/dashboard/summary?company_id=7&period=24h", headers=self.headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("top_criteria", data)
            self.assertEqual(len(data["top_criteria"]), 4)

    async def test_respects_item_filters(self):
        """6. Respeta filtros de ítems (ej: status=failed devuelve 0 porque todas son completed)."""
        async with AsyncSession(self.engine) as db:
            res = await get_dashboard_summary(db, company_id=7, period="24h", status="failed")
            self.assertEqual(res.get("top_criteria", []), [])

    async def test_no_n_plus_one_queries(self):
        """8. No hay N+1: la agregación de criterios ejecuta una única consulta SQL agrupada."""
        from unittest.mock import patch
        async with AsyncSession(self.engine) as db:
            query_count = 0
            original_execute = db.execute

            async def counting_execute(*args, **kwargs):
                nonlocal query_count
                query_count += 1
                return await original_execute(*args, **kwargs)

            with patch.object(db, "execute", side_effect=counting_execute):
                from app.services.dashboard_service import compute_top_criteria_from_analyses
                # Compute for 3 analysis IDs
                top = await compute_top_criteria_from_analyses(db, [901, 902, 903], max_items=4)
                self.assertEqual(len(top), 4)
                # Exactly 1 query executed by compute_top_criteria_from_analyses!
                self.assertEqual(query_count, 1)
