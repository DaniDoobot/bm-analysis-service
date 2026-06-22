"""
Verification test suite for personalized training cycle status logic.
Validates:
1. Cycle/run status is set to completed/in_progress instead of failed upon successful creation.
2. The generated cycle is visible and retrievable.
3. Errors before cycle creation mark status as failed.
4. Subsequent retries without force_regenerate do not create duplicates.
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
    print("=== INICIANDO PRUEBAS DE ESTADO DE LA GENERACION DE CICLOS ===")
    
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
        # TEST 1: Successful creation returns "completed" run status and "in_progress" report status
        # -------------------------------------------------------------
        print("\nTest 1: Normal cycle generation (Gemini response succeeds)...")
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

            # Check the report status in db
            stmt_rep = select(TrainingAgentReport).where(
                TrainingAgentReport.training_run_id == run_id
            )
            res_rep = await db.execute(stmt_rep)
            report = res_rep.scalars().first()
            assert report is not None
            print(f"Report status in db: {report.status}")
            assert report.status == "in_progress", f"Expected report status 'in_progress', got '{report.status}'"

        # -------------------------------------------------------------
        # TEST 2: Visibility / query endpoints work
        # -------------------------------------------------------------
        print("\nTest 2: Verifying visibility via get_agent_detail...")
        detail = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=hubspot_owner_id)
        assert detail is not None
        assert detail.get("current_report") is not None
        assert detail["current_report"]["status"] == "in_progress"
        print("[OK] Cycle successfully visible and retrieved.")

        # -------------------------------------------------------------
        # TEST 3: Idempotency / No duplicates on retry
        # -------------------------------------------------------------
        print("\nTest 3: Retrying generation without force_regenerate (should not duplicate)...")
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

            # Generate again
            run2 = await PersonalizedTrainingService.run_personalized_training_pass(
                db=db,
                hubspot_owner_ids=[hubspot_owner_id],
                period_start=p_start,
                period_end=p_end,
                force_regenerate=False # Idempotency check!
            )
            run2_id = run2.training_run_id

            # Check that we did not add a new report
            stmt_reps = select(TrainingAgentReport).where(
                TrainingAgentReport.hubspot_owner_id == hubspot_owner_id
            )
            res_reps = await db.execute(stmt_reps)
            all_reps = res_reps.scalars().all()
            print(f"Total reports found for agent: {len(all_reps)}")
            assert len(all_reps) == 1, f"Expected exactly 1 report, found {len(all_reps)}"

        # -------------------------------------------------------------
        # TEST 4: Fails when critical steps raise exceptions
        # -------------------------------------------------------------
        print("\nTest 4: Simulating critical failure (should mark status as failed)...")
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

        # Clean up mock data generated in test
        print("\nCleaning up test data...")
        await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id == hubspot_owner_id))
        await db.execute(delete(TrainingRun).where(TrainingRun.training_run_id.in_([run_id, run2_id, run_failed_id])))
        await db.commit()
        print("=== TODAS LAS PRUEBAS DE ESTADO HAN PASADO CON EXITO ===")

if __name__ == "__main__":
    asyncio.run(test_cycle_status_logic())
