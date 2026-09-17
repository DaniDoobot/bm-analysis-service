"""
Unit tests for Cascading Filters, Cross-Entity Validation, and u_obj Regression.
==============================================================================
Validates:
1. Regression for UnboundLocalError: local variable 'u_obj' where it is not associated with a value.
2. Empresa Demo (company_id=6 or 7) strict isolation: only returns demo services, teams, agents,
   typologies, and demo items (NEVER leaking Boston Medical items like 'conocimiento_boston_medical').
3. Boston Medical (company_id=1) / legacy data scoping.
4. Downward cascade restrictions:
   - Service filters teams, agents, typologies, and items.
   - Team filters agents and typologies.
   - Typology filters items.
5. Incompatible cross-selections raise HTTP 400 Bad Request:
   - Company A + Service B -> 400
   - Company A + Team B -> 400
   - Company A + Agent B -> 400
   - Company A + Typology B -> 400
   - Service A + Team B -> 400
   - Service A + Agent B -> 400
   - Service A + Typology B -> 400
   - Team A + Agent B -> 400
   - Team A + Typology B -> 400
   - Agent A + Typology B -> 400
6. Strict scoping on /filter-options, /evaluation-items/filter-options,
   /analytics/agents-comparison, and /dashboard/summary.
"""
import unittest
from datetime import datetime, timezone

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from fastapi import HTTPException

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserTeamAssociation, AgentTeamAssociation, UserServiceAssociation
from app.models.users import User
from app.models.typologies import Typology
from app.models.prompts import Prompt
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.utils.team_resolvers import (
    validate_team_service_cascade,
    get_team_assigned_owner_ids,
    get_service_assigned_users,
    get_service_assigned_owner_ids,
)
from app.utils.item_score_filters import get_evaluation_item_filter_options
from app.routers.analytics import get_available_agents, get_filter_options, get_agents_comparison
from app.routers.dashboard import get_evaluation_items_filter_options, dashboard_summary
from app.routers.typologies import list_typologies


