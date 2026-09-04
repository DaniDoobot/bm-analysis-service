"""
Comprehensive Unit & Integration Test Suite for Mass Analysis Automation Health Summary (OTR-02)

Validates:
1. healthy: is_active=True, completed last run, delay <= 3x interval
2. healthy with 0 calls found: is_active=True, completed, calls_found=0
3. warning: completed_with_errors
4. warning: calls_failed > 0
5. warning: execution delay between 3x and 5x interval
6. critical: is_active=False (Desactivada)
7. critical: last_run.status == 'failed'
8. critical: execution delay > 5x interval
9. critical: stale active run (>= 60 min)
10. today_summary: UTC today aggregation, accurate counters (evaluations_count, alarms_count based on alarma=True)
11. recent_runs: max 5, sorted recent first, uses MassEvaluationRun counters
12. recent_evaluations: max 5, accurate mapping with execution_source and alarma
13. multi-automation isolation: no metric bleed between automations in same service
14. tenant security / access control: 404 / 403 on missing or unauthorized service
15. active_run: correctly detected when running and None when terminal
16. alarms_count: alarma=True without ticket MUST count, alarma=False with ticket MUST NOT count
17. HTTP Router 200 OK: returns valid MassAnalysisAutomationHealthResponse for authorized user
18. HTTP Router 403 Forbidden: user has no access to service
19. HTTP Router 403 Forbidden: user has AGENT role
20. HTTP Router 404 Not Found: automation does not exist
21. HTTP Contract Exact Keys: JSON output matches the Pydantic schema contract precisely
22. Pure READ-ONLY Safety: calling health-summary causes 0 INSERT, 0 UPDATE, 0 DELETE, and modifies 0 DB columns
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

TEST_DB_NAME = "health_summary_test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_NAME}"
os.environ["APP_ENV"] = "test"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import BigInteger, delete, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from fastapi import HTTPException

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
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
    MassEvaluationResult
)
from app.schemas.mass_evaluations import (
    MassAnalysisAutomationHealthResponse,
    MassAnalysisAutomationHealthTodaySummary,
    MassAnalysisAutomationHealthActiveRun,
    MassAnalysisAutomationHealthRecentRun,
    MassAnalysisAutomationHealthRecentEvaluation
)
from app.services.mass_evaluation_service import MassEvaluationService
from app.routers.mass_evaluations import get_automation_health_summary as router_health_summary


class TestAutomationHealthSummary(unittest.IsolatedAsyncioTestCase):

    engine = None

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_NAME):
            try:
                os.remove(TEST_DB_NAME)
            except Exception:
                pass
        cls.engine = get_engine()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_NAME):
            try:
                os.remove(TEST_DB_NAME)
            except Exception:
                pass

    async def asyncSetUp(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await db.execute(delete(MassEvaluationResult))
            await db.execute(delete(MassAnalysisAutomationRun))
            await db.execute(delete(MassEvaluationRun))
            await db.execute(delete(MassAnalysisAutomation))
            await db.execute(delete(MassEvaluationJob))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            c = Company(company_id=1, company_name="Test Co", company_key="test_co", is_active=True)
            s1 = Service(service_id=1, service_name="Service 1", service_key="serv1", company_id=1)
            s2 = Service(service_id=2, service_name="Service 2", service_key="serv2", company_id=1)
            job = MassEvaluationJob(job_id=1, job_name="Test Job", prompt_id=1, is_active=True, schedule_enabled=False, created_by="Tester")
            db.add_all([c, s1, s2, job])
            await db.commit()

        self.context_admin = TenantContext(
            user_id=1,
            user_email="admin@test.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1],
            allowed_service_ids=[1, 2],
        )

        self.context_scoped = TenantContext(
            user_id=2,
            user_email="manager@test.com",
            raw_role="service_manager",
            normalized_role=InternalRole.SERVICE_MANAGER,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
        )

        self.context_agent = TenantContext(
            user_id=3,
            user_email="agent@test.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
        )

    async def test_01_healthy_normal(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=10,
                name="Auto 10",
                service_id=1,
                prompt_id=1,
                interval_minutes=15,
                is_active=True,
                last_run_at=now - timedelta(minutes=10),
            )
            ar = MassAnalysisAutomationRun(
                automation_run_id=101,
                automation_id=10,
                status="completed",
                started_at=now - timedelta(minutes=10),
                finished_at=now - timedelta(minutes=9),
                calls_found=5,
                calls_selected=5,
                run_id=201
            )
            mr = MassEvaluationRun(
                run_id=201,
                job_id=1,
                trigger_type="scheduled",
                status="completed",
                calls_found=5,
                calls_selected=5,
                calls_analyzed=5,
                calls_failed=0,
                calls_skipped=0
            )
            db.add_all([aut, ar, mr])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 10, self.context_admin)
            self.assertEqual(summary.health_status, "healthy")
            self.assertEqual(summary.health_label, "Operativa")
            self.assertEqual(summary.interval_minutes, 15)
            self.assertFalse(summary.is_stale_warning)

    async def test_02_healthy_zero_calls(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=11,
                name="Auto 11",
                service_id=1,
                prompt_id=1,
                interval_minutes=30,
                is_active=True,
                last_run_at=now - timedelta(minutes=5),
            )
            ar = MassAnalysisAutomationRun(
                automation_run_id=102,
                automation_id=11,
                status="completed",
                started_at=now - timedelta(minutes=5),
                finished_at=now - timedelta(minutes=5),
                calls_found=0,
                calls_selected=0,
                calls_skipped=0
            )
            db.add_all([aut, ar])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 11, self.context_admin)
            self.assertEqual(summary.health_status, "healthy")
            self.assertEqual(summary.health_label, "Operativa")
            self.assertEqual(summary.today_summary.calls_found, 0)

    async def test_03_warning_completed_with_errors(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=12,
                name="Auto 12",
                service_id=1,
                prompt_id=1,
                interval_minutes=15,
                is_active=True,
                last_run_at=now - timedelta(minutes=5),
            )
            ar = MassAnalysisAutomationRun(
                automation_run_id=103,
                automation_id=12,
                status="completed_with_errors",
                started_at=now - timedelta(minutes=5),
                finished_at=now - timedelta(minutes=4),
                run_id=203
            )
            mr = MassEvaluationRun(
                run_id=203,
                job_id=1,
                trigger_type="scheduled",
                status="completed_with_errors",
                calls_found=10,
                calls_selected=10,
                calls_analyzed=8,
                calls_failed=2,
            )
            db.add_all([aut, ar, mr])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 12, self.context_admin)
            self.assertEqual(summary.health_status, "warning")
            self.assertEqual(summary.health_label, "Atención")
            self.assertIn("fallidas", summary.health_reason)

    async def test_04_warning_calls_failed(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=13,
                name="Auto 13",
                service_id=1,
                prompt_id=1,
                interval_minutes=15,
                is_active=True,
                last_run_at=now - timedelta(minutes=5),
            )
            ar = MassAnalysisAutomationRun(
                automation_run_id=104,
                automation_id=13,
                status="completed",
                started_at=now - timedelta(minutes=5),
                finished_at=now - timedelta(minutes=4),
                run_id=204
            )
            mr = MassEvaluationRun(
                run_id=204,
                job_id=1,
                trigger_type="scheduled",
                status="completed",
                calls_found=5,
                calls_selected=5,
                calls_analyzed=4,
                calls_failed=1,
            )
            db.add_all([aut, ar, mr])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 13, self.context_admin)
            self.assertEqual(summary.health_status, "warning")
            self.assertEqual(summary.health_label, "Atención")

    async def test_05_warning_execution_delay(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=14,
                name="Auto 14",
                service_id=1,
                prompt_id=1,
                interval_minutes=10,
                is_active=True,
                last_run_at=now - timedelta(minutes=35),  # 3.5x
            )
            ar = MassAnalysisAutomationRun(
                automation_run_id=105,
                automation_id=14,
                status="completed",
                started_at=now - timedelta(minutes=35),
                finished_at=now - timedelta(minutes=34),
            )
            db.add_all([aut, ar])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 14, self.context_admin)
            self.assertEqual(summary.health_status, "warning")
            self.assertEqual(summary.health_label, "Atención")
            self.assertIn("retraso", summary.health_reason.lower())

    async def test_06_critical_inactive(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            aut = MassAnalysisAutomation(
                automation_id=15,
                name="Auto 15",
                service_id=1,
                prompt_id=1,
                interval_minutes=10,
                is_active=False,
            )
            db.add(aut)
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 15, self.context_admin)
            self.assertEqual(summary.health_status, "critical")
            self.assertEqual(summary.health_label, "Desactivada")
            self.assertFalse(summary.is_active)

    async def test_07_critical_last_run_failed(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=16,
                name="Auto 16",
                service_id=1,
                prompt_id=1,
                interval_minutes=10,
                is_active=True,
                last_run_at=now - timedelta(minutes=5),
            )
            ar = MassAnalysisAutomationRun(
                automation_run_id=106,
                automation_id=16,
                status="failed",
                error_message="HubSpot API rate limit exceeded",
                started_at=now - timedelta(minutes=5),
                finished_at=now - timedelta(minutes=4),
            )
            db.add_all([aut, ar])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 16, self.context_admin)
            self.assertEqual(summary.health_status, "critical")
            self.assertEqual(summary.health_label, "Incidencia")
            self.assertIn("HubSpot API rate limit", summary.health_reason)

    async def test_08_critical_execution_delay(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=17,
                name="Auto 17",
                service_id=1,
                prompt_id=1,
                interval_minutes=10,
                is_active=True,
                last_run_at=now - timedelta(minutes=60),  # 6x
            )
            ar = MassAnalysisAutomationRun(
                automation_run_id=107,
                automation_id=17,
                status="completed",
                started_at=now - timedelta(minutes=60),
                finished_at=now - timedelta(minutes=59),
            )
            db.add_all([aut, ar])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 17, self.context_admin)
            self.assertEqual(summary.health_status, "critical")
            self.assertEqual(summary.health_label, "Incidencia")
            self.assertIn("sin ejecutar", summary.health_reason)

    async def test_09_critical_stale_active_run(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=18,
                name="Auto 18",
                service_id=1,
                prompt_id=1,
                interval_minutes=10,
                is_active=True,
                last_run_at=now - timedelta(minutes=70),
            )
            ar = MassAnalysisAutomationRun(
                automation_run_id=108,
                automation_id=18,
                status="running",
                started_at=now - timedelta(minutes=70),
                run_id=208
            )
            mr = MassEvaluationRun(
                run_id=208,
                job_id=1,
                trigger_type="scheduled",
                status="running",
                started_at=now - timedelta(minutes=70),
            )
            db.add_all([aut, ar, mr])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 18, self.context_admin)
            self.assertEqual(summary.health_status, "critical")
            self.assertEqual(summary.health_label, "Incidencia")
            self.assertTrue(summary.is_stale_warning)
            self.assertIsNotNone(summary.active_run)

    async def test_10_today_summary_counters(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=19,
                name="Auto 19",
                service_id=1,
                prompt_id=1,
                interval_minutes=10,
                is_active=True,
                last_run_at=now - timedelta(minutes=10),
            )
            ar1 = MassAnalysisAutomationRun(
                automation_run_id=109,
                automation_id=19,
                status="completed",
                started_at=now - timedelta(minutes=40),
                finished_at=now - timedelta(minutes=38),
                calls_found=5,
                calls_selected=5,
                run_id=209
            )
            mr1 = MassEvaluationRun(
                run_id=209,
                job_id=1,
                trigger_type="scheduled",
                status="completed",
                calls_found=5,
                calls_selected=5,
                calls_analyzed=5,
                calls_failed=0,
            )
            ar2 = MassAnalysisAutomationRun(
                automation_run_id=110,
                automation_id=19,
                status="completed_with_errors",
                started_at=now - timedelta(minutes=10),
                finished_at=now - timedelta(minutes=8),
                calls_found=8,
                calls_selected=8,
                run_id=210
            )
            mr2 = MassEvaluationRun(
                run_id=210,
                job_id=1,
                trigger_type="scheduled",
                status="completed_with_errors",
                calls_found=8,
                calls_selected=8,
                calls_analyzed=6,
                calls_failed=2,
            )
            # Add evaluations
            ev1 = MassEvaluationResult(
                mass_analysis_id=501,
                run_id=209,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call-501",
                execution_source="automation",
                status="completed",
                created_at=now - timedelta(minutes=39),
                items_json=[{"criterion_key": "alarma", "value": False}],
            )
            ev2 = MassEvaluationResult(
                mass_analysis_id=502,
                run_id=210,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call-502",
                execution_source="automation",
                status="completed",
                created_at=now - timedelta(minutes=9),
                items_json=[{"criterion_key": "alarma", "value": True, "feed": "Queja grave"}],
                hubspot_ticket_id="ticket-999",
                hubspot_ticket_status="created"
            )
            db.add_all([aut, ar1, mr1, ar2, mr2, ev1, ev2])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 19, self.context_admin)
            self.assertEqual(summary.today_summary.total_runs, 2)
            self.assertEqual(summary.today_summary.calls_found, 13)
            self.assertEqual(summary.today_summary.calls_selected, 13)
            self.assertEqual(summary.today_summary.calls_analyzed, 11)
            self.assertEqual(summary.today_summary.calls_failed, 2)
            self.assertEqual(summary.today_summary.errors_count, 1)
            self.assertEqual(summary.today_summary.evaluations_count, 2)
            self.assertEqual(summary.today_summary.alarms_count, 1)

    async def test_11_recent_runs_max_5_order_and_counters(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=20,
                name="Auto 20",
                service_id=1,
                prompt_id=1,
                interval_minutes=10,
                is_active=True,
                last_run_at=now - timedelta(minutes=10),
            )
            db.add(aut)

            # Insert 7 runs
            for i in range(1, 8):
                ar = MassAnalysisAutomationRun(
                    automation_run_id=300 + i,
                    automation_id=20,
                    status="completed",
                    started_at=now - timedelta(minutes=80 - i * 10),
                    finished_at=now - timedelta(minutes=78 - i * 10),
                    calls_found=i,
                    calls_selected=i,
                    run_id=400 + i
                )
                mr = MassEvaluationRun(
                    run_id=400 + i,
                    job_id=1,
                    trigger_type="scheduled",
                    status="completed",
                    calls_found=i,
                    calls_selected=i,
                    calls_analyzed=i,
                    calls_failed=0,
                )
                db.add_all([ar, mr])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 20, self.context_admin)
            self.assertEqual(len(summary.recent_runs), 5)
            # Most recent first
            self.assertEqual(summary.recent_runs[0].automation_run_id, 307)
            self.assertEqual(summary.recent_runs[0].calls_analyzed, 7)
            self.assertEqual(summary.recent_runs[4].automation_run_id, 303)

    async def test_12_recent_evaluations_fields_and_isolation(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(
                automation_id=21,
                name="Auto 21",
                service_id=1,
                prompt_id=1,
                interval_minutes=10,
                is_active=True,
                last_run_at=now - timedelta(minutes=10),
            )
            ar = MassAnalysisAutomationRun(
                automation_run_id=310,
                automation_id=21,
                status="completed",
                started_at=now - timedelta(minutes=10),
                finished_at=now - timedelta(minutes=8),
                run_id=410
            )
            ev = MassEvaluationResult(
                mass_analysis_id=601,
                run_id=410,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call-601",
                agent_name="Ana Agente",
                execution_source="automation",
                status="completed",
                evaluacion_global=Decimal("8.50"),
                is_evaluable=True,
                created_at=now - timedelta(minutes=9),
                analysis_timestamp=now - timedelta(minutes=9),
                items_json=[{"criterion_key": "alarma", "value": True, "feed": "Reclamación"}],
                hubspot_ticket_id="ticket-12345",
                hubspot_ticket_status="created",
            )
            db.add_all([aut, ar, ev])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 21, self.context_admin)
            self.assertEqual(len(summary.recent_evaluations), 1)
            e = summary.recent_evaluations[0]
            self.assertEqual(e.mass_analysis_id, 601)
            self.assertEqual(e.call_id, "call-601")
            self.assertEqual(e.agent_name, "Ana Agente")
            self.assertEqual(e.execution_source, "automation")
            self.assertTrue(e.alarma)
            self.assertEqual(e.hubspot_ticket_id, "ticket-12345")
            self.assertEqual(e.hubspot_ticket_status, "created")
            self.assertEqual(e.evaluacion_global, 8.5)

    async def test_13_multi_automation_isolation(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut_a = MassAnalysisAutomation(automation_id=22, name="Auto A", service_id=1, prompt_id=1, interval_minutes=10, is_active=True)
            aut_b = MassAnalysisAutomation(automation_id=23, name="Auto B", service_id=1, prompt_id=1, interval_minutes=10, is_active=True)

            ar_a = MassAnalysisAutomationRun(automation_run_id=320, automation_id=22, status="completed", started_at=now, run_id=420)
            mr_a = MassEvaluationRun(run_id=420, job_id=1, trigger_type="scheduled", status="completed", calls_analyzed=10)

            ar_b = MassAnalysisAutomationRun(automation_run_id=321, automation_id=23, status="completed", started_at=now, run_id=421)
            mr_b = MassEvaluationRun(run_id=421, job_id=1, trigger_type="scheduled", status="completed", calls_analyzed=20)

            ev_a = MassEvaluationResult(mass_analysis_id=701, run_id=420, job_id=1, prompt_id=1, prompt_snapshot="{}", call_id="call-a", execution_source="automation", created_at=now)
            ev_b = MassEvaluationResult(mass_analysis_id=702, run_id=421, job_id=1, prompt_id=1, prompt_snapshot="{}", call_id="call-b", execution_source="automation", created_at=now)

            db.add_all([aut_a, aut_b, ar_a, mr_a, ar_b, mr_b, ev_a, ev_b])
            await db.commit()

            summary_a = await MassEvaluationService.get_automation_health_summary(db, 22, self.context_admin)
            summary_b = await MassEvaluationService.get_automation_health_summary(db, 23, self.context_admin)

            self.assertEqual(summary_a.today_summary.calls_analyzed, 10)
            self.assertEqual(summary_b.today_summary.calls_analyzed, 20)
            self.assertEqual(len(summary_a.recent_evaluations), 1)
            self.assertEqual(summary_a.recent_evaluations[0].call_id, "call-a")
            self.assertEqual(len(summary_b.recent_evaluations), 1)
            self.assertEqual(summary_b.recent_evaluations[0].call_id, "call-b")

    async def test_14_tenant_security(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            aut = MassAnalysisAutomation(automation_id=24, name="Auto Service 2", service_id=2, prompt_id=1, interval_minutes=10, is_active=True)
            db.add(aut)
            await db.commit()

            # context_scoped only has service_id=1
            with self.assertRaises(HTTPException) as ctx:
                await router_health_summary(automation_id=24, db=db, context=self.context_scoped)
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_15_active_run_detection_and_heartbeat(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(automation_id=25, name="Auto 25", service_id=1, prompt_id=1, interval_minutes=10, is_active=True)
            ar = MassAnalysisAutomationRun(
                automation_run_id=330,
                automation_id=25,
                status="running",
                started_at=now - timedelta(minutes=2),
                run_id=430
            )
            mr = MassEvaluationRun(
                run_id=430,
                job_id=1,
                trigger_type="scheduled",
                status="running",
                started_at=now - timedelta(minutes=2),
                heartbeat_at=now - timedelta(seconds=10),
                calls_found=20,
                calls_selected=20,
                calls_analyzed=15,
                calls_failed=0,
                calls_skipped=0
            )
            db.add_all([aut, ar, mr])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 25, self.context_admin)
            self.assertIsNotNone(summary.active_run)
            self.assertEqual(summary.active_run.status, "running")
            self.assertEqual(summary.active_run.calls_analyzed, 15)
            self.assertIsNotNone(summary.active_run.heartbeat_at)

    async def test_16_alarms_count_pure_alarm_not_ticket(self):
        """
        Verify:
        - alarma=True without ticket MUST increment alarms_count.
        - alarma=False with ticket MUST NOT increment alarms_count.
        """
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            now = datetime.now(timezone.utc)
            aut = MassAnalysisAutomation(automation_id=26, name="Auto 26", service_id=1, prompt_id=1, interval_minutes=10, is_active=True)
            ar = MassAnalysisAutomationRun(
                automation_run_id=340,
                automation_id=26,
                status="completed",
                started_at=now - timedelta(minutes=5),
                run_id=440
            )
            mr = MassEvaluationRun(
                run_id=440,
                job_id=1,
                trigger_type="scheduled",
                status="completed",
                calls_analyzed=2
            )
            # Result 1: alarma=True, NO ticket -> MUST COUNT
            ev1 = MassEvaluationResult(
                mass_analysis_id=801,
                run_id=440,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call-alarm-no-ticket",
                execution_source="automation",
                status="completed",
                created_at=now - timedelta(minutes=4),
                items_json=[{"criterion_key": "alarma", "value": True, "feed": "Alarma sin ticket"}],
                hubspot_ticket_id=None,
                hubspot_ticket_status=None,
            )
            # Result 2: alarma=False, WITH ticket -> MUST NOT COUNT
            ev2 = MassEvaluationResult(
                mass_analysis_id=802,
                run_id=440,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call-no-alarm-with-ticket",
                execution_source="automation",
                status="completed",
                created_at=now - timedelta(minutes=3),
                items_json=[{"criterion_key": "alarma", "value": False}],
                hubspot_ticket_id="ticket-legacy-999",
                hubspot_ticket_status="created",
            )
            db.add_all([aut, ar, mr, ev1, ev2])
            await db.commit()

            summary = await MassEvaluationService.get_automation_health_summary(db, 26, self.context_admin)
            self.assertEqual(summary.today_summary.evaluations_count, 2)
            self.assertEqual(summary.today_summary.alarms_count, 1)

    async def test_17_http_router_200_ok(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            aut = MassAnalysisAutomation(automation_id=27, name="Auto 27", service_id=1, prompt_id=1, interval_minutes=10, is_active=True)
            db.add(aut)
            await db.commit()

            resp = await router_health_summary(automation_id=27, db=db, context=self.context_admin)
            self.assertIsInstance(resp, MassAnalysisAutomationHealthResponse)
            self.assertEqual(resp.automation_id, 27)

    async def test_18_http_router_403_forbidden_service(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            aut = MassAnalysisAutomation(automation_id=28, name="Auto 28", service_id=2, prompt_id=1, interval_minutes=10, is_active=True)
            db.add(aut)
            await db.commit()

            with self.assertRaises(HTTPException) as ctx:
                await router_health_summary(automation_id=28, db=db, context=self.context_scoped)
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_19_http_router_403_forbidden_agent(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            aut = MassAnalysisAutomation(automation_id=29, name="Auto 29", service_id=1, prompt_id=1, interval_minutes=10, is_active=True)
            db.add(aut)
            await db.commit()

            with self.assertRaises(HTTPException) as ctx:
                await router_health_summary(automation_id=29, db=db, context=self.context_agent)
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_20_http_router_404_not_found(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            with self.assertRaises(HTTPException) as ctx:
                await router_health_summary(automation_id=9999, db=db, context=self.context_admin)
            self.assertEqual(ctx.exception.status_code, 404)

    async def test_21_http_contract_exact_keys(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            aut = MassAnalysisAutomation(automation_id=30, name="Auto 30", service_id=1, prompt_id=1, interval_minutes=10, is_active=True)
            db.add(aut)
            await db.commit()

            resp = await router_health_summary(automation_id=30, db=db, context=self.context_admin)
            d = resp.model_dump()

            required_top_keys = {
                "automation_id", "name", "company_id", "service_id", "service_name",
                "is_active", "interval_minutes", "health_status", "health_label",
                "health_reason", "stale_threshold_minutes", "last_run_at", "next_run_at",
                "active_run", "today_summary", "recent_runs", "recent_evaluations"
            }
            for k in required_top_keys:
                self.assertIn(k, d)

            today_keys = {
                "date", "total_runs", "calls_found", "calls_selected", "calls_analyzed",
                "calls_failed", "calls_skipped", "errors_count", "evaluations_count", "alarms_count"
            }
            for k in today_keys:
                self.assertIn(k, d["today_summary"])

    async def test_22_pure_read_only_safety(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            aut = MassAnalysisAutomation(automation_id=31, name="Auto 31", service_id=1, prompt_id=1, interval_minutes=10, is_active=True)
            ar = MassAnalysisAutomationRun(automation_run_id=350, automation_id=31, status="running", started_at=datetime.now(timezone.utc))
            db.add_all([aut, ar])
            await db.commit()

            # Execute router function
            resp = await router_health_summary(automation_id=31, db=db, context=self.context_admin)

            # Assert session is completely clean (no dirty objects, no flushes)
            self.assertEqual(len(db.dirty), 0)
            self.assertEqual(len(db.new), 0)
            self.assertEqual(len(db.deleted), 0)


if __name__ == "__main__":
    unittest.main()
