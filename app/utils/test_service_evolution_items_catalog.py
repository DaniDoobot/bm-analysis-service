"""
Unit tests for Dynamic Service Evolution Items Catalog.
"""
import unittest
from datetime import datetime, timezone

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
from app.models.prompts import Prompt, BaseStructureTypology
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
from app.routers.service_evolution import get_criteria, get_evolution


class TestServiceEvolutionItemsCatalog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

        async with self.async_session() as db:
            # 1. Companies
            c_bm = Company(company_id=1, company_name="Boston Medical Group", company_key="boston-medical", is_active=True)
            c_demo = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_active=True)
            db.add_all([c_bm, c_demo])

            # 2. Services
            s_bm = Service(service_id=1, service_name="Front BM", service_key="front", company_id=1, is_active=True)
            s_atc = Service(service_id=10, service_name="Atención al Cliente", service_key="atencion-al-cliente", company_id=7, is_active=True)
            s_vts = Service(service_id=11, service_name="Ventas Demo", service_key="ventas", company_id=7, is_active=True)
            db.add_all([s_bm, s_atc, s_vts])

            # 3. Typologies for Atención al Cliente (company 7, service 10)
            t_cg = Typology(typology_id=101, typology_name="Consulta General", typology_key="consulta_general", service_id=10, company_id=7, is_active=True)
            t_ri = Typology(typology_id=102, typology_name="Reclamación Incidencia", typology_key="reclamacion_incidencia", service_id=10, company_id=7, is_active=True)
            # Typology for Ventas (company 7, service 11)
            t_vt = Typology(typology_id=103, typology_name="Venta Directa", typology_key="venta_directa", service_id=11, company_id=7, is_active=True)
            db.add_all([t_cg, t_ri, t_vt])

            # 4. Active Prompts
            p_bm = Prompt(prompt_id=1, prompt_name="Prompt BM", prompt_type="audio", service_id=1, company_id=1, is_active=True, is_archived=False)
            p_atc = Prompt(prompt_id=20, prompt_name="Prompt ATC Demo", prompt_type="audio", service_id=10, company_id=7, is_active=True, is_archived=False)
            p_vts = Prompt(prompt_id=30, prompt_name="Prompt Ventas Demo", prompt_type="audio", service_id=11, company_id=7, is_active=True, is_archived=False)
            db.add_all([p_bm, p_atc, p_vts])
            await db.flush()

            # 5. Criteria for Prompt 1 (Boston Medical)
            c_bm1 = PromptCriterion(criterion_id=1, prompt_id=1, criterion_key="bm_criterio_1", criterion_name="BM Criterio 1", criterion_type="score_1_10", order_index=1, is_active=True)
            c_bm2 = PromptCriterion(criterion_id=2, prompt_id=1, criterion_key="bm_criterio_2", criterion_name="BM Criterio 2", criterion_type="boolean", order_index=2, is_active=True)
            db.add_all([c_bm1, c_bm2])

            # 6. Criteria for Prompt 20 (Demo ATC): 6 for consulta_general, 6 for reclamacion_incidencia = 12 total
            self.cg_keys = [f"cg_criterio_{i}" for i in range(1, 7)]
            self.ri_keys = [f"ri_criterio_{i}" for i in range(1, 7)]

            crit_id = 10
            for i, k in enumerate(self.cg_keys):
                crit_id += 1
                c_type = "boolean" if i == 5 else "score_1_10"
                c = PromptCriterion(criterion_id=crit_id, prompt_id=20, criterion_key=k, criterion_name=f"CG Item {i+1}", criterion_type=c_type, order_index=i+1, is_active=True)
                db.add(c)
                db.add(PromptCriterionTypology(criterion_id=crit_id, typology_id=101))

            for i, k in enumerate(self.ri_keys):
                crit_id += 1
                c_type = "boolean" if i == 5 else "score_1_10"
                c = PromptCriterion(criterion_id=crit_id, prompt_id=20, criterion_key=k, criterion_name=f"RI Item {i+1}", criterion_type=c_type, order_index=i+7, is_active=True)
                db.add(c)
                db.add(PromptCriterionTypology(criterion_id=crit_id, typology_id=102))

            # 7. Criteria for Prompt 30 (Demo Ventas)
            self.vt_keys = ["vt_criterio_1", "vt_criterio_2", "vt_criterio_3"]
            for i, k in enumerate(self.vt_keys):
                crit_id += 1
                c = PromptCriterion(criterion_id=crit_id, prompt_id=30, criterion_key=k, criterion_name=f"VT Item {i+1}", criterion_type="score_1_10", order_index=i+1, is_active=True)
                db.add(c)
                db.add(PromptCriterionTypology(criterion_id=crit_id, typology_id=103))

            await db.commit()

        # Tenant contexts
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
            user_email="manager@demo.com",
            raw_role="service_manager",
            normalized_role=InternalRole.SERVICE_MANAGER,
            company_id=7,
            is_super_admin=False,
            allowed_company_ids=[7],
            allowed_service_ids=[10, 11],
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_01_demo_atc_without_typology_returns_all_12_items_no_boston(self):
        """1. Empresa Demo + Atención al Cliente (sin tipología) returns exactly its 12 items, NO Boston Medical items."""
        async with self.async_session() as db:
            criteria = await get_criteria(
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
                db=db,
            )
            returned_keys = [c.criterion_key for c in criteria]
            self.assertEqual(len(returned_keys), 12)
            for k in self.cg_keys:
                self.assertIn(k, returned_keys)
            for k in self.ri_keys:
                self.assertIn(k, returned_keys)

            # Assert NO Boston Medical items appear
            self.assertNotIn("conocimiento_boston_medical", returned_keys)
            self.assertNotIn("explicaciones_medicas", returned_keys)
            self.assertNotIn("claridad_explicacion_economica", returned_keys)
            self.assertNotIn("bm_criterio_1", returned_keys)
            self.assertNotIn("bm_criterio_2", returned_keys)

            # Check metadata fields on CriterionListItem
            item_last = criteria[-1]
            self.assertEqual(item_last.criterion_key, "ri_criterio_6")
            self.assertEqual(item_last.key, "ri_criterio_6")
            self.assertEqual(item_last.type, "boolean")
            self.assertEqual(item_last.total_applicable, 0)
            self.assertTrue(item_last.active)

    async def test_02_demo_atc_typology_a_returns_only_6_items_isolated(self):
        """2. Empresa Demo + Atención al Cliente + consulta_general returns ONLY 6 items of consulta_general."""
        async with self.async_session() as db:
            criteria = await get_criteria(
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
                typology_key="consulta_general",
                db=db,
            )
            returned_keys = [c.criterion_key for c in criteria]
            self.assertEqual(len(returned_keys), 6)
            self.assertEqual(returned_keys, self.cg_keys)

            for k in self.ri_keys:
                self.assertNotIn(k, returned_keys)
            self.assertNotIn("conocimiento_boston_medical", returned_keys)

    async def test_03_demo_atc_typology_b_returns_only_6_items_isolated(self):
        """3. Empresa Demo + Atención al Cliente + reclamacion_incidencia returns ONLY 6 items of reclamacion_incidencia."""
        async with self.async_session() as db:
            criteria = await get_criteria(
                context=self.demo_ctx,
                company_id=7,
                service_id=10,
                typology_key="reclamacion_incidencia",
                db=db,
            )
            returned_keys = [c.criterion_key for c in criteria]
            self.assertEqual(len(returned_keys), 6)
            self.assertEqual(returned_keys, self.ri_keys)

            for k in self.cg_keys:
                self.assertNotIn(k, returned_keys)
            self.assertNotIn("conocimiento_boston_medical", returned_keys)

    async def test_04_demo_ventas_returns_only_ventas_items(self):
        """4. Empresa Demo + Ventas returns ONLY Ventas items (disjoint from Atención al Cliente)."""
        async with self.async_session() as db:
            criteria = await get_criteria(
                context=self.demo_ctx,
                company_id=7,
                service_id=11,
                db=db,
            )
            returned_keys = [c.criterion_key for c in criteria]
            self.assertEqual(len(returned_keys), 3)
            self.assertEqual(returned_keys, self.vt_keys)

            for k in self.cg_keys + self.ri_keys:
                self.assertNotIn(k, returned_keys)

    async def test_05_company_inference_from_service_id(self):
        """5. Passing only service_id=10 without company_id infers company 7 and isolates completely from Boston."""
        async with self.async_session() as db:
            criteria = await get_criteria(
                context=self.superadmin_ctx,
                service_id=10,
                db=db,
            )
            returned_keys = [c.criterion_key for c in criteria]
            self.assertEqual(len(returned_keys), 12)
            self.assertNotIn("conocimiento_boston_medical", returned_keys)
            self.assertNotIn("bm_criterio_1", returned_keys)

    async def test_06_dashboard_evaluation_items_consistent_with_service_evolution(self):
        """6. /bm/evaluation-items/filter-options returns the same items as /bm/service-evolution/criteria."""
        async with self.async_session() as db:
            res = await get_evaluation_items_filter_options(
                db=db,
                context=self.demo_ctx,
                company="empresa-demo",
                service="atencion-al-cliente",
                typology="consulta_general",
            )
            items = res["items"]
            keys = [it["key"] for it in items]
            self.assertEqual(len(keys), 6)
            self.assertEqual(keys, self.cg_keys)

    async def test_07_boston_medical_service_preserves_configured_criteria(self):
        """7. Boston Medical query returns Boston Medical configured criteria."""
        async with self.async_session() as db:
            criteria = await get_criteria(
                context=self.superadmin_ctx,
                company_id=1,
                service_id=1,
                db=db,
            )
            returned_keys = [c.criterion_key for c in criteria]
            self.assertEqual(returned_keys, ["bm_criterio_1", "bm_criterio_2"])
            for k in self.cg_keys + self.ri_keys:
                self.assertNotIn(k, returned_keys)

    async def test_08_incompatible_typology_raises_400(self):
        """8. Selecting a typology from another service (e.g. venta_directa in service 10) raises HTTP 400."""
        async with self.async_session() as db:
            with self.assertRaises(HTTPException) as ctx:
                await get_criteria(
                    context=self.demo_ctx,
                    company_id=7,
                    service_id=10,
                    typology_key="venta_directa",
                    db=db,
                )
            self.assertEqual(ctx.exception.status_code, 400)