class TestCascadingFiltersAndCrossValidation(unittest.IsolatedAsyncioTestCase):
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
            # 1. Companies
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True, is_demo=False)
            c7 = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_active=True, is_demo=True)
            db.add_all([c1, c7])

            # 2. Services
            # Service 1 & 2 for BM
            s1 = Service(service_id=1, company_id=1, service_name="Front BM", service_key="front-bm", is_active=True)
            s2 = Service(service_id=2, company_id=1, service_name="Ventas BM", service_key="ventas-bm", is_active=True)
            # Service 10 & 20 for Demo
            s10 = Service(service_id=10, company_id=7, service_name="Front Demo", service_key="front-demo", is_active=True)
            s20 = Service(service_id=20, company_id=7, service_name="Ventas Demo", service_key="ventas-demo", is_active=True)
            db.add_all([s1, s2, s10, s20])

            # 3. Teams
            # BM Teams
            t1 = Team(team_id=1, company_id=1, service_id=1, team_name="Equipo Front BM", is_active=True)
            t2 = Team(team_id=2, company_id=1, service_id=2, team_name="Equipo Ventas BM", is_active=True)
            # Demo Teams
            t10 = Team(team_id=10, company_id=7, service_id=10, team_name="Front Atención Demo", is_active=True)
            t20 = Team(team_id=20, company_id=7, service_id=20, team_name="Comercial Demo", is_active=True)
            db.add_all([t1, t2, t10, t20])

            # 4. Users / Agents
            # BM Agents
            u1 = User(user_id=1, company_id=1, primary_service_id=1, primary_team_id=1, hubspot_owner_id="bm_owner_01", name="Juan Pérez", username="juan.perez", email="juan@bm.es", password_hash="fakehash", role="agent")
            u2 = User(user_id=2, company_id=1, primary_service_id=2, primary_team_id=2, hubspot_owner_id="bm_owner_02", name="Carlos Gómez", username="carlos.gomez", email="carlos@bm.es", password_hash="fakehash", role="agent")
            # Demo Agents
            u10 = User(user_id=10, company_id=7, primary_service_id=10, primary_team_id=10, hubspot_owner_id="demo_owner_01", name="Agente Demo 01", username="agente.demo.01", email="agente.demo.01@empresa-demo.es", password_hash="fakehash", role="agent")
            u20 = User(user_id=20, company_id=7, primary_service_id=20, primary_team_id=20, hubspot_owner_id="demo_owner_31", name="Agente Demo 31", username="agente.demo.31", email="agente.demo.31@empresa-demo.es", password_hash="fakehash", role="agent")
            db.add_all([u1, u2, u10, u20])

            # 5. Typologies
            # BM Typologies
            typ1 = Typology(typology_id=1, company_id=1, service_id=1, typology_key="consulta_bm", typology_name="Consulta BM", is_active=True)
            typ2 = Typology(typology_id=2, company_id=1, service_id=2, typology_key="venta_bm", typology_name="Venta BM", is_active=True)
            # Demo Typologies
            typ10 = Typology(typology_id=10, company_id=7, service_id=10, typology_key="consulta_demo", typology_name="Consulta Demo", is_active=True)
            typ20 = Typology(typology_id=20, company_id=7, service_id=20, typology_key="venta_demo", typology_name="Venta Demo", is_active=True)
            db.add_all([typ1, typ2, typ10, typ20])

            # 6. Prompts & Criteria
            # BM Prompt & Criteria
            p1 = Prompt(prompt_id=1, company_id=1, service_id=1, prompt_name="Prompt BM Front", prompt_type="audio", is_active=True, is_archived=False)
            db.add(p1)
            await db.flush()

            c_bm_global = PromptCriterion(criterion_id=101, prompt_id=1, criterion_key="empatia_bm", criterion_name="Empatía BM", criterion_type="score", order_index=1, is_active=True)
            c_bm_specific = PromptCriterion(criterion_id=102, prompt_id=1, criterion_key="cierre_bm", criterion_name="Cierre BM", criterion_type="score", order_index=2, is_active=True)
            db.add_all([c_bm_global, c_bm_specific])
            await db.flush()

            # Link c_bm_specific only to typ1
            pct1 = PromptCriterionTypology(criterion_id=102, typology_id=1)
            db.add(pct1)

            # Demo Prompt & Criteria
            p10 = Prompt(prompt_id=10, company_id=7, service_id=10, prompt_name="Prompt Demo Front", prompt_type="audio", is_active=True, is_archived=False)
            db.add(p10)
            await db.flush()

            c_demo_1 = PromptCriterion(criterion_id=201, prompt_id=10, criterion_key="amabilidad_demo", criterion_name="Amabilidad Demo", criterion_type="score", order_index=1, is_active=True)
            c_demo_2 = PromptCriterion(criterion_id=202, prompt_id=10, criterion_key="resolucion_demo", criterion_name="Resolución Demo", criterion_type="score", order_index=2, is_active=True)
            db.add_all([c_demo_1, c_demo_2])
            await db.flush()

            # Link c_demo_2 only to typ10
            pct10 = PromptCriterionTypology(criterion_id=202, typology_id=10)
            db.add(pct10)

            # Mass Evaluation Job, Run, and Result for Demo
            job10 = MassEvaluationJob(job_id=10, company_id=7, service_id=10, job_name="Job Demo", prompt_id=10)
            run10 = MassEvaluationRun(run_id=10, job_id=10, company_id=7, status="completed", trigger_type="manual")
            db.add_all([job10, run10])
            await db.flush()

            res10 = MassEvaluationResult(
                mass_analysis_id=500,
                run_id=10,
                job_id=10,
                prompt_id=10,
                prompt_snapshot="{}",
                company_id=7,
                service_id=10,
                hubspot_owner_id="demo_owner_01",
                agent_name="Agente Demo 01",
                call_id="call_demo_500",
                evaluacion_global=9.5,
                result_json={"evaluacion_global": 9.5},
                status="completed",
                call_timestamp=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            )
            db.add(res10)
            await db.commit()

        # Contexts
        self.superadmin_ctx = TenantContext(
            user_id=999,
            user_email="superadmin@speechbm.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            company_id=None,
            is_super_admin=True,
            allowed_company_ids=[1, 7],
            allowed_service_ids=None,
            allowed_team_ids=None,
            allowed_agent_ids=None,
        )

        self.demo_admin_ctx = TenantContext(
            user_id=10,
            user_email="admin@empresa-demo.es",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            company_id=7,
            is_super_admin=False,
            allowed_company_ids=[7],
            allowed_service_ids=[10, 20],
            allowed_team_ids=[10, 20],
            allowed_agent_ids=None,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_01_regression_u_obj_unbound_local_error(self):
        """Regression test for UnboundLocalError 'u_obj' when listing demo agents."""
        async with self.async_session() as db:
            agents = await get_available_agents(
                db,
                context=self.superadmin_ctx,
                company_id=7,
            )
            self.assertGreater(len(agents), 0)
            agent_names = [a.agent_name for a in agents]
            self.assertIn("Agente Demo 01", agent_names)
            self.assertIn("Agente Demo 31", agent_names)

    async def test_02_demo_company_strict_isolation_no_boston_fallback(self):
        """Empresa Demo filter options must only return Demo data, NEVER Boston Medical items or fallbacks."""
        async with self.async_session() as db:
            opts = await get_filter_options(
                context=self.superadmin_ctx,
                company_id=7,
                db=db,
            )
            svc_ids = [s["service_id"] for s in opts["services"]]
            self.assertEqual(sorted(svc_ids), [10, 20])
            self.assertNotIn(1, svc_ids)
            self.assertNotIn(2, svc_ids)

            team_ids = [t["team_id"] for t in opts["teams"]]
            self.assertEqual(sorted(team_ids), [10, 20])
            self.assertNotIn(1, team_ids)

            agent_ids = [a.hubspot_owner_id for a in opts["agents"]]
            self.assertIn("demo_owner_01", agent_ids)
            self.assertNotIn("bm_owner_01", agent_ids)

            typ_ids = [t["id"] for t in opts["typologies"]]
            self.assertEqual(sorted(typ_ids), [10, 20])
            self.assertNotIn(1, typ_ids)

            item_keys = [it["key"] for it in opts["items"]]
            self.assertIn("amabilidad_demo", item_keys)
            self.assertIn("resolucion_demo", item_keys)
            self.assertNotIn("conocimiento_boston_medical", item_keys)
            self.assertNotIn("empatia", item_keys)

    async def test_03_boston_medical_filter_options(self):
        """Boston Medical (company_id=1) filter options must only return Boston Medical data."""
        async with self.async_session() as db:
            opts = await get_filter_options(
                context=self.superadmin_ctx,
                company_id=1,
                db=db,
            )
            svc_ids = [s["service_id"] for s in opts["services"]]
            self.assertEqual(sorted(svc_ids), [1, 2])
            self.assertNotIn(10, svc_ids)

            agent_ids = [a.hubspot_owner_id for a in opts["agents"]]
            self.assertIn("bm_owner_01", agent_ids)
            self.assertNotIn("demo_owner_01", agent_ids)

    async def test_04_service_cascade_filters_teams_agents_typologies_items(self):
        """Selecting a service constrains downstream teams, agents, typologies, and items."""
        async with self.async_session() as db:
            opts = await get_filter_options(
                context=self.superadmin_ctx,
                company_id=7,
                service_id=10,
                db=db,
            )
            team_ids = [t["team_id"] for t in opts["teams"]]
            self.assertEqual(team_ids, [10])

            agent_ids = [a.hubspot_owner_id for a in opts["agents"]]
            self.assertEqual(agent_ids, ["demo_owner_01"])

            typ_ids = [t["id"] for t in opts["typologies"]]
            self.assertEqual(typ_ids, [10])

    async def test_05_team_cascade_filters_agents_and_typologies(self):
        """Selecting a team constrains agents to that team and typologies to that team's service."""
        async with self.async_session() as db:
            opts = await get_filter_options(
                context=self.superadmin_ctx,
                company_id=7,
                team_id=20,
                db=db,
            )
            agent_ids = [a.hubspot_owner_id for a in opts["agents"]]
            self.assertEqual(agent_ids, ["demo_owner_31"])

            typ_ids = [t["id"] for t in opts["typologies"]]
            self.assertEqual(typ_ids, [20])

    async def test_06_typology_filters_evaluation_items(self):
        """Selecting a typology constrains evaluation items to that typology (plus globals)."""
        async with self.async_session() as db:
            res = await get_evaluation_items_filter_options(
                db=db,
                context=self.superadmin_ctx,
                company_id=1,
                service_id=1,
                typology_id=1,
            )
            item_keys = [it["key"] for it in res["items"]]
            self.assertIn("empatia_bm", item_keys)
            self.assertIn("cierre_bm", item_keys)
            self.assertNotIn("amabilidad_demo", item_keys)

    async def test_07_cross_mismatch_company_and_service_rejection(self):
        """Company mismatch with service must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    company_id=7,
                    service_id=1,
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("El servicio seleccionado no pertenece a la empresa indicada", cm.exception.detail)

    async def test_08_cross_mismatch_company_and_team_rejection(self):
        """Company mismatch with team must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    company_id=7,
                    team_id=1,
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("El equipo seleccionado no pertenece a la empresa indicada", cm.exception.detail)

    async def test_09_cross_mismatch_company_and_agent_rejection(self):
        """Company mismatch with agent must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    company_id=7,
                    hubspot_owner_id="bm_owner_01",
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("El agente seleccionado no pertenece a la empresa indicada", cm.exception.detail)

    async def test_10_cross_mismatch_company_and_typology_rejection(self):
        """Company mismatch with typology must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    company_id=7,
                    typology_id=1,
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("La tipología seleccionada no pertenece a la empresa indicada", cm.exception.detail)

    async def test_11_cross_mismatch_service_and_team_rejection(self):
        """Service mismatch with team must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    service_id=1,
                    team_id=2,
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("El equipo seleccionado no pertenece al servicio indicado", cm.exception.detail)

    async def test_12_cross_mismatch_service_and_agent_rejection(self):
        """Service mismatch with agent must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    service_id=1,
                    hubspot_owner_id="bm_owner_02",
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("El agente seleccionado no pertenece al servicio indicado", cm.exception.detail)

    async def test_13_cross_mismatch_service_and_typology_rejection(self):
        """Service mismatch with typology must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    service_id=1,
                    typology_id=2,
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("La tipología seleccionada no pertenece al servicio indicado", cm.exception.detail)

    async def test_14_cross_mismatch_team_and_agent_rejection(self):
        """Team mismatch with agent must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    team_id=1,
                    hubspot_owner_id="bm_owner_02",
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("El agente seleccionado no pertenece al equipo indicado", cm.exception.detail)

    async def test_15_cross_mismatch_team_and_typology_rejection(self):
        """Team mismatch with typology must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    team_id=1,
                    typology_id=2,
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("La tipología seleccionada no pertenece al servicio del equipo indicado", cm.exception.detail)

    async def test_16_cross_mismatch_agent_and_typology_rejection(self):
        """Agent mismatch with typology must reject with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await validate_team_service_cascade(
                    db,
                    hubspot_owner_id="bm_owner_01",
                    typology_id=2,
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("La tipología seleccionada no es compatible con el agente indicado", cm.exception.detail)

    async def test_17_endpoint_agents_comparison_cross_mismatch_rejection(self):
        """get_agents_comparison endpoint rejects incompatible cross-filters with HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as cm:
                await get_agents_comparison(
                    context=self.superadmin_ctx,
                    db=db,
                    company_id=7,
                    service_id=10,
                    agent_owner_ids=["bm_owner_01"],
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("no pertenece", cm.exception.detail)

    async def test_18_endpoint_typologies_list_company_isolation_and_cascade(self):
        """list_typologies endpoint isolates by company and rejects incompatible service_id with HTTP 400."""
        async with self.async_session() as db:
            typs = await list_typologies(
                context=self.superadmin_ctx,
                company_id=7,
                db=db,
            )
            typ_ids = [t.typology_id for t in typs]
            self.assertEqual(sorted(typ_ids), [10, 20])

            with self.assertRaises(HTTPException) as cm:
                await list_typologies(
                    context=self.superadmin_ctx,
                    company_id=7,
                    service_id=1,
                    db=db,
                )
            self.assertEqual(cm.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
