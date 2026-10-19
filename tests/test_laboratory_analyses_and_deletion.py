"""
Tests for Laboratory analysis listing, service scoping, and safe deletion.
"""
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select
from httpx import AsyncClient, ASGITransport

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.prompts import Prompt, PromptVersion
from app.models.analyses import Analysis, CallAnalysisCurrent, AnalysisResult, AnalysisCriterionResult
from app.utils.security import create_access_token, hash_password
from app.main import app
from app.dependencies import get_db


class TestLaboratoryAnalysesAndDeletion(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_maker() as db:
            pwd = hash_password("Password123!")

            # 1. Companies
            self.c_boston = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c_other = Company(company_id=2, company_name="Other Company", company_key="other-co", is_active=True)
            db.add_all([self.c_boston, self.c_other])
            await db.flush()

            # 2. Services
            self.s_front = Service(service_id=1, service_name="Front", service_key="front", company_id=1, is_active=True)
            self.s_expac = Service(service_id=2, service_name="Experiencia Paciente", service_key="expac", company_id=1, is_active=True)
            self.s_commercial = Service(service_id=3, service_name="Asesores Comerciales", service_key="asesorias", company_id=1, is_active=True)
            self.s_other = Service(service_id=4, service_name="Otro Servicio", service_key="otro-servicio", company_id=2, is_active=True)
            db.add_all([self.s_front, self.s_expac, self.s_commercial, self.s_other])
            await db.flush()

            # 3. Users
            self.u_super = User(user_id=1, username="superadmin", email="super@test.com", role="super_admin", password_hash=pwd, is_active=True)
            self.u_admin_boston = User(user_id=2, username="admin_boston", email="admin_boston@test.com", role="company_admin", company_id=1, password_hash=pwd, is_active=True)
            self.u_admin_other = User(user_id=3, username="admin_other", email="admin_other@test.com", role="company_admin", company_id=2, password_hash=pwd, is_active=True)
            self.u_agent_boston = User(user_id=4, username="agent_boston", email="agent_boston@test.com", role="agent", company_id=1, primary_service_id=3, password_hash=pwd, is_active=True)
            db.add_all([self.u_super, self.u_admin_boston, self.u_admin_other, self.u_agent_boston])
            await db.flush()

            # 4. Prompts
            self.p_comm = Prompt(prompt_id=76, prompt_name="EE Asesores Comerciales", prompt_type="text", service_id=3, company_id=1, is_active=True)
            self.p_other = Prompt(prompt_id=80, prompt_name="Prompt Other", prompt_type="text", service_id=4, company_id=2, is_active=True)
            db.add_all([self.p_comm, self.p_other])
            await db.commit()

        self.t_super = create_access_token({"user_id": 1, "email": "super@test.com"})
        self.t_admin_boston = create_access_token({"user_id": 2, "email": "admin_boston@test.com"})
        self.t_admin_other = create_access_token({"user_id": 3, "email": "admin_other@test.com"})
        self.t_agent_boston = create_access_token({"user_id": 4, "email": "agent_boston@test.com"})

        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        await self.client.aclose()
        await self.engine.dispose()

    async def test_laboratory_listing_service_filter(self):
        """Verify that analyses for Asesores Comerciales appear in /bm/analyses and /bm/analyses/history."""
        async with self.session_maker() as db:
            a1 = Analysis(
                analysis_id=101,
                company_id=1,
                service_id=3,
                analysis_type="text",
                call_id="upload-20261016-001",
                prompt_id=76,
                status="completed",
                evaluacion_global=Decimal("8.50"),
                created_at=datetime.now(timezone.utc),
            )
            c1 = CallAnalysisCurrent(
                call_id="upload-20261016-001",
                analysis_type="text",
                latest_analysis_id=101,
                company_id=1,
                service_id=3,
                prompt_id=76,
                status="completed",
                evaluacion_global=Decimal("8.50"),
                updated_at=datetime.now(timezone.utc),
            )
            a_other = Analysis(
                analysis_id=102,
                company_id=2,
                service_id=4,
                analysis_type="text",
                call_id="upload-20261016-002",
                prompt_id=80,
                status="completed",
                evaluacion_global=Decimal("7.00"),
                created_at=datetime.now(timezone.utc),
            )
            c_other = CallAnalysisCurrent(
                call_id="upload-20261016-002",
                analysis_type="text",
                latest_analysis_id=102,
                company_id=2,
                service_id=4,
                prompt_id=80,
                status="completed",
                evaluacion_global=Decimal("7.00"),
                updated_at=datetime.now(timezone.utc),
            )
            db.add_all([a1, c1, a_other, c_other])
            await db.commit()

        # 1. Query with superadmin for service_id=3
        res = await self.client.get(
            "/bm/analyses?service_id=3",
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 200)
        items = res.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["call_id"], "upload-20261016-001")
        self.assertEqual(items[0]["service_id"], 3)
        self.assertEqual(items[0]["service_name"], "Asesores Comerciales")

        # 2. Query history with service_id=3
        res_h = await self.client.get(
            "/bm/analyses/history?service_id=3",
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res_h.status_code, 200)
        items_h = res_h.json()
        self.assertEqual(len(items_h), 1)
        self.assertEqual(items_h[0]["analysis_id"], 101)

        # 3. Query by prompt_id=76
        res_p = await self.client.get(
            "/bm/analyses?prompt_id=76",
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res_p.status_code, 200)
        self.assertEqual(len(res_p.json()), 1)

    async def test_delete_analysis_success_and_cleanup(self):
        """Verify deleting an analysis cascades to results and cleans up CallAnalysisCurrent."""
        async with self.session_maker() as db:
            a = Analysis(
                analysis_id=201,
                company_id=1,
                service_id=3,
                analysis_type="text",
                call_id="call-test-del-1",
                prompt_id=76,
                status="completed",
                evaluacion_global=Decimal("9.00"),
                created_at=datetime.now(timezone.utc),
            )
            c = CallAnalysisCurrent(
                call_id="call-test-del-1",
                analysis_type="text",
                latest_analysis_id=201,
                company_id=1,
                service_id=3,
                prompt_id=76,
                status="completed",
                evaluacion_global=Decimal("9.00"),
                updated_at=datetime.now(timezone.utc),
            )
            r1 = AnalysisResult(
                result_id=1,
                analysis_id=201,
                criterion_key="c1",
                value_number=Decimal("8.00"),
            )
            cr1 = AnalysisCriterionResult(
                id=1,
                analysis_id=201,
                call_id="call-test-del-1",
                criterion_key="c1",
                numeric_value=Decimal("8.00"),
            )
            db.add_all([a, c, r1, cr1])
            await db.commit()

        # Execute DELETE as Boston Company Admin
        res = await self.client.delete(
            "/bm/analyses/201",
            headers={"Authorization": f"Bearer {self.t_admin_boston}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("analysis_id"), 201)

        # Verify Analysis is deleted
        async with self.session_maker() as db:
            a_db = (await db.execute(select(Analysis).where(Analysis.analysis_id == 201))).scalar_one_or_none()
            self.assertIsNone(a_db)

            # Verify CallAnalysisCurrent is deleted (no other analysis for this call_id)
            c_db = (await db.execute(select(CallAnalysisCurrent).where(CallAnalysisCurrent.call_id == "call-test-del-1"))).scalar_one_or_none()
            self.assertIsNone(c_db)

            # Verify results are deleted
            r_db = (await db.execute(select(AnalysisResult).where(AnalysisResult.analysis_id == 201))).scalars().all()
            self.assertEqual(len(r_db), 0)

            cr_db = (await db.execute(select(AnalysisCriterionResult).where(AnalysisCriterionResult.analysis_id == 201))).scalars().all()
            self.assertEqual(len(cr_db), 0)

    async def test_delete_analysis_promotes_previous_analysis(self):
        """Verify deleting the latest analysis for a call_id promotes the previous analysis in CallAnalysisCurrent."""
        async with self.session_maker() as db:
            a_old = Analysis(
                analysis_id=301,
                company_id=1,
                service_id=3,
                analysis_type="text",
                call_id="call-multi-run",
                prompt_id=76,
                status="completed",
                evaluacion_global=Decimal("7.00"),
                created_at=datetime.now(timezone.utc),
            )
            a_new = Analysis(
                analysis_id=302,
                company_id=1,
                service_id=3,
                analysis_type="text",
                call_id="call-multi-run",
                prompt_id=76,
                status="completed",
                evaluacion_global=Decimal("9.50"),
                created_at=datetime.now(timezone.utc),
            )
            c = CallAnalysisCurrent(
                call_id="call-multi-run",
                analysis_type="text",
                latest_analysis_id=302,
                company_id=1,
                service_id=3,
                prompt_id=76,
                status="completed",
                evaluacion_global=Decimal("9.50"),
                updated_at=datetime.now(timezone.utc),
            )
            db.add_all([a_old, a_new, c])
            await db.commit()

        # Delete the newest analysis (302)
        res = await self.client.delete(
            "/bm/analyses/302",
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 200)

        # Verify CallAnalysisCurrent now points to a_old (301)
        async with self.session_maker() as db:
            c_db = (await db.execute(select(CallAnalysisCurrent).where(CallAnalysisCurrent.call_id == "call-multi-run"))).scalar_one_or_none()
            self.assertIsNotNone(c_db)
            self.assertEqual(c_db.latest_analysis_id, 301)
            self.assertEqual(float(c_db.evaluacion_global), 7.00)

    async def test_delete_analysis_not_found_returns_404(self):
        """Verify deleting a nonexistent analysis returns 404."""
        res = await self.client.delete(
            "/bm/analyses/99999",
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 404)

    async def test_delete_analysis_cross_company_forbidden_403(self):
        """Verify company admin of company 2 cannot delete analysis of company 1."""
        async with self.session_maker() as db:
            a = Analysis(
                analysis_id=401,
                company_id=1,
                service_id=3,
                analysis_type="text",
                call_id="call-cross-co",
                prompt_id=76,
                status="completed",
                created_at=datetime.now(timezone.utc),
            )
            db.add(a)
            await db.commit()

        res = await self.client.delete(
            "/bm/analyses/401",
            headers={"Authorization": f"Bearer {self.t_admin_other}"}  # company_id=2
        )
        self.assertEqual(res.status_code, 403)
