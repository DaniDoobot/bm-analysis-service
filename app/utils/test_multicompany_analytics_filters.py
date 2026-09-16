"""
Unit tests for Multi-Company Filters Support and Isolation in Analytics.

Verifies:
1. Company schema response fields (`id` and `name` computed fields).
2. Hierarchy filtering: Company -> Services -> Teams -> Agents.
3. Strict data isolation between companies (Company 1 vs Company 6).
4. Legacy compatibility (NULL company_id included in company 1, excluded from company 6).
5. Authorization checks (403 Forbidden for unauthorized company_id).
"""
import unittest
from datetime import datetime, timezone

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.schemas.multitenancy import CompanyResponse
from app.utils.team_resolvers import (
    validate_team_service_cascade,
    get_team_assigned_owner_ids,
    get_service_assigned_users,
)
from app.utils.service_resolvers import resolve_service_id
from app.services.dashboard_service import get_dashboard_summary, get_agents_list
from app.services.service_evolution_service import ServiceEvolutionService
from app.services.mass_evaluation_service import MassEvaluationService
from app.routers.analytics import get_available_agents, get_filter_options
from app.routers.services import list_services


class TestMultiCompanyAnalyticsFilters(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        async with self.async_session() as db:
            # Seed 2 companies: Boston Medical (id=1) and Empresa Demo (id=6)
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True, is_demo=False)
            c6 = Company(company_id=6, company_name="Empresa Demo", company_key="empresa-demo", is_active=True, is_demo=True)
            db.add_all([c1, c6])

            # Seed services
            s1 = Service(service_id=1, company_id=1, service_name="Atención Cliente BM", service_key="atencion-cliente", is_active=True)
            s2 = Service(service_id=2, company_id=1, service_name="Front BM", service_key="front", is_active=True)
            s6 = Service(service_id=6, company_id=6, service_name="Atención Cliente Demo", service_key="atencion-cliente", is_active=True)
            db.add_all([s1, s2, s6])

            # Seed teams
            t1 = Team(team_id=1, company_id=1, service_id=1, team_name="Equipo BM 1", is_active=True)
            t6 = Team(team_id=6, company_id=6, service_id=6, team_name="Equipo Demo 6", is_active=True)
            db.add_all([t1, t6])

            # Seed users / agents
            u1 = User(user_id=1, company_id=1, primary_service_id=1, primary_team_id=1, hubspot_owner_id="agent_bm_1", name="Agente BM 1", username="agent1", email="agent1@bm.es", password_hash="fakehash", role="agent")
            u2 = User(user_id=2, company_id=1, primary_service_id=2, hubspot_owner_id="agent_bm_2", name="Agente BM 2", username="agent2", email="agent2@bm.es", password_hash="fakehash", role="agent")
            u6 = User(user_id=6, company_id=6, primary_service_id=6, primary_team_id=6, hubspot_owner_id="agent_demo_6", name="Agente Demo 6", username="agent6", email="agent6@demo.es", password_hash="fakehash", role="agent")
            db.add_all([u1, u2, u6])

            # Seed Jobs & Runs to satisfy NOT NULL constraints
            j1 = MassEvaluationJob(job_id=1, company_id=1, service_id=1, job_name="Job 1", prompt_id=1)
            r_run1 = MassEvaluationRun(run_id=1, job_id=1, company_id=1, status="completed", trigger_type="manual")
            j6 = MassEvaluationJob(job_id=6, company_id=6, service_id=6, job_name="Job 6", prompt_id=1)
            r_run6 = MassEvaluationRun(run_id=6, job_id=6, company_id=6, status="completed", trigger_type="manual")
            db.add_all([j1, r_run1, j6, r_run6])

            # Seed MassEvaluationResult records:
            # 1. Company 1 explicit
            r1 = MassEvaluationResult(
                mass_analysis_id=101,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                company_id=1,
                service_id=1,
                hubspot_owner_id="agent_bm_1",
                agent_name="Agente BM 1",
                call_id="call_bm_1",
                evaluacion_global=8.5,
                result_json={"evaluacion_global": 8.5},
                status="completed",
                call_timestamp=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
            )
            # 2. Company 1 legacy (company_id is NULL)
            r_legacy = MassEvaluationResult(
                mass_analysis_id=102,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                company_id=None,
                service_id=1,
                hubspot_owner_id="agent_bm_1",
                agent_name="Agente BM 1",
                call_id="call_bm_legacy",
                evaluacion_global=7.0,
                result_json={"evaluacion_global": 7.0},
                status="completed",
                call_timestamp=datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
            )
            # 3. Company 6 (Empresa Demo)
            r6 = MassEvaluationResult(
                mass_analysis_id=106,
                run_id=6,
                job_id=6,
                prompt_id=1,
                prompt_snapshot="{}",
                company_id=6,
                service_id=6,
                hubspot_owner_id="agent_demo_6",
                agent_name="Agente Demo 6",
                call_id="call_demo_6",
                evaluacion_global=9.0,
                result_json={"evaluacion_global": 9.0},
                status="completed",
                call_timestamp=datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc)
            )
            db.add_all([r1, r_legacy, r6])
            await db.commit()

        # Contexts:
        # Superadmin
        self.superadmin_ctx = TenantContext(
            user_id=999,
            user_email="superadmin@example.com",
            raw_role="superadmin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            company_id=1,
            allowed_company_ids=[1, 6],
            allowed_service_ids=None,
            allowed_agent_ids=None,
            allowed_team_ids=None,
        )
        # Company 1 Admin
        self.c1_admin_ctx = TenantContext(
            user_id=10,
            user_email="admin_c1@example.com",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=None,
            allowed_agent_ids=None,
            allowed_team_ids=None,
        )
        # Company 6 Admin
        self.c6_admin_ctx = TenantContext(
            user_id=60,
            user_email="admin_c6@example.com",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=6,
            allowed_company_ids=[6],
            allowed_service_ids=None,
            allowed_agent_ids=None,
            allowed_team_ids=None,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    def test_company_schema_computed_fields(self):
        """CompanyResponse should provide id and name alongside company_id and company_name."""
        resp = CompanyResponse(
            company_id=6,
            company_name="Empresa Demo",
            company_key="empresa-demo",
            is_active=True,
            is_demo=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        data = resp.model_dump()
        self.assertEqual(data["id"], 6)
        self.assertEqual(data["name"], "Empresa Demo")
        self.assertEqual(data["company_id"], 6)
        self.assertEqual(data["company_name"], "Empresa Demo")

    async def test_resolve_service_id_scoped_to_company(self):
        """Resolving service 'atencion-cliente' should return service 1 for company 1, service 6 for company 6."""
        async with self.async_session() as db:
            s1_id, _ = await resolve_service_id(db, service_key="atencion-cliente", company_ids=[1])
            self.assertEqual(s1_id, 1)

            s6_id, _ = await resolve_service_id(db, service_key="atencion-cliente", company_ids=[6])
            self.assertEqual(s6_id, 6)

    async def test_agent_catalog_isolation_by_company(self):
        """Available agents catalog must only return agents belonging to the requested company."""
        async with self.async_session() as db:
            # Superadmin querying company 6
            agents_c6 = await get_available_agents(db, context=self.superadmin_ctx, company_id=6)
            agent_ids_c6 = [a.hubspot_owner_id for a in agents_c6]
            self.assertEqual(agent_ids_c6, ["agent_demo_6"])

            # Superadmin querying company 1
            agents_c1 = await get_available_agents(db, context=self.superadmin_ctx, company_id=1)
            agent_ids_c1 = [a.hubspot_owner_id for a in agents_c1]
            self.assertIn("agent_bm_1", agent_ids_c1)
            self.assertIn("agent_bm_2", agent_ids_c1)
            self.assertNotIn("agent_demo_6", agent_ids_c1)

    async def test_team_assigned_users_company_scoping(self):
        """Team resolvers must filter by company_id when provided."""
        async with self.async_session() as db:
            owners_c1 = await get_team_assigned_owner_ids(db, team_id=1, context=self.superadmin_ctx, company_id=1)
            self.assertEqual(owners_c1, {"agent_bm_1"})

            owners_c6 = await get_team_assigned_owner_ids(db, team_id=6, context=self.superadmin_ctx, company_id=6)
            self.assertEqual(owners_c6, {"agent_demo_6"})

    async def test_dashboard_summary_isolation(self):
        """Dashboard summary must isolate evaluations by company_id."""
        async with self.async_session() as db:
            # Query company 6: should only see call_demo_6 (1 call, avg 9.0)
            sum_c6 = await get_dashboard_summary(
                db,
                context=self.superadmin_ctx,
                company_id=6,
                date_from="2026-01-01",
                date_to="2026-12-31"
            )
            self.assertEqual(sum_c6["kpis"]["total_analyses"], 1)
            self.assertAlmostEqual(sum_c6["kpis"]["avg_evaluacion_global"], 9.0, places=1)

            # Query company 1: should see call_bm_1 and call_bm_legacy (2 calls, avg (8.5+7.0)/2 = 7.75 -> 7.8)
            sum_c1 = await get_dashboard_summary(
                db,
                context=self.superadmin_ctx,
                company_id=1,
                date_from="2026-01-01",
                date_to="2026-12-31"
            )
            self.assertEqual(sum_c1["kpis"]["total_analyses"], 2)
            self.assertAlmostEqual(sum_c1["kpis"]["avg_evaluacion_global"], 7.8, places=1)

    async def test_dashboard_agents_list_isolation(self):
        """Dashboard agents list must not leak agents between companies."""
        async with self.async_session() as db:
            agents_c6 = await get_agents_list(
                db,
                context=self.superadmin_ctx,
                company_id=6,
                date_from="2026-01-01",
                date_to="2026-12-31"
            )
            names_c6 = [a["agent_name"] for a in agents_c6]
            self.assertEqual(names_c6, ["Agente Demo 6"])

            agents_c1 = await get_agents_list(
                db,
                context=self.superadmin_ctx,
                company_id=1,
                date_from="2026-01-01",
                date_to="2026-12-31"
            )
            names_c1 = [a["agent_name"] for a in agents_c1]
            self.assertIn("Agente BM 1", names_c1)
            self.assertNotIn("Agente Demo 6", names_c1)

    async def test_service_evolution_services_isolation(self):
        """Service evolution services catalog must only list services for selected company."""
        async with self.async_session() as db:
            svcs_c6 = await ServiceEvolutionService.get_services(
                db,
                context=self.superadmin_ctx,
                company_id=6
            )
            s_keys_c6 = [s.service_key for s in svcs_c6]
            self.assertEqual(len(s_keys_c6), 1)
            self.assertEqual(svcs_c6[0].service_id, 6)
            self.assertEqual(svcs_c6[0].total_calls, 1)

            svcs_c1 = await ServiceEvolutionService.get_services(
                db,
                context=self.superadmin_ctx,
                company_id=1
            )
            s_ids_c1 = [s.service_id for s in svcs_c1]
            self.assertIn(1, s_ids_c1)
            self.assertIn(2, s_ids_c1)
            self.assertNotIn(6, s_ids_c1)

    async def test_mass_evaluation_results_isolation(self):
        """MassEvaluationService.count_results and list_results must isolate by company_id without leaking legacy NULLs to non-1 companies."""
        async with self.async_session() as db:
            # Company 6: exactly 1 call (call_demo_6). No legacy call_bm_legacy!
            cnt_c6 = await MassEvaluationService.count_results(
                db,
                company_ids=[6]
            )
            self.assertEqual(cnt_c6, 1)
            res_c6 = await MassEvaluationService.list_results(
                db,
                company_ids=[6]
            )
            self.assertEqual(len(res_c6), 1)
            self.assertEqual(res_c6[0].call_id, "call_demo_6")

            # Company 1: exactly 2 calls (r1 and r_legacy)
            cnt_c1 = await MassEvaluationService.count_results(
                db,
                company_ids=[1]
            )
            self.assertEqual(cnt_c1, 2)
            res_c1 = await MassEvaluationService.list_results(
                db,
                company_ids=[1]
            )
            self.assertEqual(len(res_c1), 2)
            call_ids_c1 = [r.call_id for r in res_c1]
            self.assertIn("call_bm_1", call_ids_c1)
            self.assertIn("call_bm_legacy", call_ids_c1)

    async def test_unauthorized_company_rejection(self):
        """Non-superadmin cannot access another company's data."""
        from fastapi import HTTPException
        # Company 1 admin attempts to query company 6
        with self.assertRaises(HTTPException) as cm:
            company_id = 6
            context = self.c1_admin_ctx
            if company_id is not None and not context.is_super_admin:
                if company_id not in context.allowed_company_ids:
                    raise HTTPException(status_code=403, detail="Acceso denegado a otra empresa.")
        self.assertEqual(cm.exception.status_code, 403)

    async def test_filter_options_cascading_and_unified_response(self):
        """get_filter_options must return scoped companies, services, teams, agents, typologies for company_id."""
        async with self.async_session() as db:
            # Company 6 scope
            res_c6 = await get_filter_options(
                context=self.superadmin_ctx,
                company_id=6,
                db=db,
            )
            self.assertIn("companies", res_c6)
            self.assertIn("services", res_c6)
            self.assertIn("teams", res_c6)
            self.assertIn("agents", res_c6)
            self.assertIn("typologies", res_c6)

            # Services for Company 6: exactly service 6
            s_ids_c6 = [s["service_id"] for s in res_c6["services"]]
            self.assertEqual(s_ids_c6, [6])

            # Teams for Company 6: exactly team 6
            t_ids_c6 = [t["team_id"] for t in res_c6["teams"]]
            self.assertEqual(t_ids_c6, [6])

            # Agents for Company 6: exactly agent_demo_6
            agent_owners_c6 = [a.hubspot_owner_id for a in res_c6["agents"]]
            self.assertEqual(agent_owners_c6, ["agent_demo_6"])

            # Company 1 scope
            res_c1 = await get_filter_options(
                context=self.superadmin_ctx,
                company_id=1,
                db=db,
            )
            s_ids_c1 = [s["service_id"] for s in res_c1["services"]]
            self.assertIn(1, s_ids_c1)
            self.assertIn(2, s_ids_c1)
            self.assertNotIn(6, s_ids_c1)

            t_ids_c1 = [t["team_id"] for t in res_c1["teams"]]
            self.assertIn(1, t_ids_c1)
            self.assertNotIn(6, t_ids_c1)

            agent_owners_c1 = [a.hubspot_owner_id for a in res_c1["agents"]]
            self.assertIn("agent_bm_1", agent_owners_c1)
            self.assertIn("agent_bm_2", agent_owners_c1)
            self.assertNotIn("agent_demo_6", agent_owners_c1)

    async def test_retroactive_cascade_service_mismatch_rejection(self):
        """Cross-company mismatch (service from company 1 queried under company 6) must reject with 400."""
        from fastapi import HTTPException
        async with self.async_session() as db:
            # 1. Direct cascade validation
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    service_id=1,  # Belongs to Company 1
                    company_id=6,   # Queried under Company 6
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("El servicio seleccionado no pertenece a la empresa indicada", cm.exception.detail)

            # 2. Service resolution rejection
            with self.assertRaises(HTTPException) as cm_res:
                await resolve_service_id(
                    db,
                    service_id=1,
                    company_ids=[6]
                )
            self.assertEqual(cm_res.exception.status_code, 400)
            self.assertIn("El servicio seleccionado no pertenece a la empresa indicada", cm_res.exception.detail)

            # 3. get_filter_options rejection
            with self.assertRaises(HTTPException) as cm_opt:
                await get_filter_options(
                    context=self.superadmin_ctx,
                    company_id=6,
                    service_id=1,
                    db=db,
                )
            self.assertEqual(cm_opt.exception.status_code, 400)

    async def test_retroactive_cascade_team_mismatch_rejection(self):
        """Cross-company mismatch (team from company 1 queried under company 6) must reject with 400."""
        from fastapi import HTTPException
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    team_id=1,     # Belongs to Company 1
                    company_id=6,   # Queried under Company 6
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("El equipo seleccionado no pertenece a la empresa indicada", cm.exception.detail)

            with self.assertRaises(HTTPException) as cm_opt:
                await get_filter_options(
                    context=self.superadmin_ctx,
                    company_id=6,
                    team_id=1,
                    db=db,
                )
            self.assertEqual(cm_opt.exception.status_code, 400)

    async def test_list_services_company_isolation(self):
        """list_services must strictly isolate services by company_id and enforce tenant permissions."""
        from fastapi import HTTPException
        async with self.async_session() as db:
            # Superadmin querying company 6
            svcs_c6 = await list_services(
                context=self.superadmin_ctx,
                company_id=6,
                db=db,
            )
            self.assertEqual(len(svcs_c6), 1)
            self.assertEqual(svcs_c6[0].service_id, 6)

            # Superadmin querying company 1
            svcs_c1 = await list_services(
                context=self.superadmin_ctx,
                company_id=1,
                db=db,
            )
            s_ids_c1 = [s.service_id for s in svcs_c1]
            self.assertIn(1, s_ids_c1)
            self.assertIn(2, s_ids_c1)
            self.assertNotIn(6, s_ids_c1)

            # Company 1 admin attempting to list company 6 services -> 403
            with self.assertRaises(HTTPException) as cm:
                await list_services(
                    context=self.c1_admin_ctx,
                    company_id=6,
                    db=db,
                )
            self.assertEqual(cm.exception.status_code, 403)

    async def test_retroactive_cascade_service_demo_under_c1_rejection(self):
        """Querying Demo service (service_id=6) under company_id=1 must reject with 400."""
        from fastapi import HTTPException
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    service_id=6,   # Belongs to Company 6 (Demo)
                    company_id=1,   # Queried under Company 1 (BM)
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("El servicio seleccionado no pertenece a la empresa indicada", cm.exception.detail)

    async def test_retroactive_cascade_agent_mismatch_rejection(self):
        """Querying an agent belonging to company 1 under company 6 (or vice versa) must reject with 400."""
        from fastapi import HTTPException
        async with self.async_session() as db:
            # 1. Direct cascade validation with BM agent under company 6
            with self.assertRaises(HTTPException) as cm1:
                await validate_team_service_cascade(
                    db,
                    company_id=6,
                    hubspot_owner_id="agent_bm_1",
                )
            self.assertEqual(cm1.exception.status_code, 400)
            self.assertIn("El agente seleccionado no pertenece a la empresa indicada", cm1.exception.detail)

            # 2. Direct cascade validation with Demo agent under company 1
            with self.assertRaises(HTTPException) as cm2:
                await validate_team_service_cascade(
                    db,
                    company_id=1,
                    hubspot_owner_id="agent_demo_6",
                )
            self.assertEqual(cm2.exception.status_code, 400)
            self.assertIn("El agente seleccionado no pertenece a la empresa indicada", cm2.exception.detail)

    async def test_real_company_names_and_no_demo_codes(self):
        """Boston Medical (company_id=1) must maintain real agent names and NO demo codes (AC/VT)."""
        async with self.async_session() as db:
            agents_c1 = await get_available_agents(
                db,
                context=self.superadmin_ctx,
                company_id=1,
            )
            # Find agent_bm_1 and agent_bm_2
            bm_names = {a.hubspot_owner_id: a.agent_name for a in agents_c1}
            bm_codes = {a.hubspot_owner_id: a.agent_code for a in agents_c1}

            self.assertEqual(bm_names.get("agent_bm_1"), "Agente BM 1")
            self.assertEqual(bm_names.get("agent_bm_2"), "Agente BM 2")

            # Must NOT use demo code pattern (AC-F, AC-B, VT-C, VT-R)
            for oid, code in bm_codes.items():
                self.assertFalse(
                    code.startswith("AC-") or code.startswith("VT-"),
                    f"Agent {oid} in company 1 unexpectedly received demo code {code}"
                )


if __name__ == "__main__":
    unittest.main()
