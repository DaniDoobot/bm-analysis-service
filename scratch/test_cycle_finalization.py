import asyncio
import os
import sys
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select, and_

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

# Mock OpenAI before import
import app.services.personalized_training_service

async def mock_complete_text(messages, temperature=0.3, response_format=None):
    print("[MOCK] OpenAI complete_text called for consolidation")
    return """
    {
        "summary_final": "El agente ha mostrado excelente mejoria en general.",
        "strengths": [
            {"title": "Escucha activa", "description": "Muestra empatía en todo momento", "evidence": "Llamada 3"},
            {"title": "Claridad", "description": "Explica bien el tratamiento", "evidence": "Llamada 4"},
            {"title": "Saludo", "description": "Saluda cordialmente", "evidence": "Llamada 1"}
        ],
        "weaknesses": [
            {"title": "Cierre", "description": "Le cuesta concretar la cita", "evidence": "Llamada 2"},
            {"title": "Precio", "description": "Muestra inseguridad al hablar de costes", "evidence": "Llamada 3"},
            {"title": "Objeciones", "description": "No argumenta con firmeza", "evidence": "Llamada 4"}
        ],
        "recommendations": "Seguir practicando objeciones de precio en el proximo ciclo.",
        "objectives_status": [
            {
                "title": "Escucha activa",
                "type": "general",
                "description": "Prestar atencion",
                "status": "superado",
                "score": 8.50,
                "justification": "Supero por 1.5 puntos"
            },
            {
                "title": "Claridad en el saludo",
                "type": "especifico",
                "description": "Saludo",
                "status": "no_superado",
                "score": 6.80,
                "justification": "Solo mejoro 0.3 puntos"
            }
        ]
    }
    """

app.services.personalized_training_service.complete_text = mock_complete_text

from app.db import get_engine
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingCallEvaluation,
    TrainingEvaluationPrompt
)
from app.services.personalized_training_service import (
    check_and_finalize_training_cycle,
    PersonalizedTrainingService
)
from app.services.db_init_service import init_db

