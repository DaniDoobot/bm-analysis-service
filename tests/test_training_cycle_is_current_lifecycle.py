# -*- coding: utf-8 -*-
"""
Regression test suite for TrainingAgentReport.is_current lifecycle rules.

Covers all 7 mandatory regression cases:
1. Cycle A in_progress + Cycle B generated (pending_approval) & approved (in_progress)
   -> Cycle A remains is_current=True; Cycle B is is_current=False until Cycle A completes.
   -> When Cycle A completes via check_and_finalize_training_cycle, is_current transitions
      automatically from Cycle A (False) to Cycle B (True).
2. Cycle A in_progress + Cycle B failed -> Cycle A retains is_current=True; Cycle B is failed
   with is_current=False and does not supersede A.
3. Cycle A completed + Cycle B generated & approved -> Cycle A stays is_current=True while B
   is pending_approval; once B is approved (in_progress), B becomes is_current=True and A
   becomes is_current=False.
4. New cycle failed never leaves the agent without access to their previous valid cycle
   (in_progress or completed).
5. Simulations, completion statuses, sessions, recordings, evaluations, and transcriptions
   of Cycle A remain 100% intact.
6. super_admin continues seeing the full history (active, prepared, completed, failed).
7. Agent (/bm/training/me/current) receives the functional active cycle.
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

DB_FILE = "test_training_cycle_is_current_lifecycle.db"
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test_cycle_service_context.db")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB


@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingRun,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingCallEvaluation,
)
from app.services.personalized_training_service import (
    PersonalizedTrainingService,
    check_and_finalize_training_cycle,
)
from app.routers.personalized_training import (
    get_my_current_training,
    get_agent_detail_admin,
)
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole

MOCK_REPORT_AI_JSON = """{
    "summary_general": "Resumen del nuevo ciclo de octubre.",
    "strengths": [{"title": "Empatía", "description": "Buen trato.", "evidence": "Evidencia"}],
    "weaknesses": [{"title": "Cierre", "description": "Falta cierre.", "evidence": "Evidencia"}],
    "notable_data": [{"title": "Estable", "description": "Ritmo estable.", "metric_or_pattern": "Estable"}],
    "evolution_summary": "Evolución positiva.",
    "general_objectives": [
        {
            "title": "Mejorar Cierre",
            "description": "Confirmar cita al final.",
            "rationale": "Aumentar conversión.",
            "expected_behavior": "Preguntar disponibilidad concreta.",
            "success_indicators": ["Confirmación explícita"]
        }
    ],
    "specific_objectives": [
        {
            "title": "Manejo de Objeciones",
            "description": "Argumentar valor antes de colgar.",
            "related_criteria": ["cierre_cita"],
            "specific_behavior_to_improve": "Usar doble alternativa ante duda del paciente.",
            "success_indicators": ["Uso de doble alternativa"]
        }
    ]
}"""

MOCK_APPROVAL_PROMPTS_JSON = """{
    "simulation_prompts": [
        {"prompt_number": 1, "title": "Sim 1 Octubre", "scenario_type": "roleplay", "prompt_text": "Prompt 1"},
        {"prompt_number": 2, "title": "Sim 2 Octubre", "scenario_type": "roleplay", "prompt_text": "Prompt 2"},
        {"prompt_number": 3, "title": "Sim 3 Octubre", "scenario_type": "roleplay", "prompt_text": "Prompt 3"},
        {"prompt_number": 4, "title": "Sim 4 Octubre", "scenario_type": "roleplay", "prompt_text": "Prompt 4"}
    ]
}"""

MOCK_FINALIZE_JSON = """{
    "summary_final": "Ciclo completado satisfactoriamente.",
    "strengths_demonstrated": ["Empatía", "Escucha activa"],
    "areas_for_improvement": ["Cierre"],
    "objectives_status": [
        {
            "title": "Objetivo Ciclo A",
            "type": "general",
            "description": "Descripción",
            "status": "superado",
            "justification": "Superado con éxito."
        }
    ]
}"""


class TestTrainingCycleIsCurrentLifecycle(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        if os.path.exists(DB_FILE):
            try:
                os.remove(DB_FILE)
            except Exception:
                pass
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{DB_FILE}")

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        async with self.async_session() as db:
            db.add(
                Company(
                    company_id=1,
                    company_name="Boston Medical Group",
                    company_key="boston",
                    is_active=True,
                )
            )
            db.add(
                Service(
                    service_id=1,
                    company_id=1,
                    service_name="Front",
                    service_key="front",
                    is_active=True,
                )
            )
            db.add(
                Team(
                    team_id=1,
                    company_id=1,
                    service_id=1,
                    team_name="Equipo Front",
                )
            )
            db.add_all(
                [
                    User(
                        user_id=197,
                        company_id=1,
                        username="cmontenegro",
                        email="cristina@boston.com",
                        name="Cristina Montenegro",
                        role="agente",
                        hubspot_owner_id="33013276",
                        is_active=True,
                        primary_team_id=1,
                        primary_service_id=1,
                        password_hash="x",
                    ),
                    User(
                        user_id=1,
                        company_id=1,
                        username="admin",
                        email="admin@boston.com",
                        name="Super Admin",
                        role="super_admin",
                        is_active=True,
                        password_hash="x",
                    ),
                ]
            )
            db.add(
                TrainingAgentSetting(
                    setting_id=1,
                    company_id=1,
                    hubspot_owner_id="33013276",
                    agent_name="Cristina Montenegro",
                    agent_initials="CM",
                    is_enabled=True,
                    include_in_scheduler=True,
                )
            )
            db.add_all(
                [
                    TrainingRun(
                        training_run_id=103,
                        company_id=1,
                        service_id=1,
                        period_start=datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc),
                        period_end=datetime(2026, 9, 27, 23, 59, 59, tzinfo=timezone.utc),
                        status="completed",
                        triggered_by="scheduler",
                    ),
                    TrainingRun(
                        training_run_id=107,
                        company_id=1,
                        service_id=1,
                        period_start=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
                        period_end=datetime(2026, 10, 14, 23, 59, 59, tzinfo=timezone.utc),
                        status="running",
                        triggered_by="scheduler",
                    ),
                ]
            )

            # Cycle A (Report 273): in_progress with 3/4 completed simulations and 1 pending
            cycle_a = TrainingAgentReport(
                training_report_id=273,
                training_run_id=103,
                company_id=1,
                service_id=1,
                hubspot_owner_id="33013276",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                period_start=datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 9, 27, 23, 59, 59, tzinfo=timezone.utc),
                status="in_progress",
                is_current=True,
                evaluations_count=10,
                calls_count=10,
                general_objectives_json=[
                    {"title": "Objetivo Ciclo A", "description": "Descripción", "base_score": 6.0}
                ],
                specific_objectives_json=[],
            )
            db.add(cycle_a)
            await db.flush()

            for i in range(1, 5):
                prompt = TrainingSimulationPrompt(
                    simulation_prompt_id=920 + i,
                    training_report_id=273,
                    hubspot_owner_id="33013276",
                    prompt_number=i,
                    title=f"Simulación Ciclo A #{i}",
                    scenario_type="roleplay",
                    prompt_text=f"Prompt Ciclo A #{i}",
                )
                db.add(prompt)
                await db.flush()

                if i <= 3:
                    sess = TrainingCallSession(
                        session_id=250 + i,
                        call_sid=f"CA_TEST_{250 + i}",
                        agent_id="33013276",
                        cycle_id=273,
                        conversation_id=920 + i,
                        status="evaluated",
                        recording_url=f"https://audio.example.com/rec_{250 + i}.mp3",
                    )
                    db.add(sess)
                    await db.flush()

                    ev = TrainingCallEvaluation(
                        evaluation_id=200 + i,
                        session_id=sess.session_id,
                        cycle_id=273,
                        conversation_id=920 + i,
                        agent_id="33013276",
                        prompt_version_id=1,
                        transcription="Agente: Hola\nPaciente: Buenos días",
                        result_json={"cierre_cita": True},
                        score=Decimal("8.5"),
                        feedback=f"Excelente simulación {i}",
                    )
                    db.add(ev)
                    await db.flush()

                    comp = TrainingCompletionStatus(
                        completion_id=920 + i,
                        training_report_id=273,
                        simulation_prompt_id=prompt.simulation_prompt_id,
                        hubspot_owner_id="33013276",
                        status="completed",
                        call_session_id=sess.session_id,
                        evaluation_id=ev.evaluation_id,
                    )
                else:
                    comp = TrainingCompletionStatus(
                        completion_id=920 + i,
                        training_report_id=273,
                        simulation_prompt_id=prompt.simulation_prompt_id,
                        hubspot_owner_id="33013276",
                        status="pending",
                    )
                db.add(comp)

            await db.commit()

        self.agent_ctx = TenantContext(
            user_id=197,
            user_email="cristina@boston.com",
            user_role="agente",
            raw_role="agente",
            normalized_role=InternalRole.AGENT,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
            allowed_agent_ids=["33013276"],
            is_super_admin=False,
        )
        self.admin_ctx = TenantContext(
            user_id=1,
            user_email="admin@boston.com",
            user_role="super_admin",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=None,
            allowed_agent_ids=None,
            is_super_admin=True,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        if os.path.exists(DB_FILE):
            try:
                os.remove(DB_FILE)
            except Exception:
                pass

    def _mock_aggregates(self):
        return {
            "evaluations_count": 6,
            "calls_count": 6,
            "avg_evaluacion_global": Decimal("7.8"),
            "criteria_averages": {},
            "tipologia_distribution": {},
            "critical_feedbacks": [],
            "cierre_cita_rate": Decimal("75.0"),
        }

    # =========================================================================
    # CASE 2, 4, 5, 6, 7: Cycle A in_progress + Cycle B FAILED
    # =========================================================================
    @patch("app.services.personalized_training_service.complete_text")
    @patch("app.services.personalized_training_service.PersonalizedTrainingService.aggregate_agent_evaluations")
    async def test_failed_new_cycle_never_hides_active_in_progress_cycle(
        self, mock_agg, mock_complete
    ):
        """
        Cases 2, 4, 5, 6, 7:
        When Cycle A (273) is in_progress (3/4 simulations done) and Cycle B fails during generation:
        - Cycle B is saved with status='failed', is_current=False.
        - Cycle A retains status='in_progress', is_current=True, superseded_by_report_id=None.
        - Cycle A's 3 completed simulations, sessions, recordings, evaluations, and transcriptions remain intact.
        - Agent (/bm/training/me/current) continues seeing Cycle A (273) with 3/4 completed.
        - Super Admin continues seeing both Cycle A and the failed Cycle B in history.
        """
        mock_agg.return_value = self._mock_aggregates()
        mock_complete.side_effect = RuntimeError("Simulated LLM failure during October generation")

        async with self.async_session() as db:
            failed_b = await PersonalizedTrainingService.generate_report_for_agent(
                db,
                hubspot_owner_id="33013276",
                period_start=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 10, 14, 23, 59, 59, tzinfo=timezone.utc),
                run_id=107,
            )

            self.assertEqual(failed_b.status, "failed")
            self.assertFalse(failed_b.is_current, "Failed report must NOT be marked is_current=True when an active cycle exists")

            # Verify Cycle A (273) is untouched
            cycle_a = (
                await db.execute(
                    select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == 273)
                )
            ).scalars().first()
            self.assertEqual(cycle_a.status, "in_progress")
            self.assertTrue(cycle_a.is_current)
            self.assertIsNone(cycle_a.superseded_by_report_id)

            # Verify Agent view (/bm/training/me/current) still returns Cycle A with 3/4 simulations
            me_view = await get_my_current_training(context=self.agent_ctx, db=db)
            self.assertIsNotNone(me_view)
            self.assertEqual(me_view["training_report_id"], 273)
            self.assertEqual(me_view["status"], "in_progress")
            self.assertEqual(me_view["progress_completed"], 3)
            sims = me_view["simulations"]
            self.assertEqual(len(sims), 4)
            completed_sims = [s for s in sims if s["status"] == "completed"]
            self.assertEqual(len(completed_sims), 3)
            for s in completed_sims:
                self.assertIsNotNone(s["recording_url"])
                self.assertIsNotNone(s["score"])
                self.assertTrue(len(s["transcription_turns"]) >= 2)

            # Verify Super Admin view sees both Cycle A and failed Cycle B in history
            admin_view = await get_agent_detail_admin(
                hubspot_owner_id="33013276",
                include_archived=False,
                context=self.admin_ctx,
                db=db,
            )
            self.assertEqual(admin_view["current_report"]["training_report_id"], 273)
            hist_ids = {h["training_report_id"]: h["status"] for h in admin_view["history"]}
            self.assertEqual(hist_ids.get(273), "in_progress")
            self.assertEqual(hist_ids.get(failed_b.training_report_id), "failed")

    # =========================================================================
    # CASE 1, 5, 6, 7: Cycle A in_progress + Cycle B generated & approved
    #                  + automatic transition when Cycle A completes
    # =========================================================================
    @patch("app.services.personalized_training_service.complete_text")
    @patch("app.services.personalized_training_service.PersonalizedTrainingService.aggregate_agent_evaluations")
    async def test_new_cycle_generated_and_approved_does_not_hide_active_cycle_until_completed(
        self, mock_agg, mock_complete
    ):
        """
        Cases 1, 5, 6, 7:
        1. Cycle A (273) is in_progress (3/4 completed).
        2. Cycle B is generated (pending_approval): Cycle A stays is_current=True, Cycle B has is_current=False.
        3. Cycle B is approved (in_progress with 4 prompts): Cycle A STILL stays is_current=True,
           Cycle B has status='in_progress' and is_current=False (no two competing is_current=True cycles).
        4. Agent (/bm/training/me/current) still sees Cycle A (273) with its 3 completed simulations.
        5. Super Admin sees both Cycle A and Cycle B in history.
        6. When the 4th simulation of Cycle A is completed and check_and_finalize_training_cycle(273) runs:
           Cycle A transitions to status='completed', is_current=False, and Cycle B automatically transitions
           to is_current=True!
        7. Agent (/bm/training/me/current) now receives Cycle B as current_report.
        """
        mock_agg.return_value = self._mock_aggregates()
        mock_complete.return_value = MOCK_REPORT_AI_JSON

        async with self.async_session() as db:
            # Step 1: Generate Cycle B (pending_approval)
            cycle_b = await PersonalizedTrainingService.generate_report_for_agent(
                db,
                hubspot_owner_id="33013276",
                period_start=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 10, 14, 23, 59, 59, tzinfo=timezone.utc),
                run_id=107,
            )
            self.assertEqual(cycle_b.status, "pending_approval")
            self.assertFalse(cycle_b.is_current)

            cycle_a = (
                await db.execute(
                    select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == 273)
                )
            ).scalars().first()
            self.assertTrue(cycle_a.is_current)

            # Step 2: Approve Cycle B (transitions to in_progress)
            mock_complete.return_value = MOCK_APPROVAL_PROMPTS_JSON
            approved_b = await PersonalizedTrainingService.approve_training_cycle(
                db=db,
                report_id=cycle_b.training_report_id,
                approved_by_user_id=1,
            )
            self.assertEqual(approved_b.status, "in_progress")
            self.assertFalse(
                approved_b.is_current,
                "Approved Cycle B must remain is_current=False while Cycle A still has pending simulations",
            )

            # Verify only ONE report has is_current=True (Rule D)
            current_reps = (
                await db.execute(
                    select(TrainingAgentReport).where(
                        TrainingAgentReport.hubspot_owner_id == "33013276",
                        TrainingAgentReport.is_current == True,
                    )
                )
            ).scalars().all()
            self.assertEqual(len(current_reps), 1)
            self.assertEqual(current_reps[0].training_report_id, 273)

            # Verify Agent view still shows Cycle A (273)
            me_view = await get_my_current_training(context=self.agent_ctx, db=db)
            self.assertEqual(me_view["training_report_id"], 273)
            self.assertEqual(me_view["progress_completed"], 3)

            # Step 3: Complete the 4th simulation of Cycle A and finalize Cycle A
            comp_4 = (
                await db.execute(
                    select(TrainingCompletionStatus).where(
                        TrainingCompletionStatus.completion_id == 924
                    )
                )
            ).scalars().first()
            comp_4.status = "completed"
            await db.commit()

            mock_complete.return_value = MOCK_FINALIZE_JSON
            with patch(
                "app.services.training_knowledge_service.TrainingKnowledgeService.generate_cycle_knowledge_documents",
                return_value=None,
            ):
                await check_and_finalize_training_cycle(db, 273)

            # Verify Cycle A is now completed (is_current=False) and Cycle B is now is_current=True!
            await db.refresh(cycle_a)
            await db.refresh(approved_b)
            self.assertEqual(cycle_a.status, "completed")
            self.assertFalse(cycle_a.is_current)
            self.assertEqual(approved_b.status, "in_progress")
            self.assertTrue(approved_b.is_current)

            # Verify Agent view now transitions to Cycle B
            me_view_after = await get_my_current_training(context=self.agent_ctx, db=db)
            self.assertEqual(
                me_view_after["training_report_id"],
                approved_b.training_report_id,
            )
            self.assertEqual(me_view_after["status"], "in_progress")
            self.assertEqual(me_view_after["progress_completed"], 0)

    # =========================================================================
    # CASE 3 & 4: Cycle A completed + Cycle B generated, failed, or approved
    # =========================================================================
    @patch("app.services.personalized_training_service.complete_text")
    @patch("app.services.personalized_training_service.PersonalizedTrainingService.aggregate_agent_evaluations")
    async def test_completed_cycle_transitions_only_when_new_cycle_is_approved(
        self, mock_agg, mock_complete
    ):
        """
        Cases 3 & 4:
        When Cycle A (273) is already 'completed':
        1. If Cycle B generation FAILS: Cycle A stays is_current=True; agent still sees Cycle A.
        2. When Cycle B is generated (pending_approval): Cycle A stays is_current=True for the agent
           until Cycle B is approved.
        3. When Cycle B is approved (in_progress): Cycle B becomes is_current=True and Cycle A
           becomes is_current=False.
        """
        mock_agg.return_value = self._mock_aggregates()

        async with self.async_session() as db:
            # Mark Cycle A as already completed (4/4)
            cycle_a = (
                await db.execute(
                    select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == 273)
                )
            ).scalars().first()
            cycle_a.status = "completed"
            comp_4 = (
                await db.execute(
                    select(TrainingCompletionStatus).where(
                        TrainingCompletionStatus.completion_id == 924
                    )
                )
            ).scalars().first()
            comp_4.status = "completed"
            await db.commit()

            # 1. Generation of Cycle B fails -> Cycle A (completed) stays is_current=True
            mock_complete.side_effect = RuntimeError("Temporary LLM error")
            failed_b = await PersonalizedTrainingService.generate_report_for_agent(
                db,
                hubspot_owner_id="33013276",
                period_start=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 10, 14, 23, 59, 59, tzinfo=timezone.utc),
                run_id=107,
            )
            self.assertEqual(failed_b.status, "failed")
            self.assertFalse(failed_b.is_current)

            me_view_1 = await get_my_current_training(context=self.agent_ctx, db=db)
            self.assertEqual(me_view_1["training_report_id"], 273)
            self.assertEqual(me_view_1["status"], "completed")

            # 2. Retry generation of Cycle B succeeds (pending_approval) -> Cycle A stays visible to agent
            mock_complete.side_effect = None
            mock_complete.return_value = MOCK_REPORT_AI_JSON
            pending_b = await PersonalizedTrainingService.generate_report_for_agent(
                db,
                hubspot_owner_id="33013276",
                period_start=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 10, 14, 23, 59, 59, tzinfo=timezone.utc),
                run_id=107,
            )
            self.assertEqual(pending_b.status, "pending_approval")
            self.assertFalse(pending_b.is_current)

            me_view_2 = await get_my_current_training(context=self.agent_ctx, db=db)
            self.assertEqual(me_view_2["training_report_id"], 273)

            # 3. Approve Cycle B -> Cycle B becomes is_current=True, Cycle A becomes is_current=False
            mock_complete.return_value = MOCK_APPROVAL_PROMPTS_JSON
            approved_b = await PersonalizedTrainingService.approve_training_cycle(
                db=db,
                report_id=pending_b.training_report_id,
                approved_by_user_id=1,
            )
            await db.refresh(cycle_a)
            self.assertEqual(approved_b.status, "in_progress")
            self.assertTrue(approved_b.is_current)
            self.assertFalse(cycle_a.is_current)

            me_view_3 = await get_my_current_training(context=self.agent_ctx, db=db)
            self.assertEqual(
                me_view_3["training_report_id"],
                approved_b.training_report_id,
            )
            self.assertEqual(me_view_3["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
