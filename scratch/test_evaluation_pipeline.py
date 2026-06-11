import asyncio
import os
import sys
from datetime import datetime, timezone
from sqlalchemy import select, and_

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

# Mock external services before importing service tasks
import app.services.twilio_service
import app.services.openai_service

async def mock_download_audio(self, url):
    print("[MOCK] download_audio called")
    return b"dummy audio bytes"

async def mock_analyze_audio_bytes(audio_bytes, prompt_text, audio_format):
    print("[MOCK] analyze_audio_bytes called")
    return '{"score": 8.5, "feedback": "Buen trato del paciente y resolucion correcta.", "transcription": "Agente: Buenos dias. Paciente: Hola, tengo efectos secundarios."}'

app.services.twilio_service.TwilioService.download_audio = mock_download_audio
app.services.openai_service.analyze_audio_bytes = mock_analyze_audio_bytes

from app.db import get_engine
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.personalized_training import (
    TrainingCallSession,
    TrainingCompletionStatus,
    TrainingCallEvaluation,
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingSimulationPrompt
)
from app.services.personalized_training_service import evaluate_training_session_task
from app.services.db_init_service import init_db

async def main():
    print("=== TEST: PIPELINE DE EVALUACION ASINCRONA ===")
    
    # Initialize DB
    # await init_db()
    
    engine = get_engine()
    async with AsyncSession(engine) as db:
        # Resolve an agent setting or create one
        stmt_set = select(TrainingAgentSetting).limit(1)
        res_set = await db.execute(stmt_set)
        setting = res_set.scalar()
        if not setting:
            print("⚠️ No hay agentes configurados. Creando agente dummy...")
            setting = TrainingAgentSetting(
                hubspot_owner_id="dummy_owner_1",
                agent_name="Agente Pruebas",
                agent_initials="AP",
                is_enabled=True,
                training_code="AP11",
                training_numeric_code="1111"
            )
            db.add(setting)
            await db.flush()
            
        hubspot_owner_id = setting.hubspot_owner_id
        
        # Resolve/Create an agent cycle report
        stmt_rep = select(TrainingAgentReport).where(
            and_(
                TrainingAgentReport.hubspot_owner_id == hubspot_owner_id,
                TrainingAgentReport.status == "running"
            )
        ).limit(1)
        res_rep = await db.execute(stmt_rep)
        report = res_rep.scalar()
        
        if not report:
            print("Creando reporte/ciclo de pruebas...")
            report = TrainingAgentReport(
                hubspot_owner_id=hubspot_owner_id,
                agent_name=setting.agent_name,
                agent_initials=setting.agent_initials,
                period_start=datetime.now(timezone.utc),
                period_end=datetime.now(timezone.utc),
                status="running",
                is_current=True,
                evaluations_count=1,
                calls_count=1,
                avg_evaluacion_global=7.0,
                general_objectives_json=[{
                    "title": "Escucha activa",
                    "description": "Prestar atencion a dudas",
                    "base_score": 7.0
                }],
                specific_objectives_json=[{
                    "title": "Claridad en el saludo",
                    "description": "Saludo inicial segun protocolo",
                    "related_criteria": ["claridad"],
                    "base_score": 6.5
                }]
            )
            db.add(report)
            await db.flush()
            
        # Resolve/Create a prompt
        stmt_pr = select(TrainingSimulationPrompt).where(
            TrainingSimulationPrompt.training_report_id == report.training_report_id
        ).limit(1)
        res_pr = await db.execute(stmt_pr)
        prompt = res_pr.scalar()
        
        if not prompt:
            print("Creando prompt de simulacion de pruebas...")
            prompt = TrainingSimulationPrompt(
                training_report_id=report.training_report_id,
                hubspot_owner_id=hubspot_owner_id,
                prompt_number=1,
                title="Caso Ondas de Choque",
                scenario_type="roleplay",
                prompt_text="Eres el paciente Juan..."
            )
            db.add(prompt)
            await db.flush()
            
        # Resolve/Create completion status
        stmt_comp = select(TrainingCompletionStatus).where(
            and_(
                TrainingCompletionStatus.training_report_id == report.training_report_id,
                TrainingCompletionStatus.simulation_prompt_id == prompt.simulation_prompt_id
            )
        ).limit(1)
        res_comp = await db.execute(stmt_comp)
        comp = res_comp.scalar()
        
        if not comp:
            comp = TrainingCompletionStatus(
                training_report_id=report.training_report_id,
                simulation_prompt_id=prompt.simulation_prompt_id,
                hubspot_owner_id=hubspot_owner_id,
                status="pending"
            )
            db.add(comp)
            await db.flush()
            
        # Create completed Call Session
        print("Creando sesion de llamada completed...")
        import uuid
        test_call_sid = f"test_call_sid_{uuid.uuid4().hex[:10]}"
        session = TrainingCallSession(
            call_sid=test_call_sid,
            agent_id=hubspot_owner_id,
            cycle_id=report.training_report_id,
            conversation_id=prompt.simulation_prompt_id,
            status="ended", # set to ended after hangup
            recording_url="http://api.twilio.com/recordings/RE999.wav"
        )
        db.add(session)
        await db.flush()
        
        session_id = session.session_id
        comp.call_session_id = session_id
        await db.commit()
        
    # Run evaluation task
    print(f"Disparando evaluate_training_session_task para session_id={session_id}...")
    await evaluate_training_session_task(session_id)
    
    # Check database updates
    async with AsyncSession(engine) as db:
        # Check session status
        stmt_sess = select(TrainingCallSession).where(TrainingCallSession.session_id == session_id)
        res_sess = await db.execute(stmt_sess)
        updated_session = res_sess.scalar()
        print(f"Estado de sesion actualizado: {updated_session.status} (esperado: 'evaluated')")
        assert updated_session.status == "evaluated"
        
        # Check completion status
        stmt_comp = select(TrainingCompletionStatus).where(
            and_(
                TrainingCompletionStatus.training_report_id == updated_session.cycle_id,
                TrainingCompletionStatus.simulation_prompt_id == updated_session.conversation_id
            )
        )
        res_comp = await db.execute(stmt_comp)
        updated_comp = res_comp.scalar()
        print(f"Estado de completion actualizado: {updated_comp.status} (esperado: 'completed')")
        assert updated_comp.status == "completed"
        
        # Check training evaluation score
        stmt_eval = select(TrainingCallEvaluation).where(TrainingCallEvaluation.session_id == session_id)
        res_eval = await db.execute(stmt_eval)
        eval_record = res_eval.scalar()
        print(f"Evaluacion guardada. Score: {eval_record.score} (esperado: 8.50)")
        assert float(eval_record.score) == 8.50
        
    print("[OK] Pipeline de evaluacion asincrona validado con exito.")

if __name__ == "__main__":
    asyncio.run(main())
