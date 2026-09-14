"""
Comprehensive unit tests for Bloque 2: Central Fail-Closed Guard for Demo Tenants.

Tests cover:
1. Core guard logic and resolution (Section 14: A through I)
2. HubSpot write & read containment (Section 15: HTTP call count = 0 for demo)
3. Scheduler containment (Section 16: automation, mass jobs, training)
4. EXPAC containment preserved alongside Demo guard (Section 17)
5. Performance / N+1 avoidance (Section 18)
"""
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# SQLite compatibility shims for PostgreSQL JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.config import get_settings
from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
)
from app.models.users import User
from app.models.teams import Team
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingRun,
)
from app.core.side_effects import (
    is_external_side_effect_allowed,
    resolve_company_demo_status,
    IntegrationType,
)
from app.services.hubspot_service import HubSpotService
from app.services.mass_evaluation_service import MassEvaluationService
from app.services.personalized_training_service import PersonalizedTrainingService


class TestDemoTenantIsolation(unittest.IsolatedAsyncioTestCase):
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

        self.settings = get_settings()
        self.settings.hubspot_access_token = "test_token"
        self.settings.hubspot_portal_id = "12345"
        self.settings.hubspot_ticket_pipeline = "test_pipeline"
        self.settings.hubspot_ticket_stage = "test_stage"
        self.settings.hubspot_tipo_de_rem = "test_rem"
        self.settings.hubspot_alarm_tickets_enabled = True
        self.settings.hubspot_alarm_company_id = 1

        # Seed test entities:
        # Real Company (id=1, is_demo=False)
        # Demo Company (id=99, is_demo=True)
        # Real Service Front (id=1, company_id=1)
        # Real Service EXPAC (id=2, company_id=1)
        # Demo Service (id=10, company_id=99)
        async with self.async_session() as db:
            c_real = Company(
                company_id=1,
                company_name="Boston Medical",
                company_key="boston-medical",
                is_active=True,
                is_demo=False
            )
            c_demo = Company(
                company_id=99,
                company_name="Empresa Demo",
                company_key="empresa-demo",
                is_active=True,
                is_demo=True
            )
            s_front = Service(
                service_id=1,
                company_id=1,
                service_name="Front",
                service_key="front",
                is_active=True
            )
            s_expac = Service(
                service_id=2,
                company_id=1,
                service_name="EXPAC",
                service_key="expac",
                is_active=True
            )
            s_demo = Service(
                service_id=10,
                company_id=99,
                service_name="Demo Service",
                service_key="demo-service",
                is_active=True
            )
            db.add_all([c_real, c_demo, s_front, s_expac, s_demo])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    # =========================================================================
    # 1. CORE GUARD LOGIC & RESOLUTION TESTS (Section 14: A through I)
    # =========================================================================
    async def test_section_14_a_real_company_generic_allowed(self):
        """A) Real company + generic safe external rule -> True (existing behavior)."""
        async with self.async_session() as db:
            allowed = await is_external_side_effect_allowed(
                db, company_id=1, integration="generic"
            )
            self.assertTrue(allowed)

    async def test_section_14_b_demo_company_hubspot_denied(self):
        """B) Demo company + HubSpot write/read -> False."""
        async with self.async_session() as db:
            write_allowed = await is_external_side_effect_allowed(
                db, company_id=99, integration="hubspot_write"
            )
            read_allowed = await is_external_side_effect_allowed(
                db, company_id=99, integration="hubspot_read"
            )
            self.assertFalse(write_allowed)
            self.assertFalse(read_allowed)

    async def test_section_14_c_demo_company_automation_denied(self):
        """C) Demo company + automation -> False."""
        async with self.async_session() as db:
            allowed = await is_external_side_effect_allowed(
                db, company_id=99, integration="automation"
            )
            self.assertFalse(allowed)

    async def test_section_14_d_demo_company_mass_scheduler_denied(self):
        """D) Demo company + mass_scheduler -> False."""
        async with self.async_session() as db:
            allowed = await is_external_side_effect_allowed(
                db, company_id=99, integration="mass_scheduler"
            )
            self.assertFalse(allowed)

    async def test_section_14_e_demo_company_training_scheduler_denied(self):
        """E) Demo company + training_scheduler -> False."""
        async with self.async_session() as db:
            allowed = await is_external_side_effect_allowed(
                db, company_id=99, integration="training_scheduler"
            )
            self.assertFalse(allowed)

    async def test_section_14_f_demo_company_trainer_interactive_allowed(self):
        """F) Demo company + trainer_interactive -> True (explicit controlled exception)."""
        async with self.async_session() as db:
            allowed = await is_external_side_effect_allowed(
                db, company_id=99, integration="trainer_interactive"
            )
            self.assertTrue(allowed)

            # Twilio interactive is also explicitly allowed
            twilio_allowed = await is_external_side_effect_allowed(
                db, company_id=99, integration="twilio_interactive"
            )
            self.assertTrue(twilio_allowed)

            # Generic has no boolean override parameter: denied fail-closed
            generic_denied = await is_external_side_effect_allowed(
                db, company_id=99, integration="generic"
            )
            self.assertFalse(generic_denied)

    async def test_section_14_g_nonexistent_company_fail_closed(self):
        """G) Nonexistent company_id -> False (fail-closed)."""
        async with self.async_session() as db:
            allowed = await is_external_side_effect_allowed(
                db, company_id=99999, integration="generic"
            )
            self.assertFalse(allowed)

    async def test_section_14_h_nonexistent_service_fail_closed(self):
        """H) Nonexistent service_id -> False (fail-closed)."""
        async with self.async_session() as db:
            allowed = await is_external_side_effect_allowed(
                db, service_id=88888, integration="generic"
            )
            self.assertFalse(allowed)

    async def test_section_14_i_service_without_company_fail_closed(self):
        """I) Service without resolvable company -> False (fail-closed)."""
        async with self.async_session() as db:
            s_orphan = Service(
                service_id=55,
                company_id=None,
                service_name="Orphan Service",
                service_key="orphan",
                is_active=True
            )
            db.add(s_orphan)
            await db.commit()

            allowed = await is_external_side_effect_allowed(
                db, service_id=55, integration="generic"
            )
            self.assertFalse(allowed)

    async def test_fail_closed_without_db_or_cached_status(self):
        """No DB session and no pre-resolved is_demo -> False (fail closed)."""
        allowed = await is_external_side_effect_allowed(
            None, company_id=1, integration="generic"
        )
        self.assertFalse(allowed)

    # =========================================================================
    # 2. HUBSPOT WRITE & READ CONTAINMENT (Section 15 & 17)
    # =========================================================================
    async def test_section_15_hubspot_alarm_ticket_demo_zero_http_calls(self):
        """Demo company: _process_alarm_hubspot_ticket must NOT make any HTTP calls."""
        async with self.async_session() as db:
            # Create a completed mass evaluation result for demo company
            res = MassEvaluationResult(
                mass_analysis_id=101,
                run_id=1,
                job_id=1,
                company_id=99,  # Demo company
                service_id=10,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="demo_call_001",
                status="completed",
                is_evaluable=True,
                items_json=[{"criterion_key": "alarma", "value": True, "boolean_value": True}],
                hubspot_ticket_status="pending"
            )
            db.add(res)
            await db.commit()

            with patch.object(HubSpotService, "create_ticket", new=AsyncMock()) as mock_create,                  patch.object(HubSpotService, "find_alarm_ticket", new=AsyncMock(return_value=None)) as mock_find:
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=db,
                    mass_analysis_id=101,
                    execution_source="automation",
                    company_id=99,
                    service_name="Demo Service",
                    agent_name="Demo Agent",
                    call_id="demo_call_001",
                    call_timestamp=datetime.now(timezone.utc),
                    typology_name=None,
                    direction="INBOUND",
                    call_duration_seconds=120,
                    evaluacion_global=Decimal("5.0"),
                    alarma_feed="Alarma detectada",
                    service_id=10
                )

                # HTTP CLIENT CALL COUNT = 0
                self.assertEqual(mock_create.call_count, 0)
                self.assertEqual(mock_find.call_count, 0)

    async def test_section_17_expac_containment_preserved_alongside_demo(self):
        """
        Verify two-tier containment:
        - Real Front (company 1, service 1) -> permitted (create_ticket called).
        - Real EXPAC (company 1, service 2) -> blocked by is_hubspot_side_effect_allowed (call_count=0).
        - Demo Front (company 99, service 1) -> blocked by is_demo (call_count=0).
        - Demo EXPAC (company 99, service 2) -> blocked by both is_demo and containment (call_count=0).
        """
        async with self.async_session() as db:
            # 1. Real Front: allowed
            self.assertTrue(
                await is_external_side_effect_allowed(
                    db, company_id=1, service_id=1, integration="hubspot_write"
                )
            )

            # 2. Real EXPAC: blocked by HubSpot containment
            self.assertFalse(
                await is_external_side_effect_allowed(
                    db, company_id=1, service_id=2, integration="hubspot_write"
                )
            )

            # 3. Demo Front: blocked by demo flag
            self.assertFalse(
                await is_external_side_effect_allowed(
                    db, company_id=99, service_id=1, integration="hubspot_write"
                )
            )

            # 4. Demo EXPAC: blocked
            self.assertFalse(
                await is_external_side_effect_allowed(
                    db, company_id=99, service_id=2, integration="hubspot_write"
                )
            )

    async def test_section_15_hubspot_preview_search_demo_zero_http_calls(self):
        """Demo job: search_candidate_calls preview must NOT make any HubSpot search calls."""
        async with self.async_session() as db:
            job = MassEvaluationJob(
                job_id=201,
                company_id=99,  # Demo company
                service_id=10,
                job_name="Demo Preview Job",
                prompt_id=1,
                job_mode="standard",
                execution_source="on_demand",
                direction="all",
                only_with_recording=True,
                max_calls=50,
                is_active=True
            )
            db.add(job)
            await db.commit()

            with patch.object(HubSpotService, "search_calls_for_mass_evaluation", new=AsyncMock()) as mock_search:
                result = await MassEvaluationService.search_calls_for_job_preview(db, job_id=201)

                # HTTP CLIENT CALL COUNT = 0
                self.assertEqual(mock_search.call_count, 0)
                self.assertEqual(result["calls_found"], 0)
                self.assertEqual(result["calls"], [])

    # =========================================================================
    # 3. SCHEDULER CONTAINMENT TESTS (Section 16)
    # =========================================================================
    async def test_section_16_automation_scheduler_skips_demo(self):
        """Automation belonging to demo service is cleanly skipped and not launched."""
        async with self.async_session() as db:
            aut = MassAnalysisAutomation(
                automation_id=301,
                name="Demo Automation",
                service_id=10,  # Demo service
                prompt_id=1,
                is_active=True,
                interval_minutes=30,
                lookback_minutes=30,
                delay_minutes=5,
            )
            db.add(aut)
            await db.commit()

            with patch.object(MassEvaluationService, "run_job", new=AsyncMock()) as mock_run_job:
                run_res = await MassEvaluationService.run_automation_run(db, aut, trigger_type="scheduled")

                self.assertEqual(run_res.status, "skipped")
                self.assertIn("demo_tenant", run_res.error_message)
                # Underlying mass evaluation job runner NOT invoked
                self.assertEqual(mock_run_job.call_count, 0)

    async def test_section_16_mass_job_scheduler_skips_demo(self):
        """Due job belonging to demo company is skipped by run_due_jobs."""
        async with self.async_session() as db:
            due_time = datetime.now(timezone.utc)
            job = MassEvaluationJob(
                job_id=401,
                company_id=99,  # Demo company
                service_id=10,
                job_name="Demo Due Job",
                prompt_id=1,
                is_active=True,
                schedule_enabled=True,
                schedule_type="daily",
                next_run_at=due_time,
            )
            db.add(job)
            await db.commit()

            with patch.object(MassEvaluationService, "run_job", new=AsyncMock()) as mock_run_job:
                stats = await MassEvaluationService.run_due_jobs(db)

                # Due count was 1, but launched count is 0
                self.assertEqual(stats["due_jobs_count"], 1)
                self.assertEqual(stats["launched_jobs_count"], 0)
                self.assertEqual(mock_run_job.call_count, 0)

    async def test_section_16_training_scheduler_excludes_demo_agents(self):
        """Scheduled training pass excludes agents belonging to demo company."""
        async with self.async_session() as db:
            # Agent for Real Company
            a_real = TrainingAgentSetting(
                setting_id=501,
                company_id=1,
                hubspot_owner_id="agent_real_1",
                agent_name="Real Agent",
                agent_initials="RA",
                is_enabled=True,
            )
            # Agent for Demo Company (accidentally set is_enabled=True)
            a_demo = TrainingAgentSetting(
                setting_id=502,
                company_id=99,
                hubspot_owner_id="agent_demo_1",
                agent_name="Demo Agent",
                agent_initials="DA",
                is_enabled=True,
            )
            db.add_all([a_real, a_demo])
            await db.commit()

            with patch.object(
                PersonalizedTrainingService,
                "generate_report_for_agent",
                new=AsyncMock(return_value=MagicMock(status="completed", error_message=None))
            ) as mock_gen_rep:
                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    triggered_by="scheduler"
                )

                # Exactly 1 agent processed (Real Agent), Demo Agent excluded!
                self.assertEqual(run.agents_total, 1)
                self.assertEqual(mock_gen_rep.call_count, 1)
                called_owner_ids = [call.kwargs.get("hubspot_owner_id") for call in mock_gen_rep.call_args_list]
                self.assertIn("agent_real_1", called_owner_ids)
                self.assertNotIn("agent_demo_1", called_owner_ids)

    # =========================================================================
    # 4. PERFORMANCE / N+1 PREVENTION (Section 18)
    # =========================================================================
    async def test_section_18_no_n_plus_one_when_is_demo_provided(self):
        """Passing pre-resolved is_demo executes 0 queries against the database session."""
        # Create a mock session that would raise if execute() was called
        mock_db = MagicMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(side_effect=AssertionError("DB query executed when is_demo was provided!"))

        # Pre-resolved is_demo=False
        res_false = await is_external_side_effect_allowed(
            mock_db,
            company_id=1,
            integration="generic",
            is_demo=False
        )
        self.assertTrue(res_false)
        self.assertEqual(mock_db.execute.call_count, 0)

        # Pre-resolved is_demo=True
        res_true = await is_external_side_effect_allowed(
            mock_db,
            company_id=99,
            integration="automation",
            is_demo=True
        )
        self.assertFalse(res_true)
        self.assertEqual(mock_db.execute.call_count, 0)

    # =========================================================================
    # 5. FRONTIER DEFENSE & DEEP CONTAINMENT (Sections 20, 21, 22)
    # =========================================================================
    async def test_section_20_hubspot_service_frontier_defense(self):
        """
        HubSpotService frontier defense:
        When is_demo is True (either instance level or call level),
        create_ticket, find_alarm_ticket, get_call, and search_calls_for_mass_evaluation
        execute ZERO HTTP requests and return safe empty results fail-closed.
        """
        import httpx
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock()) as mock_post, \
             patch.object(httpx.AsyncClient, "get", new=AsyncMock()) as mock_get:

            # Instance-level is_demo=True
            demo_hs = HubSpotService(is_demo=True)
            res_ticket = await demo_hs.create_ticket({"subject": "test"}, contact_id="123")
            self.assertEqual(res_ticket, {})
            self.assertEqual(mock_post.call_count, 0)

            res_alarm = await demo_hs.find_alarm_ticket(mass_analysis_id=999, call_id="c123")
            self.assertIsNone(res_alarm)
            self.assertEqual(mock_post.call_count, 0)

            res_call = await demo_hs.get_call("call_999")
            self.assertEqual(res_call, {})
            self.assertEqual(mock_get.call_count, 0)

            res_search = await demo_hs.search_calls_for_mass_evaluation({"direction": "INBOUND"})
            self.assertEqual(res_search, [])
            self.assertEqual(mock_post.call_count, 0)

            # Call-level is_demo=True override
            standard_hs = HubSpotService(is_demo=False)
            res_ticket_override = await standard_hs.create_ticket({"subject": "test"}, is_demo=True)
            self.assertEqual(res_ticket_override, {})
            self.assertEqual(mock_post.call_count, 0)

            res_call_override = await standard_hs.get_call("call_999", is_demo=True)
            self.assertEqual(res_call_override, {})
            self.assertEqual(mock_get.call_count, 0)

            res_alarm_override = await standard_hs.find_alarm_ticket(mass_analysis_id=999, is_demo=True)
            self.assertIsNone(res_alarm_override)
            self.assertEqual(mock_post.call_count, 0)

            res_search_override = await standard_hs.search_calls_for_mass_evaluation({}, is_demo=True)
            self.assertEqual(res_search_override, [])
            self.assertEqual(mock_post.call_count, 0)

    async def test_section_21_hubspot_preview_manual_call_ids_demo_zero_http_calls(self):
        """
        Demo job with selection_mode='manual_call_ids':
        search_calls_for_job_preview must NOT execute get_call or make any HubSpot requests.
        """
        async with self.async_session() as db:
            job = MassEvaluationJob(
                job_id=202,
                company_id=99,  # Demo company
                service_id=10,
                job_name="Demo Manual Call IDs Job",
                prompt_id=1,
                selection_mode="manual_call_ids",
                call_ids=["hs_call_1", "hs_call_2"],
                execution_source="on_demand",
                is_active=True
            )
            db.add(job)
            await db.commit()

            with patch.object(HubSpotService, "get_call", new=AsyncMock()) as mock_get_call:
                preview = await MassEvaluationService.search_calls_for_job_preview(db, job_id=202)

                self.assertEqual(mock_get_call.call_count, 0)
                self.assertEqual(preview["calls_found"], 0)
                self.assertEqual(preview["calls"], [])

    async def test_section_22_historical_recovery_demo_rejected(self):
        """
        Historical recovery on a demo company job is strictly rejected:
        raises ValueError fail-closed without querying HubSpot.
        """
        async with self.async_session() as db:
            job = MassEvaluationJob(
                job_id=203,
                company_id=99,  # Demo company
                service_id=10,
                job_name="Demo Recovery Job",
                prompt_id=1,
                selection_mode="manual_call_ids",
                call_ids=["hs_call_rec_1"],
                execution_source="on_demand",
                is_active=True
            )
            db.add(job)
            await db.commit()

            with patch.object(HubSpotService, "get_call", new=AsyncMock()) as mock_get_call:
                with self.assertRaises(ValueError) as ctx:
                    await MassEvaluationService.run_historical_recovery(
                        db=db,
                        job_id=203,
                        call_ids=["hs_call_rec_1"],
                        reason="testing isolation"
                    )
                self.assertIn("Historical recovery is not allowed for demo tenants", str(ctx.exception))
                self.assertEqual(mock_get_call.call_count, 0)

    # =========================================================================
    # 6. OBLIGATORY CHECKS A THROUGH I (FAIL-CLOSED & UNKNOWN RESOLUTION)
    # =========================================================================

    async def test_check_a_training_setting_company_demo_scheduler_skip(self):
        """A) training setting with demo company -> scheduler skip"""
        async with self.async_session() as db:
            setting = TrainingAgentSetting(
                setting_id=601,
                company_id=99,  # Demo company (is_demo=True)
                hubspot_owner_id="demo_owner_601",
                agent_name="Demo Agent A",
                agent_initials="DA",
                is_enabled=True,
            )
            db.add(setting)
            await db.commit()

            with patch.object(
                PersonalizedTrainingService,
                "generate_report_for_agent",
                new=AsyncMock()
            ) as mock_gen:
                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    triggered_by="scheduler"
                )
                called_owners = [c.kwargs.get("hubspot_owner_id") for c in mock_gen.call_args_list]
                self.assertNotIn("demo_owner_601", called_owners)

    async def test_check_b_training_setting_company_real_processed(self):
        """B) training setting with real company -> processed / behavior preserved"""
        async with self.async_session() as db:
            setting = TrainingAgentSetting(
                setting_id=602,
                company_id=1,  # Real company (is_demo=False)
                hubspot_owner_id="real_owner_602",
                agent_name="Real Agent B",
                agent_initials="RB",
                is_enabled=True,
            )
            db.add(setting)
            await db.commit()

            with patch.object(
                PersonalizedTrainingService,
                "generate_report_for_agent",
                new=AsyncMock(return_value=MagicMock(status="completed", error_message=None))
            ) as mock_gen:
                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    triggered_by="scheduler"
                )
                called_owners = [c.kwargs.get("hubspot_owner_id") for c in mock_gen.call_args_list]
                self.assertIn("real_owner_602", called_owners)

    async def test_check_c_training_setting_company_null_unresolvable_scheduler_skip(self):
        """C) training setting with company NULL and unresolvable -> scheduler skip (fail closed)"""
        async with self.async_session() as db:
            setting = TrainingAgentSetting(
                setting_id=603,
                company_id=None,  # Null company, no matching User record
                hubspot_owner_id="orphan_owner_603",
                agent_name="Orphan Agent C",
                agent_initials="OC",
                is_enabled=True,
            )
            db.add(setting)
            await db.commit()

            with patch.object(
                PersonalizedTrainingService,
                "generate_report_for_agent",
                new=AsyncMock()
            ) as mock_gen:
                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    triggered_by="scheduler"
                )
                called_owners = [c.kwargs.get("hubspot_owner_id") for c in mock_gen.call_args_list]
                self.assertNotIn("orphan_owner_603", called_owners)

    async def test_check_d_training_setting_company_null_canonical_user_resolved(self):
        """D) training setting with company NULL but owner resolvable to real company -> processed"""
        async with self.async_session() as db:
            # Canonical user has company_id=1 (real, is_demo=False)
            u = User(
                user_id=604,
                username="legacy_agent_d",
                email="legacy_d@test.com",
                password_hash="fake_hash",
                hubspot_owner_id="legacy_owner_604",
                company_id=1,
                is_active=True,
            )
            setting = TrainingAgentSetting(
                setting_id=604,
                company_id=None,  # Setting has None, resolved canonically via User.company_id
                hubspot_owner_id="legacy_owner_604",
                agent_name="Legacy Agent D",
                agent_initials="LD",
                is_enabled=True,
            )
            db.add_all([u, setting])
            await db.commit()

            with patch.object(
                PersonalizedTrainingService,
                "generate_report_for_agent",
                new=AsyncMock(return_value=MagicMock(status="completed", error_message=None))
            ) as mock_gen:
                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    triggered_by="scheduler"
                )
                called_owners = [c.kwargs.get("hubspot_owner_id") for c in mock_gen.call_args_list]
                self.assertIn("legacy_owner_604", called_owners)

    async def test_check_e_hubspot_boundary_is_demo_true_zero_http(self):
        """E) HubSpotService boundary: call with is_demo=True -> 0 HTTP calls"""
        import httpx
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock()) as mock_post, \
             patch.object(httpx.AsyncClient, "get", new=AsyncMock()) as mock_get:
            hs = HubSpotService(is_demo=True)
            res1 = await hs.get_call("call_e")
            res2 = await hs.create_ticket({"subject": "test"})
            res3 = await hs.find_alarm_ticket(1, "call_e")
            res4 = await hs.search_calls_for_mass_evaluation({})
            res5 = await hs.get_owner_name("owner_e")

            self.assertEqual(res1, {})
            self.assertEqual(res2, {})
            self.assertIsNone(res3)
            self.assertEqual(res4, [])
            self.assertEqual(res5, "owner_e")  # Safe fallback without HTTP
            self.assertEqual(mock_post.call_count, 0)
            self.assertEqual(mock_get.call_count, 0)

    async def test_check_f_hubspot_boundary_is_demo_false_front_allowed(self):
        """F) HubSpotService boundary: call with is_demo=False obtained from DB + Front allowed -> behavior preserved"""
        import httpx
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": "ticket_real_front"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_resp)) as mock_post:
            hs = HubSpotService(is_demo=False)
            # Service 1 is Front (allowed by is_hubspot_side_effect_allowed)
            res = await hs.create_ticket({"subject": "real alarm"}, service_id=1)
            self.assertEqual(res.get("id"), "ticket_real_front")
            self.assertGreater(mock_post.call_count, 0)

    async def test_check_g_hubspot_boundary_unknown_zero_http(self):
        """G) HubSpotService boundary: call with demo status UNKNOWN / not provided -> 0 HTTP calls (fail-closed)"""
        import httpx
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock()) as mock_post, \
             patch.object(httpx.AsyncClient, "get", new=AsyncMock()) as mock_get:
            hs = HubSpotService()  # default is_demo=None (UNKNOWN)
            res1 = await hs.get_call("call_g")
            res2 = await hs.create_ticket({"subject": "test"})
            res3 = await hs.find_alarm_ticket(1, "call_g")
            res4 = await hs.search_calls_for_mass_evaluation({})
            res5 = await hs.get_owner_name("owner_g")

            self.assertEqual(res1, {})
            self.assertEqual(res2, {})
            self.assertIsNone(res3)
            self.assertEqual(res4, [])
            self.assertEqual(res5, "owner_g")  # Safe fallback without HTTP
            self.assertEqual(mock_post.call_count, 0)
            self.assertEqual(mock_get.call_count, 0)

    async def test_check_h_hubspot_boundary_uninitialized_context_zero_http(self):
        """H) HubSpotService initialized without sufficient company/service context to authorize -> 0 HTTP calls"""
        import httpx
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock()) as mock_post, \
             patch.object(httpx.AsyncClient, "get", new=AsyncMock()) as mock_get:
            # Caller does not supply verified demo context
            hs = HubSpotService()
            # Attempt to create ticket without verified is_demo
            await hs.create_ticket({"subject": "test"}, service_id=None, is_demo=None)
            await hs.get_call("call_h", is_demo=None)
            self.assertEqual(mock_post.call_count, 0)
            self.assertEqual(mock_get.call_count, 0)

    async def test_check_i_hubspot_boundary_filters_is_demo_false_not_authorizing(self):
        """I) payload/filter={'is_demo': False} without verified DB context -> does NOT authorize external request (0 HTTP calls)"""
        import httpx
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock()) as mock_post:
            # Service initialized without verified DB context
            hs = HubSpotService()
            # Caller passes filters with is_demo=False trying to bypass
            res = await hs.search_calls_for_mass_evaluation({"is_demo": False})
            self.assertEqual(res, [])
            self.assertEqual(mock_post.call_count, 0)


if __name__ == "__main__":
    unittest.main()