async def main():
    print("=== TEST: FINALIZACION DE CICLO Y ARRASTRE DE OBJETIVOS ===")
    
    # Initialize DB
    # await init_db()
    
    engine = get_engine()
    async with AsyncSession(engine) as db:
        # Resolve/Create agent setting
        stmt_set = select(TrainingAgentSetting).limit(1)
        res_set = await db.execute(stmt_set)
        setting = res_set.scalar()
        if not setting:
            print("Creando agente dummy...")
            setting = TrainingAgentSetting(
                hubspot_owner_id="dummy_owner_2",
                agent_name="Agente Ciclos",
                agent_initials="AC",
                is_enabled=True,
                training_code="AC22",
                training_numeric_code="2222"
            )
            db.add(setting)
            await db.flush()
            
        hubspot_owner_id = setting.hubspot_owner_id
        agent_name = setting.agent_name
        agent_initials = setting.agent_initials
        
        # Create a report cycle
        print("Creando reporte cycle...")
        report = TrainingAgentReport(
            hubspot_owner_id=hubspot_owner_id,
            agent_name=agent_name,
            agent_initials=agent_initials,
            period_start=datetime.now(timezone.utc),
            period_end=datetime.now(timezone.utc),
            status="running",
            is_current=True,
            evaluations_count=4,
            calls_count=4,
            avg_evaluacion_global=7.0,
            general_objectives_json=[{
                "title": "Escucha activa",
                "description": "Prestar atencion",
                "base_score": 7.0
            }],
            specific_objectives_json=[{
                "title": "Claridad en el saludo",
                "description": "Saludo",
                "related_criteria": ["claridad"],
                "base_score": 6.5
            }]
        )
        db.add(report)
        await db.flush()
        cycle_report_id = report.training_report_id
        
        # Seed 4 prompts and mark completion as pending
        prompts = []
        for i in range(1, 5):
            pr = TrainingSimulationPrompt(
                training_report_id=cycle_report_id,
                hubspot_owner_id=hubspot_owner_id,
                prompt_number=i,
                title=f"Roleplay {i}",
                scenario_type="roleplay",
                prompt_text=f"Prompt {i}"
            )
            db.add(pr)
            await db.flush()
            prompts.append(pr)
            
            comp = TrainingCompletionStatus(
                training_report_id=cycle_report_id,
                simulation_prompt_id=pr.simulation_prompt_id,
                hubspot_owner_id=hubspot_owner_id,
                status="completed", # Mark as completed to satisfy 4/4 completed rule
                completed_at=datetime.now(timezone.utc)
            )
            db.add(comp)
            await db.flush()
            
        # Create active prompt version for service
        stmt_srv = select(TrainingEvaluationPrompt).limit(1)
        res_srv = await db.execute(stmt_srv)
        prompt_ver = res_srv.scalar()
        if not prompt_ver:
            prompt_ver = TrainingEvaluationPrompt(
                service_id=1,
                prompt_text="Prompt de eval",
                is_active=True
            )
            db.add(prompt_ver)
            await db.flush()
            
        # Add 4 Call Sessions and Evaluations
        # Call 1: score=8.0, claridad=7.0
        # Call 2: score=8.5, claridad=null (excluded)
        # Call 3: score=9.0, claridad=6.0
        # Call 4: score=9.0, claridad=7.5
        # General math expected average score: (8.0 + 8.5 + 9.0 + 9.0)/4 = 8.625 -> improvement: 8.625 - 7.0 = 1.625 >= 1.0 (SUPERADO)
        # Specific Clarity average: (7.0 + 6.0 + 7.5)/3 = 6.833 -> improvement: 6.833 - 6.5 = 0.333 < 1.0 (NO_SUPERADO - carried over)
        scores = [8.0, 8.5, 9.0, 9.0]
        clarity_scores = [7.0, None, 6.0, 7.5]
        
        import uuid
        run_uuid = uuid.uuid4().hex[:8]
        for idx in range(4):
            sess = TrainingCallSession(
                call_sid=f"call_sid_cycle_{run_uuid}_{idx}",
                agent_id=hubspot_owner_id,
                cycle_id=cycle_report_id,
                conversation_id=prompts[idx].simulation_prompt_id,
                status="evaluated"
            )
            db.add(sess)
            await db.flush()
            
            eval_record = TrainingCallEvaluation(
                session_id=sess.session_id,
                cycle_id=cycle_report_id,
                conversation_id=prompts[idx].simulation_prompt_id,
                agent_id=hubspot_owner_id,
                prompt_version_id=prompt_ver.id,
                transcription="Transcripcion dummy",
                result_json={"claridad": clarity_scores[idx]},
                score=Decimal(str(scores[idx])),
                feedback="Feedback dummy"
            )
            db.add(eval_record)
            await db.flush()
            
        await db.commit()
        
    print(f"Finalizando ciclo report_id={cycle_report_id}...")
    async with AsyncSession(engine) as db:
        await check_and_finalize_training_cycle(db, cycle_report_id)
        
    # Check values in DB after finalization
    async with AsyncSession(engine) as db:
        stmt_check = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == cycle_report_id)
        res_check = await db.execute(stmt_check)
        rep = res_check.scalar()
        
        print(f"Report status: {rep.status} (esperado: 'completed')")
        assert rep.status == "completed"
        
        # Verify post-processed math values are enforced in final_report_json
        final_json = rep.final_report_json
        obj_status = final_json.get("objectives_status") or []
        print(f"Número de objetivos en reporte final: {len(obj_status)}")
        
        escucha_obj = [o for o in obj_status if o.get("title") == "Escucha activa"][0]
        claridad_obj = [o for o in obj_status if o.get("title") == "Claridad en el saludo"][0]
        
        print(f"Escucha activa (General): base_score={escucha_obj.get('base_score')}, score={escucha_obj.get('score')}, status={escucha_obj.get('status')}")
        assert escucha_obj.get("status") == "superado"
        assert float(escucha_obj.get("score")) == 8.62
        
        print(f"Claridad en el saludo (Especifico): base_score={claridad_obj.get('base_score')}, score={claridad_obj.get('score')}, status={claridad_obj.get('status')}")
        assert claridad_obj.get("status") == "no_superado"
        assert float(claridad_obj.get("score")) == 6.83
        
        # Test carry over: generating next report should carry over the Clarity objective
        print("Probando generación de próximo ciclo para ver el arrastre de objetivos...")
        # Mock aggregates for next report
        mock_aggregates = {
            "evaluations_count": 1,
            "calls_count": 1,
            "avg_evaluacion_global": 7.5,
            "criteria_averages": {
                "claridad": {"name": "Claridad", "value": 7.0, "type": "numeric"}
            },
            "critical_feedbacks": [],
            "tipologia_distribution": {},
            "cierre_cita_rate": 80.0
        }
        
        # Monkeypatch aggregate_agent_evaluations
        async def mock_aggregate(db, owner_id, start, end):
            return mock_aggregates
        
        import app.services.personalized_training_service
        app.services.personalized_training_service.PersonalizedTrainingService.aggregate_agent_evaluations = mock_aggregate
        
        # Mock OpenAI report generation response (exactly 3 new objectives)
        async def mock_complete_text_gen(messages, temperature=0.3, response_format=None):
            return """
            {
                "summary_general": "Resumen general nuevo",
                "strengths": ["S1", "S2", "S3"],
                "weaknesses": ["W1", "W2", "W3"],
                "notable_data": ["D1", "D2", "D3"],
                "evolution_summary": "Evolución nueva",
                "general_objectives": [
                    {"title": "Nuevo Obj Gen 1", "description": "Desc", "rationale": "Just", "expected_behavior": "Beh", "success_indicators": ["Ind"]},
                    {"title": "Nuevo Obj Gen 2", "description": "Desc", "rationale": "Just", "expected_behavior": "Beh", "success_indicators": ["Ind"]},
                    {"title": "Nuevo Obj Gen 3", "description": "Desc", "rationale": "Just", "expected_behavior": "Beh", "success_indicators": ["Ind"]}
                ],
                "specific_objectives": [
                    {"title": "Nuevo Obj Spec 1", "description": "Desc", "related_criteria": ["crit1"], "success_indicators": ["Ind"]},
                    {"title": "Nuevo Obj Spec 2", "description": "Desc", "related_criteria": ["crit2"], "success_indicators": ["Ind"]},
                    {"title": "Nuevo Obj Spec 3", "description": "Desc", "related_criteria": ["crit3"], "success_indicators": ["Ind"]}
                ],
                "simulation_prompts": [
                    {"prompt_number": 1, "title": "S1", "scenario_type": "roleplay", "prompt_text": "P1", "objective_focus": ["f1"]},
                    {"prompt_number": 2, "title": "S2", "scenario_type": "roleplay", "prompt_text": "P2", "objective_focus": ["f2"]},
                    {"prompt_number": 3, "title": "S3", "scenario_type": "roleplay", "prompt_text": "P3", "objective_focus": ["f3"]},
                    {"prompt_number": 4, "title": "S4", "scenario_type": "roleplay", "prompt_text": "P4", "objective_focus": ["f4"]}
                ]
            }
            """
        app.services.personalized_training_service.complete_text = mock_complete_text_gen
        
        # Generate new report
        next_report = await PersonalizedTrainingService.generate_report_for_agent(
            db=db,
            hubspot_owner_id=hubspot_owner_id,
            period_start=datetime.now(timezone.utc),
            period_end=datetime.now(timezone.utc),
            force_regenerate=True
        )
        await db.refresh(next_report)
        
        print(f"Total objetivos generales en nuevo ciclo: {len(next_report.general_objectives_json)}")
        print(f"Total objetivos específicos en nuevo ciclo: {len(next_report.specific_objectives_json)}")
        
        # Clarity specific objective (carried over) must be appended!
        # It should be the 4th specific objective
        assert len(next_report.general_objectives_json) == 3 # None was carried over
        assert len(next_report.specific_objectives_json) == 4 # 3 new + 1 carried over!
        
        carried_spec = next_report.specific_objectives_json[3]
        print(f"Objetivo arrastrado detectado: title={carried_spec.get('title')}, is_carried_over={carried_spec.get('is_carried_over')}, base_score={carried_spec.get('base_score')}")
        assert carried_spec.get("is_carried_over") is True
        assert carried_spec.get("title") == "Claridad en el saludo"
        assert float(carried_spec.get("base_score")) == 6.5 # Preserves previous base_score!
        
    print("[OK] Consolidacion de ciclo y arrastre de objetivos validados con exito.")

if __name__ == "__main__":
    asyncio.run(main())
