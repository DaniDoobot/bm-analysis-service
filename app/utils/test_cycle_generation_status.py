"""
Verification test suite for personalized training cycle status logic.
Validates the NEW two-phase approval flow:
1. Generation creates cycle in 'pending_approval' status (no prompts yet).
2. Agents cannot see pending_approval cycles (get_agent_detail excludes them).
3. Admins CAN see pending_approval cycles (include_pending_approval=True).
4. Approving a cycle generates prompts and moves status to 'in_progress'.
5. Agents CAN see in_progress cycles.
6. Idempotency: re-approving an in_progress cycle is a no-op.
7. Errors mark status as failed.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine
from app.services.personalized_training_service import PersonalizedTrainingService
from app.models.personalized_training import TrainingAgentSetting, TrainingAgentReport, TrainingRun
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

MOCK_AI_RESPONSE = """{
    "summary_general": "El agente necesita mejorar su consistencia en el saludo.",
    "strengths": [
        {"title": "Escucha Activa", "description": "Muestra interés.", "evidence": "Empatía."}
    ],
    "weaknesses": [
        {"title": "Cierre Incompleto", "description": "No verifica si hay dudas.", "evidence": "Cuelga rápido."}
    ],
    "notable_data": [
        {"title": "Tasa de Cierre", "description": "Buen promedio.", "metric_or_pattern": "85%"}
    ],
    "evolution_summary": "Línea base inicial.",
    "general_objectives": [
        {"title": "Estructurar el Cierre", "description": "Preguntar siempre si hay más dudas.", "rationale": "Para garantizar satisfacción.", "expected_behavior": "Pregunta", "success_indicators": ["Pregunta final"]}
    ],
    "specific_objectives": [
        {
            "title": "Manejo de Objeciones",
            "description": "Responder de forma empática.",
            "related_criteria": ["protocolo_general"],
            "specific_behavior_to_improve": "Usar frases empáticas.",
            "success_indicators": ["Disminución del tono defensivo"]
        }
    ],
    "simulation_prompts": [
        {"prompt_number": 1, "title": "Sim 1", "scenario_type": "roleplay", "prompt_text": "Prompt 1"},
        {"prompt_number": 2, "title": "Sim 2", "scenario_type": "roleplay", "prompt_text": "Prompt 2"},
        {"prompt_number": 3, "title": "Sim 3", "scenario_type": "roleplay", "prompt_text": "Prompt 3"},
        {"prompt_number": 4, "title": "Sim 4", "scenario_type": "roleplay", "prompt_text": "Prompt 4"}
    ]
}"""

async def test_cycle_status_logic():
    print("=== INICIANDO PRUEBAS DE ESTADO DE LA GENERACION DE CICLOS (FLUJO DE APROBACION) ===")
    
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        # Find an enabled agent in the database to test
        stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.is_enabled == True)
        res_set = await db.execute(stmt_set)
        agent_setting = res_set.scalars().first()
        
        if not agent_setting:
            print("[SKIP] No enabled agents found in db. Cannot run test.")
            return

        hubspot_owner_id = agent_setting.hubspot_owner_id
        initials = agent_setting.agent_initials
        print(f"Testing with agent: {agent_setting.agent_name} ({hubspot_owner_id})")

        # Cleanup existing reports/runs for this agent to avoid conflicts
        await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id == hubspot_owner_id))
        await db.commit()

        p_start = datetime.now(timezone.utc) - timedelta(days=14)
        p_end = datetime.now(timezone.utc)

        # -------------------------------------------------------------
        # TEST 1: Generation creates 'pending_approval' status (NOT in_progress)
        # -------------------------------------------------------------
        print("\nTest 1: Generation returns pending_approval status...")
        run_id = None
        run2_id = None
        run_failed_id = None
        with patch("app.services.personalized_training_service.complete_text", new_callable=AsyncMock) as mock_complete, \
             patch.object(PersonalizedTrainingService, "aggregate_agent_evaluations", new_callable=AsyncMock) as mock_aggregate:
            
            mock_complete.return_value = MOCK_AI_RESPONSE
            mock_aggregate.return_value = {
                "evaluations_count": 3,
                "calls_count": 3,
                "avg_evaluacion_global": 8.0,
                "criteria_averages": {},
                "tipologia_distribution": {},
                "critical_feedbacks": [],
                "cierre_cita_rate": 90.0
            }

            run = await PersonalizedTrainingService.run_personalized_training_pass(
                db=db,
                hubspot_owner_ids=[hubspot_owner_id],
                period_start=p_start,
                period_end=p_end,
                force_regenerate=True
            )
            run_id = run.training_run_id

            print(f"Run status: {run.status}")
            print(f"Agents completed: {run.agents_completed}, failed: {run.agents_failed}")
            
            assert run.status == "completed", f"Expected run status 'completed', got '{run.status}'"
            assert run.agents_failed == 0, f"Expected 0 failed agents, got {run.agents_failed}"
            assert run.agents_completed == 1, f"Expected 1 completed agent, got {run.agents_completed}"

            # Check the report status is 'pending_approval' (NEW FLOW)
            stmt_rep = select(TrainingAgentReport).where(
                TrainingAgentReport.training_run_id == run_id
            )
            res_rep = await db.execute(stmt_rep)
            report = res_rep.scalars().first()
            assert report is not None
            print(f"Report status in db: {report.status}")
            assert report.status == "pending_approval", f"Expected 'pending_approval', got '{report.status}'"
            
            # Verify NO prompts were created yet
            from app.models.personalized_training import TrainingSimulationPrompt, TrainingCompletionStatus
            stmt_prompts = select(TrainingSimulationPrompt).where(
                TrainingSimulationPrompt.training_report_id == report.training_report_id
            )
            res_prompts = await db.execute(stmt_prompts)
            prompts = res_prompts.scalars().all()
            assert len(prompts) == 0, f"Expected 0 prompts before approval, got {len(prompts)}"
            print("[OK] No prompts created before approval.")
            
            report_id = report.training_report_id

        # -------------------------------------------------------------
        # TEST 2: Agent cannot see pending_approval cycles
        # -------------------------------------------------------------
        print("\nTest 2: Agent visibility - pending_approval cycle should NOT be visible...")
        detail_agent = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=hubspot_owner_id)
        assert detail_agent is not None
        # Agent's current_report should be None (pending_approval is hidden)
        assert detail_agent.get("current_report") is None, (
            f"Expected None for agent (pending_approval hidden), got: {detail_agent.get('current_report', {}).get('status')}"
        )
        print("[OK] Agent cannot see pending_approval cycle.")

        # -------------------------------------------------------------
        # TEST 3: Admin CAN see pending_approval cycles
        # -------------------------------------------------------------
        print("\nTest 3: Admin visibility - pending_approval cycle should be visible...")
        detail_admin = await PersonalizedTrainingService.get_agent_detail(
            db, hubspot_owner_id=hubspot_owner_id, include_pending_approval=True
        )
        assert detail_admin is not None
        assert detail_admin.get("current_report") is not None, "Admin should see pending_approval cycle"
        assert detail_admin["current_report"]["status"] == "pending_approval"
        print("[OK] Admin can see pending_approval cycle.")

        # -------------------------------------------------------------
        # TEST 4: Approve cycle - generates prompts and moves to in_progress
        # -------------------------------------------------------------
        print("\nTest 4: Approving cycle...")
        approved_report = await PersonalizedTrainingService.approve_training_cycle(
            db=db,
            report_id=report_id,
            approved_by_user_id=1  # Fake admin user_id for test
        )
        print(f"Approved report status: {approved_report.status}")
        assert approved_report.status == "in_progress", f"Expected 'in_progress', got '{approved_report.status}'"
        assert approved_report.approved_at is not None
        assert approved_report.approved_by_user_id == 1
        
        # Verify prompts were created after approval
        from app.models.personalized_training import TrainingSimulationPrompt, TrainingCompletionStatus
        stmt_prompts = select(TrainingSimulationPrompt).where(
            TrainingSimulationPrompt.training_report_id == report_id
        )
        res_prompts = await db.execute(stmt_prompts)
        prompts = res_prompts.scalars().all()
        assert len(prompts) == 4, f"Expected 4 prompts after approval, got {len(prompts)}"
        
        stmt_comp = select(TrainingCompletionStatus).where(
            TrainingCompletionStatus.training_report_id == report_id
        )
        res_comp = await db.execute(stmt_comp)
        completions = res_comp.scalars().all()
        assert len(completions) == 4, f"Expected 4 completion statuses after approval, got {len(completions)}"
        print(f"[OK] {len(prompts)} prompts and {len(completions)} completion statuses created after approval.")

        # -------------------------------------------------------------
        # TEST 5: Agent CAN see the cycle after approval
        # -------------------------------------------------------------
        print("\nTest 5: Agent visibility - cycle should be visible after approval...")
        detail_agent_after = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=hubspot_owner_id)
        assert detail_agent_after is not None
        assert detail_agent_after.get("current_report") is not None, "Agent should see cycle after approval"
        assert detail_agent_after["current_report"]["status"] == "in_progress"
        print("[OK] Agent can see cycle after approval.")

        # -------------------------------------------------------------
        # TEST 6: Idempotency - retrying generation without force_regenerate does not duplicate
        # -------------------------------------------------------------
        print("\nTest 6: Retrying generation without force_regenerate (should not duplicate)...")
        with patch("app.services.personalized_training_service.complete_text", new_callable=AsyncMock) as mock_complete, \
             patch.object(PersonalizedTrainingService, "aggregate_agent_evaluations", new_callable=AsyncMock) as mock_aggregate:
            
            mock_complete.return_value = MOCK_AI_RESPONSE
            mock_aggregate.return_value = {
                "evaluations_count": 3,
                "calls_count": 3,
                "avg_evaluacion_global": 8.0,
                "criteria_averages": {},
                "tipologia_distribution": {},
                "critical_feedbacks": [],
                "cierre_cita_rate": 90.0
            }

            run2 = await PersonalizedTrainingService.run_personalized_training_pass(
                db=db,
                hubspot_owner_ids=[hubspot_owner_id],
                period_start=p_start,
                period_end=p_end,
                force_regenerate=False  # Idempotency check!
            )
            run2_id = run2.training_run_id

            # Should return the existing report (not create a new one)
            stmt_reps = select(TrainingAgentReport).where(
                TrainingAgentReport.hubspot_owner_id == hubspot_owner_id
            )
            res_reps = await db.execute(stmt_reps)
            all_reps = res_reps.scalars().all()
            print(f"Total reports found for agent: {len(all_reps)}")
            assert len(all_reps) == 1, f"Expected exactly 1 report (idempotency), found {len(all_reps)}"
        print("[OK] Idempotency verified - no duplicate reports.")

        # -------------------------------------------------------------
        # TEST 7: Re-approving an in_progress cycle is a no-op
        # -------------------------------------------------------------
        print("\nTest 7: Re-approving an already in_progress cycle (should be no-op)...")
        re_approved = await PersonalizedTrainingService.approve_training_cycle(
            db=db,
            report_id=report_id,
            approved_by_user_id=1
        )
        assert re_approved.status == "in_progress", "Re-approval should keep in_progress status"
        # Prompts should not be duplicated
        stmt_prompts2 = select(TrainingSimulationPrompt).where(
            TrainingSimulationPrompt.training_report_id == report_id
        )
        res_prompts2 = await db.execute(stmt_prompts2)
        prompts2 = res_prompts2.scalars().all()
        assert len(prompts2) == 4, f"Expected 4 prompts (no duplicates), got {len(prompts2)}"
        print("[OK] Re-approval is idempotent - no duplicate prompts.")

        # -------------------------------------------------------------
        # TEST 8: Failure marks status as failed
        # -------------------------------------------------------------
        print("\nTest 8: Simulating critical failure (should mark status as failed)...")
        with patch.object(PersonalizedTrainingService, "aggregate_agent_evaluations", side_effect=ValueError("DB connection error")):
            
            run_failed = await PersonalizedTrainingService.run_personalized_training_pass(
                db=db,
                hubspot_owner_ids=[hubspot_owner_id],
                period_start=p_start,
                period_end=p_end,
                force_regenerate=True
            )
            run_failed_id = run_failed.training_run_id

            print(f"Failed Run status: {run_failed.status}")
            print(f"Failed Run agents_failed: {run_failed.agents_failed}")
            assert run_failed.status == "failed"
            assert run_failed.agents_failed == 1

            # Check failed report in db
            stmt_failed_rep = select(TrainingAgentReport).where(
                TrainingAgentReport.training_run_id == run_failed_id
            )
            res_failed_rep = await db.execute(stmt_failed_rep)
            failed_rep = res_failed_rep.scalars().first()
            assert failed_rep is not None
            assert failed_rep.status == "failed"
            print(f"Failed report error message: {failed_rep.error_message}")
            assert "DB connection error" in failed_rep.error_message
        print("[OK] Failure correctly marks report as failed.")

        # Clean up test data
        print("\nCleaning up test data...")
        await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id == hubspot_owner_id))
        run_ids = [rid for rid in [run_id, run2_id, run_failed_id] if rid is not None]
        if run_ids:
            await db.execute(delete(TrainingRun).where(TrainingRun.training_run_id.in_(run_ids)))
        await db.commit()
        print("=== TODAS LAS PRUEBAS DE ESTADO HAN PASADO CON EXITO ===")

if __name__ == "__main__":
    asyncio.run(test_cycle_status_logic())
