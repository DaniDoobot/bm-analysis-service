"""
Unit tests for Typology-Isolated Items Filtering and Service Evolution 0-calls Handling.
=======================================================================================
Verifies:
1. Typology X returns only items of X (6 items).
2. Typology Y returns only items of Y (6 items).
3. X and Y do not share items.
4. Without typology returns all 12 items for the service.
5. Empresa Demo never receives Boston Medical fallback items.
6. Incompatible typology with company/service raises HTTP 400.
7. Service Evolution with 0 calls returns HTTP 200 with schema-compliant empty/zero data.
8. Service Evolution accepts service string and handles norm_direction cleanly.
"""
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
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
from app.models.teams import Team
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
from app.utils.item_score_filters import get_evaluation_item_filter_options
from app.routers.dashboard import get_evaluation_items_filter_options
from app.routers.analytics import get_filter_options, get_analytics_items
from app.services.service_evolution_service import ServiceEvolutionService
from app.routers.service_evolution import get_evolution, get_criteria


class TestTypologyItemsAndEvolutionZeroCalls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

        async with self.async_session() as db:
            # 1. Companies
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True, is_demo=False)
            c7 = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_active=True, is_demo=True)
            db.add_all([c1, c7])

            # 2. Services
            s1 = Service(service_id=1, company_id=1, service_name="Front BM", service_key="front-bm", is_active=True)
            s3 = Service(service_id=3, company_id=1, service_name="Asesores Comerciales", service_key="asesores-comerciales", is_active=True)
            s10 = Service(service_id=10, company_id=7, service_name="Atención al Cliente", service_key="atencion-cliente", is_active=True)
            s20 = Service(service_id=20, company_id=7, service_name="Ventas Demo", service_key="ventas-demo", is_active=True)
            db.add_all([s1, s3, s10, s20])

            # 3. Teams
            t10 = Team(team_id=10, company_id=7, service_id=10, team_name="Equipo Atención", is_active=True)
            db.add(t10)

            # 4. Users
            u10 = User(user_id=10, company_id=7, primary_service_id=10, primary_team_id=10, hubspot_owner_id="demo_owner_01", name="Agente Demo 01", username="agente.01", email="a01@demo.es", password_hash="h", role="agent")
            db.add(u10)

            # 5. Typologies
            t_gen = Typology(typology_id=101, company_id=7, service_id=10, typology_key="consulta_general", typology_name="Consulta General", is_active=True)
            t_sop = Typology(typology_id=102, company_id=7, service_id=10, typology_key="soporte_tecnico", typology_name="Soporte Técnico", is_active=True)
            t_rec = Typology(typology_id=103, company_id=7, service_id=10, typology_key="reclamacion_incidencia", typology_name="Reclamación e Incidencia", is_active=True)
            t_fac = Typology(typology_id=104, company_id=7, service_id=10, typology_key="facturacion_cobros", typology_name="Facturación y Cobros", is_active=True)
            t_ven = Typology(typology_id=201, company_id=7, service_id=20, typology_key="captacion_nuevo", typology_name="Captación Nuevo", is_active=True)
            t_bm1 = Typology(typology_id=1, company_id=1, service_id=1, typology_key="consulta_bm", typology_name="Consulta BM", is_active=True)
            db.add_all([t_gen, t_sop, t_rec, t_fac, t_ven, t_bm1])

            # 6. Prompts for Empresa Demo Atención al Cliente
            p_gen = Prompt(prompt_id=10, company_id=7, service_id=10, prompt_name="Calidad Atención General", prompt_type="audio", is_active=True, is_archived=False)
            p_rec = Prompt(prompt_id=11, company_id=7, service_id=10, prompt_name="Gestión Reclamaciones", prompt_type="audio", is_active=True, is_archived=False)
            db.add_all([p_gen, p_rec])
            await db.flush()

            # 6 criteria for p_gen
            gen_keys = ["saludo_identificacion", "escucha_activa", "resolucion_consulta", "conocimiento_catalogo", "cordialidad_empatia", "despedida_protocolo"]
            gen_crits = []
            for i, k in enumerate(gen_keys, start=1):
                gen_crits.append(PromptCriterion(criterion_id=100 + i, prompt_id=10, criterion_key=k, criterion_name=k.replace("_", " ").capitalize(), criterion_type="score_1_10", order_index=i * 10, is_active=True))
            db.add_all(gen_crits)

            # 6 criteria for p_rec
            rec_keys = ["acogida_emocional", "analisis_conflicto", "gestion_frustracion", "propuesta_solucion", "compromiso_tiempos", "cierre_reclamacion"]
            rec_crits = []
            for i, k in enumerate(rec_keys, start=1):
                rec_crits.append(PromptCriterion(criterion_id=200 + i, prompt_id=11, criterion_key=k, criterion_name=k.replace("_", " ").capitalize(), criterion_type="score_1_10", order_index=i * 10, is_active=True))
            db.add_all(rec_crits)
            await db.flush()

            # 7. Mass evaluations
            job1 = MassEvaluationJob(job_id=1, company_id=7, service_id=10, job_name="Job Demo", prompt_id=10)
            run1 = MassEvaluationRun(run_id=1, job_id=1, company_id=7, status="completed", trigger_type="manual")
            db.add_all([job1, run1])
            await db.flush()

            # Run 1: Call with consulta_general
            r_gen = MassEvaluationResult(
                mass_analysis_id=1,
                run_id=1,
                job_id=1,
                prompt_id=10,
                prompt_snapshot="{}",
                company_id=7,
                service_id=10,
                typology_id=101,
                typology_key="consulta_general",
                typology_name="Consulta General",
                hubspot_owner_id="demo_owner_01",
                agent_name="Agente Demo 01",
                call_id="call_01",
                evaluacion_global=8.5,
                result_json={"evaluacion_global": 8.5},
                status="completed",
                call_timestamp=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
            )
            # Run 1: Call with reclamacion_incidencia
            r_rec = MassEvaluationResult(
                mass_analysis_id=2,
                run_id=1,
                job_id=1,
                prompt_id=11,
                prompt_snapshot="{}",
                company_id=7,
                service_id=10,
                typology_id=103,
                typology_key="reclamacion_incidencia",
                typology_name="Reclamación e Incidencia",
                hubspot_owner_id="demo_owner_01",
                agent_name="Agente Demo 01",
                call_id="call_02",
                evaluacion_global=7.0,
                result_json={"evaluacion_global": 7.0},
                status="completed",
                call_timestamp=datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc),
            )
            db.add_all([r_gen, r_rec])
            await db.flush()

            # Criterion results for call 1 (consulta_general)
            cid = 1
            for k in gen_keys:
                db.add(MassEvaluationCriterionResult(
                    id=cid,
                    call_id="call_01",
                    run_id=1,
                    job_id=1,
                    mass_analysis_id=1,
                    prompt_id=10,
                    service_id=10,
                    typology_id=101,
                    typology_key="consulta_general",
                    typology_name="Consulta General",
                    criterion_key=k,
                    criterion_name=k.replace("_", " ").capitalize(),
                    criterion_type="score",
                    numeric_value=8.5,
                    is_applicable=True,
                ))
                cid += 1

            # Criterion results for call 2 (reclamacion_incidencia)
            for k in rec_keys:
                db.add(MassEvaluationCriterionResult(
                    id=cid,
                    call_id="call_02",
                    run_id=1,
                    job_id=1,
                    mass_analysis_id=2,
                    prompt_id=11,
                    service_id=10,
                    typology_id=103,
                    typology_key="reclamacion_incidencia",
                    typology_name="Reclamación e Incidencia",
                    criterion_key=k,
                    criterion_name=k.replace("_", " ").capitalize(),
                    criterion_type="score",
                    numeric_value=7.0,
                    is_applicable=True,
                ))
                cid += 1

            await db.commit()

        self.superadmin_ctx = TenantContext(
            user_id=999,
            user_email="superadmin@speechbm.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            company_id=None,
            is_super_admin=True,
            allowed_company_ids=[1, 7],
        )

        self.demo_ctx = TenantContext(
            user_id=10,
            user_email="admin@demo.es",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            company_id=7,
            is_super_admin=False,
            allowed_company_ids=[7],
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_a_typology_x_returns_only_items_of_x(self):
        """A) Empresa Demo + Atención al Cliente + consulta_general returns only 6 items of consulta_general."""
        async with self.async_session() as db:
            res = await get_evaluation_items_filter_options(
                db=db,
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
                typology_id=101,
            )
            items = res["items"]
            keys = [it["key"] for it in items]
            self.assertEqual(len(items), 6)
            expected = ["saludo_identificacion", "escucha_activa", "resolucion_consulta", "conocimiento_catalogo", "cordialidad_empatia", "despedida_protocolo"]
            self.assertEqual(sorted(keys), sorted(expected))

    async def test_b_typology_y_returns_only_items_of_y(self):
        """B) Empresa Demo + Atención al Cliente + reclamacion_incidencia returns only 6 items of reclamacion."""
        async with self.async_session() as db:
            res = await get_evaluation_items_filter_options(
                db=db,
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
                typology_id=103,
            )
            items = res["items"]
            keys = [it["key"] for it in items]
            self.assertEqual(len(items), 6)
            expected = ["acogida_emocional", "analisis_conflicto", "gestion_frustracion", "propuesta_solucion", "compromiso_tiempos", "cierre_reclamacion"]
            self.assertEqual(sorted(keys), sorted(expected))

    async def test_c_typologies_x_and_y_do_not_share_items(self):
        """C) Typology X and Y do not share items."""
        async with self.async_session() as db:
            res_x = await get_evaluation_items_filter_options(
                db=db,
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
                typology_id=101,
            )
            res_y = await get_evaluation_items_filter_options(
                db=db,
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
                typology_id=103,
            )
            keys_x = set(it["key"] for it in res_x["items"])
            keys_y = set(it["key"] for it in res_y["items"])
            intersection = keys_x.intersection(keys_y)
            self.assertEqual(len(intersection), 0, f"Unexpected shared items between typologies: {intersection}")

    async def test_d_without_typology_returns_all_items_of_service(self):
        """D) Without typology returns all 12 items for the service."""
        async with self.async_session() as db:
            res = await get_evaluation_items_filter_options(
                db=db,
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
                typology_id=None,
            )
            items = res["items"]
            keys = [it["key"] for it in items]
            self.assertEqual(len(items), 12)
            self.assertIn("saludo_identificacion", keys)
            self.assertIn("acogida_emocional", keys)

    async def test_e_empresa_demo_never_receives_boston_fallbacks(self):
        """E) Empresa Demo never receives Boston Medical fallback items."""
        async with self.async_session() as db:
            # Via /evaluation-items/filter-options
            res = await get_evaluation_items_filter_options(
                db=db,
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
            )
            keys = [it["key"] for it in res["items"]]
            self.assertNotIn("claridad", keys)
            self.assertNotIn("empatia", keys)
            self.assertNotIn("cierre_cita", keys)

            # Via /analytics/filter-options
            f_opts = await get_filter_options(
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
                db=db,
            )
            f_keys = [it["key"] for it in f_opts["items"]]
            self.assertNotIn("claridad", f_keys)
            self.assertNotIn("cierre_cita", f_keys)

            # Via /analytics/items
            a_items = await get_analytics_items(
                context=self.demo_ctx,
                db=db,
                company_id=7,
                service_id=10,
            )
            a_keys = [it["key"] if isinstance(it, dict) else it.key for it in a_items]
            self.assertNotIn("claridad", a_keys)
            self.assertNotIn("cierre_cita", a_keys)

    async def test_f_incompatible_typology_raises_http_400(self):
        """F) Incompatible typology with service or company raises HTTP 400 Bad Request."""
        async with self.async_session() as db:
            # Typology 201 belongs to service 20 (Ventas Demo), NOT service 10 (Atención al Cliente)
            with self.assertRaises(HTTPException) as cm:
                await get_evaluation_items_filter_options(
                    db=db,
                    context=self.demo_ctx,
                    company_id=7,
                    service_id=10,
                    typology_id=201,
                )
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("no pertenece al servicio indicado", cm.exception.detail)

            # Typology 1 belongs to company 1 (BM), NOT company 7 (Demo)
            with self.assertRaises(HTTPException) as cm2:
                await get_evaluation_items_filter_options(
                    db=db,
                    context=self.superadmin_ctx,
                    company_id=7,
                    service_id=10,
                    typology_id=1,
                )
            self.assertEqual(cm2.exception.status_code, 400)
            self.assertIn("no pertenece a la empresa indicada", cm2.exception.detail)

    async def test_g_service_evolution_with_zero_calls_returns_http_200(self):
        """G) Service evolution with 0 calls in range returns HTTP 200 with schema-compliant empty/zero data."""
        async with self.async_session() as db:
            # Service 3 (Asesores Comerciales) has 0 calls in DB
            res = await get_evolution(
                context=self.superadmin_ctx,
                service_id=3,
                company_id=1,
                date_from="2026-01-01",
                date_to="2026-03-31",
                granularity="day",
                db=db,
            )
            self.assertIsNotNone(res)
            self.assertEqual(res.summary.total_calls, 0)
            self.assertIsNone(res.summary.avg_evaluacion_global)
            self.assertIsNone(res.summary.avg_claridad)
            self.assertIsNone(res.summary.avg_empatia)
            self.assertIsNone(res.summary.avg_procedimiento)
            self.assertIsNone(res.summary.cierre_cita_rate)
            self.assertIsNone(res.summary.main_typology)
            self.assertEqual(len(res.series), 0)
            self.assertEqual(len(res.by_agent), 0)
            self.assertEqual(len(res.criteria_ranking), 0)

    async def test_h_service_evolution_accepts_service_name_and_normalizes_direction(self):
        """H) Service evolution accepts service param, handles norm_direction cleanly without 500 error."""
        async with self.async_session() as db:
            # Calling with service="asesores-comerciales" and direction="inbound"
            res = await get_evolution(
                context=self.superadmin_ctx,
                service="asesores-comerciales",
                company_id=1,
                direction="inbound",
                db=db,
            )
            self.assertIsNotNone(res)
            self.assertEqual(res.filters.service_name, "Asesores Comerciales")
            self.assertEqual(res.summary.total_calls, 0)

            # Also get_criteria with service string
            crit_list = await get_criteria(
                context=self.superadmin_ctx,
                service="asesores-comerciales",
                company_id=1,
                db=db,
            )
            self.assertEqual(len(crit_list), 0)

    async def test_i_service_evolution_with_calls_returns_expected_metrics(self):
        """I) Service evolution with calls returns HTTP 200 with calculated metrics."""
        async with self.async_session() as db:
            res = await get_evolution(
                context=self.demo_ctx,
                service_id=10,
                company_id=7,
                date_from="2026-03-01",
                date_to="2026-03-31",
                granularity="day",
                db=db,
            )
            self.assertIsNotNone(res)
            self.assertEqual(res.summary.total_calls, 2)
            self.assertAlmostEqual(res.summary.avg_evaluacion_global, 7.75, places=2)
            self.assertGreater(len(res.series), 0)
            self.assertEqual(len(res.by_agent), 1)
            self.assertEqual(res.by_agent[0].agent_name, "Agente Demo 01")


if __name__ == "__main__":
    unittest.main()
