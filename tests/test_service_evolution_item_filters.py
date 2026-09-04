"""
Unit test suite for item_filters in Service Evolution (GET /bm/service-evolution):
1. sin item_filters: resultado idéntico al comportamiento previo;
2. filtro numérico: Empatía min/max;
3. filtro boolean true;
4. filtro boolean false;
5. dos filtros AND;
6. tres filtros AND;
7. filtro neutral: no altera resultados;
8. JSON inválido: 422;
9. min > max: 422;
10. más de 3 filtros: 422;
11. alias: criterion_filters;
12. alias: score_filters;
13. alias: item_score_filters;
14. filtro afecta total_calls;
15. filtro afecta series;
16. filtro afecta by_typology;
17. filtro afecta by_agent;
18. filtro afecta criteria_ranking;
19. llamada sin criterion requerido queda excluida;
20. multiempresa/service scoping intacto.
"""
import os
import sys
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

TEST_DB_NAME = "service_evolution_item_filt_test.db"
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
from app.models.typologies import Typology
from app.models.users import User
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from app.services.service_evolution_service import ServiceEvolutionService
from app.routers.service_evolution import get_evolution
from fastapi import HTTPException


class TestServiceEvolutionItemFilters(unittest.IsolatedAsyncioTestCase):

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

        self.context_super_admin = TenantContext(
            user_id=1,
            email="superadmin@test.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1, 2],
            allowed_service_ids=[1, 2],
        )

        self.context_tenant = TenantContext(
            user_id=2,
            email="company1_user@test.com",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
        )

        # Seed database
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            c1 = Company(company_id=1, company_name="Company 1", company_key="comp1", is_active=True)
            c2 = Company(company_id=2, company_name="Company 2", company_key="comp2", is_active=True)
            s1 = Service(service_id=1, service_name="Front", service_key="front", company_id=1, is_active=True)
            s2 = Service(service_id=2, service_name="Back", service_key="back", company_id=2, is_active=True)

            t1 = Typology(typology_id=1, company_id=1, service_id=1, typology_key="cita", typology_name="Cita", is_active=True, sort_order=1)
            t2 = Typology(typology_id=2, company_id=1, service_id=1, typology_key="otros", typology_name="Otros", is_active=True, sort_order=2)
            t3 = Typology(typology_id=3, company_id=1, service_id=1, typology_key="reagendo", typology_name="Reagendo", is_active=True, sort_order=3)

            u1 = User(user_id=10, username="bryan", email="bryan@test.com", name="Bryan Herrera", role="agent", company_id=1, hubspot_owner_id="33013277", is_active=True, password_hash="dummy")
            u2 = User(user_id=11, username="maria", email="maria@test.com", name="Maria Perez", role="agent", company_id=1, hubspot_owner_id="44023188", is_active=True, password_hash="dummy")

            job1 = MassEvaluationJob(job_id=1, job_name="Job Front", prompt_id=1, is_active=True, schedule_enabled=False, created_by="Tester")
            run1 = MassEvaluationRun(run_id=1, job_id=1, trigger_type="manual", status="completed")

            db.add_all([c1, c2, s1, s2, t1, t2, t3, u1, u2, job1, run1])

            # Seed 3 Calls with distinct criteria:
            # Call 1: Bryan, Cita, empatia=9.0, claridad=8.0, alarma=True, cierre_cita=True, evaluacion_global=8.5
            # Call 2: Bryan, Cita, empatia=6.0, claridad=7.0, alarma=False, cierre_cita=False, evaluacion_global=6.5
            # Call 3: Maria, Otros, empatia=9.5, claridad=9.0, alarma=False, cierre_cita=True, evaluacion_global=9.25
            # Call 4 (Company 2, Service 2): Other tenant call
            # Call 5: Non-evaluable call (is_evaluable=False)

            call_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc)

            r1 = MassEvaluationResult(
                mass_analysis_id=101, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call_101", company_id=1, service_id=1, service_key="front", service_name="Front",
                typology_id=1, typology_key="cita", typology_name="Cita",
                hubspot_owner_id="33013277", agent_name="Bryan Herrera",
                call_timestamp=call_time, analysis_timestamp=call_time,
                status="completed", is_evaluable=True, evaluacion_global=Decimal("8.5"),
                result_json={}, items_json=[]
            )
            r2 = MassEvaluationResult(
                mass_analysis_id=102, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call_102", company_id=1, service_id=1, service_key="front", service_name="Front",
                typology_id=1, typology_key="cita", typology_name="Cita",
                hubspot_owner_id="33013277", agent_name="Bryan Herrera",
                call_timestamp=call_time, analysis_timestamp=call_time,
                status="completed", is_evaluable=True, evaluacion_global=Decimal("6.5"),
                result_json={}, items_json=[]
            )
            r3 = MassEvaluationResult(
                mass_analysis_id=103, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call_103", company_id=1, service_id=1, service_key="front", service_name="Front",
                typology_id=2, typology_key="otros", typology_name="Otros",
                hubspot_owner_id="44023188", agent_name="Maria Perez",
                call_timestamp=call_time, analysis_timestamp=call_time,
                status="completed", is_evaluable=True, evaluacion_global=Decimal("9.25"),
                result_json={}, items_json=[]
            )
            r4 = MassEvaluationResult(
                mass_analysis_id=104, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call_104", company_id=2, service_id=2, service_key="back", service_name="Back",
                typology_id=None, typology_key=None, typology_name=None,
                hubspot_owner_id="99999", agent_name="Tenant 2 Agent",
                call_timestamp=call_time, analysis_timestamp=call_time,
                status="completed", is_evaluable=True, evaluacion_global=Decimal("10.0"),
                result_json={}, items_json=[]
            )
            r5 = MassEvaluationResult(
                mass_analysis_id=105, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call_105", company_id=1, service_id=1, service_key="front", service_name="Front",
                typology_id=1, typology_key="cita", typology_name="Cita",
                hubspot_owner_id="33013277", agent_name="Bryan Herrera",
                call_timestamp=call_time, analysis_timestamp=call_time,
                status="completed", is_evaluable=False, evaluacion_global=None,
                result_json={}, items_json=[]
            )
            db.add_all([r1, r2, r3, r4, r5])

            # Criteria results:
            crit_rows = [
                # Call 101 criteria
                MassEvaluationCriterionResult(
                    mass_analysis_id=101, run_id=1, job_id=1, call_id="call_101",
                    criterion_key="empatia", criterion_name="Empatía", criterion_type="score",
                    numeric_value=Decimal("9.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    mass_analysis_id=101, run_id=1, job_id=1, call_id="call_101",
                    criterion_key="claridad", criterion_name="Claridad", criterion_type="score",
                    numeric_value=Decimal("8.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    mass_analysis_id=101, run_id=1, job_id=1, call_id="call_101",
                    criterion_key="alarma", criterion_name="Alarma", criterion_type="boolean",
                    boolean_value=True, is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    mass_analysis_id=101, run_id=1, job_id=1, call_id="call_101",
                    criterion_key="cierre_cita", criterion_name="Cierre de Cita", criterion_type="boolean",
                    boolean_value=True, is_applicable=True
                ),

                # Call 102 criteria
                MassEvaluationCriterionResult(
                    mass_analysis_id=102, run_id=1, job_id=1, call_id="call_102",
                    criterion_key="empatia", criterion_name="Empatía", criterion_type="score",
                    numeric_value=Decimal("6.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    mass_analysis_id=102, run_id=1, job_id=1, call_id="call_102",
                    criterion_key="claridad", criterion_name="Claridad", criterion_type="score",
                    numeric_value=Decimal("7.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    mass_analysis_id=102, run_id=1, job_id=1, call_id="call_102",
                    criterion_key="alarma", criterion_name="Alarma", criterion_type="boolean",
                    boolean_value=False, is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    mass_analysis_id=102, run_id=1, job_id=1, call_id="call_102",
                    criterion_key="cierre_cita", criterion_name="Cierre de Cita", criterion_type="boolean",
                    boolean_value=False, is_applicable=True
                ),

                # Call 103 criteria
                MassEvaluationCriterionResult(
                    mass_analysis_id=103, run_id=1, job_id=1, call_id="call_103",
                    criterion_key="empatia", criterion_name="Empatía", criterion_type="score",
                    numeric_value=Decimal("9.5"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    mass_analysis_id=103, run_id=1, job_id=1, call_id="call_103",
                    criterion_key="claridad", criterion_name="Claridad", criterion_type="score",
                    numeric_value=Decimal("9.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    mass_analysis_id=103, run_id=1, job_id=1, call_id="call_103",
                    criterion_key="alarma", criterion_name="Alarma", criterion_type="boolean",
                    boolean_value=False, is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    mass_analysis_id=103, run_id=1, job_id=1, call_id="call_103",
                    criterion_key="cierre_cita", criterion_name="Cierre de Cita", criterion_type="boolean",
                    boolean_value=True, is_applicable=True
                ),

                # Call 104 criteria (Tenant 2)
                MassEvaluationCriterionResult(
                    mass_analysis_id=104, run_id=1, job_id=1, call_id="call_104",
                    criterion_key="empatia", criterion_name="Empatía", criterion_type="score",
                    numeric_value=Decimal("10.0"), is_applicable=True
                ),
            ]
            db.add_all(crit_rows)
            await db.commit()

    # -------------------------------------------------------------
    # 1. Sin item_filters: Comportamiento idéntico previo
    # -------------------------------------------------------------
    async def test_01_no_item_filters(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant
            )
            # 4 completed calls in service 1 (101, 102, 103 evaluable + 105 non-evaluable)
            self.assertEqual(res.summary.total_calls, 4)
            self.assertAlmostEqual(res.summary.avg_evaluacion_global, 8.0833, places=2)
            self.assertEqual(len(res.series), 1)
            self.assertEqual(res.series[0].total_calls, 4)
            self.assertEqual(len(res.by_agent), 2)  # Bryan, Maria

    # -------------------------------------------------------------
    # 2. Filtro numérico: Empatía >= 8.0 (min=8.0, max=10.0)
    # -------------------------------------------------------------
    async def test_02_numeric_filter(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            filt = [{"key": "empatia", "min": 8.0, "max": 10.0}]
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant, item_filters=filt
            )
            # Matches Call 101 (empatia 9.0) and Call 103 (empatia 9.5) -> 2 calls
            self.assertEqual(res.summary.total_calls, 2)
            self.assertAlmostEqual(res.summary.avg_evaluacion_global, 8.875, places=2)

    # -------------------------------------------------------------
    # 3. Filtro boolean true: Alarma = Sí
    # -------------------------------------------------------------
    async def test_03_boolean_filter_true(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            filt = [{"key": "alarma", "value": True}]
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant, item_filters=filt
            )
            # Matches Call 101 only
            self.assertEqual(res.summary.total_calls, 1)
            self.assertAlmostEqual(res.summary.avg_evaluacion_global, 8.5, places=2)

    # -------------------------------------------------------------
    # 4. Filtro boolean false: Alarma = No
    # -------------------------------------------------------------
    async def test_04_boolean_filter_false(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            filt = [{"key": "alarma", "value": False}]
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant, item_filters=filt
            )
            # Matches Call 102 and Call 103 -> 2 calls
            self.assertEqual(res.summary.total_calls, 2)
            self.assertAlmostEqual(res.summary.avg_evaluacion_global, 7.875, places=2)

    # -------------------------------------------------------------
    # 5. Dos filtros AND: Empatía >= 8 AND Cierre de Cita = True
    # -------------------------------------------------------------
    async def test_05_two_filters_and(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            filt = [
                {"key": "empatia", "min": 8.0, "max": 10.0},
                {"key": "cierre_cita", "value": True}
            ]
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant, item_filters=filt
            )
            # Call 101 (9.0, True) and Call 103 (9.5, True) pass
            self.assertEqual(res.summary.total_calls, 2)

    # -------------------------------------------------------------
    # 6. Tres filtros AND: Empatía >= 8 AND Alarma = True AND Claridad >= 8
    # -------------------------------------------------------------
    async def test_06_three_filters_and(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            filt = [
                {"key": "empatia", "min": 8.0, "max": 10.0},
                {"key": "alarma", "value": True},
                {"key": "claridad", "min": 8.0, "max": 10.0}
            ]
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant, item_filters=filt
            )
            # Call 101 only passes all 3
            self.assertEqual(res.summary.total_calls, 1)
            self.assertEqual(res.summary.main_typology, "Cita")

    # -------------------------------------------------------------
    # 7. Filtro neutral: min=0.0, max=10.0 -> no altera resultados
    # -------------------------------------------------------------
    async def test_07_neutral_filter_discarded(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            filt = [{"key": "empatia", "min": 0.0, "max": 10.0}]
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant, item_filters=filt
            )
            # Neutral filter discarded -> returns all 4 calls
            self.assertEqual(res.summary.total_calls, 4)

    # -------------------------------------------------------------
    # 8. JSON inválido: HTTP 422
    # -------------------------------------------------------------
    async def test_08_invalid_json(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            with self.assertRaises(HTTPException) as ctx:
                await get_evolution(
                    context=self.context_tenant,
                    service_id=1,
                    item_filters="{invalid_json}",
                    db=db
                )
            self.assertEqual(ctx.exception.status_code, 422)

    # -------------------------------------------------------------
    # 9. min > max: HTTP 422
    # -------------------------------------------------------------
    async def test_09_min_greater_than_max(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            with self.assertRaises(HTTPException) as ctx:
                await get_evolution(
                    context=self.context_tenant,
                    service_id=1,
                    item_filters=json.dumps([{"key": "empatia", "min": 9.0, "max": 5.0}]),
                    db=db
                )
            self.assertEqual(ctx.exception.status_code, 422)

    # -------------------------------------------------------------
    # 10. Más de 3 filtros: HTTP 422
    # -------------------------------------------------------------
    async def test_10_more_than_three_filters(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            four_filters = [
                {"key": "empatia", "min": 8.0, "max": 10.0},
                {"key": "claridad", "min": 8.0, "max": 10.0},
                {"key": "alarma", "value": True},
                {"key": "cierre_cita", "value": True}
            ]
            with self.assertRaises(HTTPException) as ctx:
                await get_evolution(
                    context=self.context_tenant,
                    service_id=1,
                    item_filters=json.dumps(four_filters),
                    db=db
                )
            self.assertEqual(ctx.exception.status_code, 422)

    # -------------------------------------------------------------
    # 11. Alias: criterion_filters
    # -------------------------------------------------------------
    async def test_11_alias_criterion_filters(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await get_evolution(
                context=self.context_tenant,
                service_id=1,
                criterion_filters=json.dumps([{"key": "alarma", "value": True}]),
                db=db
            )
            self.assertEqual(res.summary.total_calls, 1)

    # -------------------------------------------------------------
    # 12. Alias: score_filters
    # -------------------------------------------------------------
    async def test_12_alias_score_filters(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await get_evolution(
                context=self.context_tenant,
                service_id=1,
                score_filters=json.dumps([{"key": "alarma", "value": True}]),
                db=db
            )
            self.assertEqual(res.summary.total_calls, 1)

    # -------------------------------------------------------------
    # 13. Alias: item_score_filters
    # -------------------------------------------------------------
    async def test_13_alias_item_score_filters(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await get_evolution(
                context=self.context_tenant,
                service_id=1,
                item_score_filters=json.dumps([{"key": "alarma", "value": True}]),
                db=db
            )
            self.assertEqual(res.summary.total_calls, 1)

    # -------------------------------------------------------------
    # 14. Filtro afecta total_calls
    # -------------------------------------------------------------
    async def test_14_filter_affects_total_calls(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Empatia between 5.0 and 7.0 matches Call 102 only
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant,
                item_filters=[{"key": "empatia", "min": 5.0, "max": 7.0}]
            )
            self.assertEqual(res.summary.total_calls, 1)

    # -------------------------------------------------------------
    # 15. Filtro afecta series
    # -------------------------------------------------------------
    async def test_15_filter_affects_series(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant,
                item_filters=[{"key": "alarma", "value": True}]
            )
            self.assertEqual(len(res.series), 1)
            self.assertEqual(res.series[0].total_calls, 1)
            self.assertAlmostEqual(res.series[0].avg_evaluacion_global, 8.5, places=2)

    # -------------------------------------------------------------
    # 16. Filtro afecta by_typology
    # -------------------------------------------------------------
    async def test_16_filter_affects_by_typology(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant,
                item_filters=[{"key": "alarma", "value": True}]
            )
            # Call 101 has typology "cita".
            cita_item = next((t for t in res.by_typology if t.typology_key == "cita"), None)
            otros_item = next((t for t in res.by_typology if t.typology_key == "otros"), None)
            self.assertIsNotNone(cita_item)
            self.assertEqual(cita_item.total_calls, 1)
            self.assertIsNotNone(otros_item)
            self.assertEqual(otros_item.total_calls, 0)

    # -------------------------------------------------------------
    # 17. Filtro afecta by_agent
    # -------------------------------------------------------------
    async def test_17_filter_affects_by_agent(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant,
                item_filters=[{"key": "alarma", "value": True}]
            )
            # Only Bryan Herrera has a matching call
            self.assertEqual(len(res.by_agent), 1)
            self.assertEqual(res.by_agent[0].agent_name, "Bryan Herrera")
            self.assertEqual(res.by_agent[0].total_calls, 1)

    # -------------------------------------------------------------
    # 18. Filtro afecta criteria_ranking
    # -------------------------------------------------------------
    async def test_18_filter_affects_criteria_ranking(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant,
                item_filters=[{"key": "empatia", "min": 9.2, "max": 10.0}]
            )
            # Matches Call 103 only (empatia 9.5, claridad 9.0)
            self.assertEqual(res.summary.total_calls, 1)
            empatia_rank = next((c for c in res.criteria_ranking if c.criterion_key == "empatia"), None)
            claridad_rank = next((c for c in res.criteria_ranking if c.criterion_key == "claridad"), None)
            self.assertIsNotNone(empatia_rank)
            self.assertAlmostEqual(empatia_rank.avg_value, 9.5, places=2)
            self.assertIsNotNone(claridad_rank)
            self.assertAlmostEqual(claridad_rank.avg_value, 9.0, places=2)

    # -------------------------------------------------------------
    # 19. Llamada sin criterion requerido queda excluida
    # -------------------------------------------------------------
    async def test_19_missing_criterion_excluded(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Query for a criterion that does not exist in any call
            res = await ServiceEvolutionService.get_evolution(
                db, service_id=1, context=self.context_tenant,
                item_filters=[{"key": "non_existent_criterion", "min": 1.0, "max": 10.0}]
            )
            self.assertEqual(res.summary.total_calls, 0)
            self.assertEqual(len(res.series), 0)
            self.assertEqual(len(res.by_agent), 0)

    # -------------------------------------------------------------
    # 20. Multiempresa/service scoping intacto
    # -------------------------------------------------------------
    async def test_20_multitenancy_scoping_intact(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Tenant 1 cannot access service 2 (Company 2)
            with self.assertRaises(HTTPException) as ctx:
                await get_evolution(
                    context=self.context_tenant,
                    service_id=2,
                    item_filters=json.dumps([{"key": "empatia", "min": 8.0, "max": 10.0}]),
                    db=db
                )
            self.assertEqual(ctx.exception.status_code, 403)

            # Super admin can query service 2
            res_super = await get_evolution(
                context=self.context_super_admin,
                service_id=2,
                item_filters=json.dumps([{"key": "empatia", "min": 8.0, "max": 10.0}]),
                db=db
            )
            self.assertEqual(res_super.summary.total_calls, 1)


if __name__ == "__main__":
    unittest.main()
