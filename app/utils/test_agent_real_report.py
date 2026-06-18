import sys
import os
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

# Add workspace directory to path
sys.path.append(".")

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.db import get_engine
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingCallEvaluation,
    TrainingEvaluationPrompt,
)
from app.utils.security import create_access_token, hash_password

async def test_agent_real_report_mapping():
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        print("=== INICIANDO PRUEBA DE REGRESIÓN DE INFORME REAL ===")
        
        # 1. Limpieza inicial por seguridad
        await db.execute(delete(TrainingCompletionStatus).where(TrainingCompletionStatus.hubspot_owner_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
        await db.execute(delete(TrainingCallEvaluation).where(TrainingCallEvaluation.agent_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
        await db.execute(delete(TrainingCallSession).where(TrainingCallSession.agent_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
        await db.execute(delete(TrainingSimulationPrompt).where(TrainingSimulationPrompt.hubspot_owner_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
        await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
        await db.execute(delete(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
        await db.execute(delete(User).where(User.username.in_(["real_agent_fernanda", "real_agent_other", "real_admin"])))
        await db.commit()
        
        # 2. Creación de usuarios
        fernanda = User(
            username="real_agent_fernanda",
            email="fernanda.test@bostonmedical.com",
            role="agente",
            hubspot_owner_id="real_agent_fernanda_123",
            password_hash=hash_password("testpass123"),
            is_active=True,
            agent_initials="FR"
        )
        other_agent = User(
            username="real_agent_other",
            email="other.test@bostonmedical.com",
            role="agente",
            hubspot_owner_id="real_agent_other_456",
            password_hash=hash_password("testpass123"),
            is_active=True,
            agent_initials="OA"
        )
        admin = User(
            username="real_admin",
            email="admin.test@bostonmedical.com",
            role="administrador",
            hubspot_owner_id=None,
            password_hash=hash_password("testpass123"),
            is_active=True,
            agent_initials="ADM"
        )
        db.add_all([fernanda, other_agent, admin])
        await db.commit()
        
        token_fernanda = create_access_token({"user_id": fernanda.user_id, "username": fernanda.username})
        token_other = create_access_token({"user_id": other_agent.user_id, "username": other_agent.username})
        token_admin = create_access_token({"user_id": admin.user_id, "username": admin.username})
        
        # 3. Configuraciones del agente
        setting_fernanda = TrainingAgentSetting(
            hubspot_owner_id="real_agent_fernanda_123",
            agent_name="Fernanda Rodrigues",
            agent_initials="FR",
            is_enabled=True
        )
        setting_other = TrainingAgentSetting(
            hubspot_owner_id="real_agent_other_456",
            agent_name="Other Agent",
            agent_initials="OA",
            is_enabled=True
        )
        db.add_all([setting_fernanda, setting_other])
        await db.commit()
        
        # 4. Estructuras reales anonimizadas
        gen_objectives = [
            {
                "title": "Objetivo General",
                "status": "no_superado",
                "base_score": 7.045,
                "description": "Perfeccionar el protocolo de saludo e identificación en llamadas salientes, asegurando el orden correcto y la personalización desde el inicio.",
                "success_indicators": ["Correcto orden de saludo"]
            },
            {
                "title": "Objetivo General",
                "status": "no_superado",
                "base_score": 7.045,
                "description": "Desarrollar habilidades avanzadas de empatía mediante el uso de preguntas abiertas y escucha activa para profundizar en las emociones y necesidades del paciente.",
                "success_indicators": ["Uso de preguntas abiertas"]
            },
            {
                "title": "Objetivo General",
                "status": "no_superado",
                "base_score": 7.045,
                "description": "Incrementar la frecuencia y calidad de la reformulación de la patología y el uso del nombre del paciente durante la llamada, reforzando la personalización y validación de la información.",
                "success_indicators": ["Uso del nombre del paciente"]
            }
        ]
        
        spec_objectives = [
            {
                "title": "Objetivo Específico",
                "status": "no_superado",
                "base_score": 7.045,
                "description": "Aplicar el protocolo de saludo correcto en el 100% de las llamadas salientes, preguntando primero por el paciente antes de identificarse con la empresa o el propio nombre.",
                "success_indicators": ["Saludo inicial Boston Medical"]
            },
            {
                "title": "Objetivo Específico",
                "status": "no_superado",
                "base_score": 7.045,
                "description": "Utilizar al menos tres preguntas abiertas en cada llamada para explorar el motivo de consulta, el contexto emocional y las expectativas del paciente.",
                "success_indicators": ["Mínimo tres preguntas abiertas"]
            },
            {
                "title": "Objetivo Específico",
                "status": "no_superado",
                "base_score": 7.045,
                "description": "Reformular explícitamente la patología y utilizar el nombre del paciente al menos una vez cada dos minutos durante la conversación.",
                "success_indicators": ["Reformulación de patología"]
            }
        ]
        
        final_report_json = {
            "strengths": ["Saludo inicial estructurado", "Empatía"],
            "weaknesses": ["Detallar explicaciones de Boston Medical Group"],
            "summary_final": "Fernanda Rodrigues ha demostrado una evolución positiva y sostenida a lo largo del ciclo de entrenamiento.",
            "objectives_status": [
                {
                    "type": "general",
                    "score": 8.25,
                    "title": "Objetivo General",
                    "status": "superado",
                    "base_score": 7.045,
                    "description": "Perfeccionar el protocolo de saludo e identificación en llamadas salientes, asegurando el orden correcto y la personalización desde el inicio.",
                    "justification": "Fernanda ha mejorado notablemente en la aplicación del protocolo de saludo, pasando de un score inicial de 7.05 a 8.25.",
                    "improvement_delta": 1.2
                },
                {
                    "type": "general",
                    "score": 8.25,
                    "title": "Objetivo General",
                    "status": "superado",
                    "base_score": 7.045,
                    "description": "Desarrollar habilidades avanzadas de empatía mediante el uso de preguntas abiertas y escucha activa para profundizar en las emociones y necesidades del paciente.",
                    "justification": "Se evidencia una evolución en la empatía y escucha activa, con una mejora de +1.20 puntos.",
                    "improvement_delta": 1.2
                },
                {
                    "type": "general",
                    "score": 8.25,
                    "title": "Objetivo General",
                    "status": "superado",
                    "base_score": 7.045,
                    "description": "Incrementar la frecuencia y calidad de la reformulación de la patología y el uso del nombre del paciente durante la llamada, reforzando la personalización y validación de la información.",
                    "justification": "Fernanda incrementó el uso del nombre del paciente y la reformulación.",
                    "improvement_delta": 1.2
                },
                {
                    "type": "especifico",
                    "score": 8.25,
                    "title": "Objetivo Específico",
                    "status": "superado",
                    "base_score": 7.045,
                    "description": "Aplicar el protocolo de saludo correcto en el 100% de las llamadas salientes, preguntando primero por el paciente antes de identificarse con la empresa o el propio nombre.",
                    "justification": "Fernanda ha demostrado consistencia en el protocolo de saludo.",
                    "improvement_delta": 1.2
                },
                {
                    "type": "especifico",
                    "score": 8.25,
                    "title": "Objetivo Específico",
                    "status": "superado",
                    "base_score": 7.045,
                    "description": "Utilizar al menos tres preguntas abiertas en cada llamada para explorar el motivo de consulta, el contexto emocional y las expectativas del paciente.",
                    "justification": "El agente ha incrementado el uso de preguntas abiertas.",
                    "improvement_delta": 1.2
                },
                {
                    "type": "especifico",
                    "score": 8.25,
                    "title": "Objetivo Específico",
                    "status": "superado",
                    "base_score": 7.045,
                    "description": "Reformular explícitamente la patología y utilizar el nombre del paciente al menos una vez cada dos minutos durante la conversación.",
                    "justification": "Fernanda ha demostrado un uso frecuente del nombre del paciente.",
                    "improvement_delta": 1.2
                }
            ]
        }
        
        report = TrainingAgentReport(
            hubspot_owner_id="real_agent_fernanda_123",
            agent_name="Fernanda Rodrigues",
            agent_initials="FR",
            period_start=datetime(2026, 5, 18, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 31, tzinfo=timezone.utc),
            status="completed",
            summary_general="Resumen antiguo que debe ser sobreescrito",
            general_objectives_json=gen_objectives,
            specific_objectives_json=spec_objectives,
            final_report_json=final_report_json,
            is_current=True,
            evaluations_count=4,
            calls_count=4
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        
        # 5. Creación de Simulation Prompts
        prompts_data = [
            (1, "Saludo y Protocolo Inicial en Llamada Saliente", "roleplay"),
            (2, "Profundización Empática y Uso de Preguntas Abiertas", "roleplay"),
            (3, "Reformulación de Patología y Personalización", "roleplay"),
            (4, "Gestión Compleja: Empatía, Reformulación y Protocolo Bajo Presión", "roleplay")
        ]
        prompts = []
        for num, title, stype in prompts_data:
            p = TrainingSimulationPrompt(
                training_report_id=report.training_report_id,
                hubspot_owner_id="real_agent_fernanda_123",
                prompt_number=num,
                title=title,
                scenario_type=stype,
                prompt_text=f"Instrucciones secretas del prompt {num}"
            )
            db.add(p)
            prompts.append(p)
        await db.commit()
        for p in prompts:
            await db.refresh(p)
            
        # 6. Evaluation prompts & Call Sessions
        eval_prompt = TrainingEvaluationPrompt(
            service_id=1,
            prompt_text="Prompt de evaluación real",
            version=1,
            is_active=True
        )
        db.add(eval_prompt)
        await db.commit()
        await db.refresh(eval_prompt)
        
        sessions = []
        for i, p in enumerate(prompts):
            s = TrainingCallSession(
                call_sid=f"real_call_sid_{p.simulation_prompt_id}",
                agent_id="real_agent_fernanda_123",
                cycle_id=report.training_report_id,
                conversation_id=p.simulation_prompt_id,
                status="evaluated"
            )
            db.add(s)
            sessions.append(s)
        await db.commit()
        for s in sessions:
            await db.refresh(s)
            
        # 7. Evaluations (using Boolean criteria for Sim 1 & Sim 2, list criteria for Sim 3 & Sim 4)
        evaluations = []
        
        # Sim 1 (Boolean criteria only)
        ev_1 = TrainingCallEvaluation(
            session_id=sessions[0].session_id,
            cycle_id=report.training_report_id,
            conversation_id=prompts[0].simulation_prompt_id,
            agent_id="real_agent_fernanda_123",
            prompt_version_id=eval_prompt.id,
            transcription="Agente: Hola\nPaciente: Buenas",
            result_json={
                "score": 8.5,
                "feedback": "Feedback real sim 1",
                "result_json": {
                    "agendar_cita": True,
                    "manejo_objeciones": True,
                    "objetivos_cumplidos": True,
                    "claridad_comunicacion": True,
                    "explicacion_servicios": True
                }
            },
            score=Decimal("8.50"),
            feedback="Feedback real sim 1"
        )
        
        # Sim 2 (Boolean criteria only)
        ev_2 = TrainingCallEvaluation(
            session_id=sessions[1].session_id,
            cycle_id=report.training_report_id,
            conversation_id=prompts[1].simulation_prompt_id,
            agent_id="real_agent_fernanda_123",
            prompt_version_id=eval_prompt.id,
            transcription="Agente: Hola\nPaciente: Buenas",
            result_json={
                "score": 8.5,
                "feedback": "Feedback real sim 2",
                "result_json": {
                    "empathy_shown": True,
                    "objectives_met": True,
                    "call_flow_followed": True,
                    "information_gathered": True,
                    "next_steps_explained": "partially"
                }
            },
            score=Decimal("8.50"),
            feedback="Feedback real sim 2"
        )
        
        # Sim 3 (With list criteria)
        ev_3 = TrainingCallEvaluation(
            session_id=sessions[2].session_id,
            cycle_id=report.training_report_id,
            conversation_id=prompts[2].simulation_prompt_id,
            agent_id="real_agent_fernanda_123",
            prompt_version_id=eval_prompt.id,
            transcription="Agente: Hola\nPaciente: Buenas",
            result_json={
                "score": 8.5,
                "feedback": "Feedback real sim 3",
                "result_json": {
                    "objectives_met": [
                        "Confirmación de información del paciente",
                        "Uso del nombre del paciente para una atención personalizada"
                    ]
                }
            },
            score=Decimal("8.50"),
            feedback="Feedback real sim 3"
        )
        
        # Sim 4 (With list criteria and boolean criteria)
        ev_4 = TrainingCallEvaluation(
            session_id=sessions[3].session_id,
            cycle_id=report.training_report_id,
            conversation_id=prompts[3].simulation_prompt_id,
            agent_id="real_agent_fernanda_123",
            prompt_version_id=eval_prompt.id,
            transcription="Agente: Hola\nPaciente: Buenas",
            result_json={
                "score": 7.5,
                "feedback": "Feedback real sim 4",
                "result_json": {
                    "objectives_met": [
                        "Muestra de empatía y escucha activa",
                        "Intento de aclarar el proceso"
                    ],
                    "provided_solutions": False
                }
            },
            score=Decimal("7.50"),
            feedback="Feedback real sim 4"
        )
        db.add_all([ev_1, ev_2, ev_3, ev_4])
        await db.commit()
        await db.refresh(ev_1)
        await db.refresh(ev_2)
        await db.refresh(ev_3)
        await db.refresh(ev_4)
        
        # 8. Completions
        comp_1 = TrainingCompletionStatus(
            training_report_id=report.training_report_id,
            simulation_prompt_id=prompts[0].simulation_prompt_id,
            hubspot_owner_id="real_agent_fernanda_123",
            status="completed",
            completed_at=datetime.now(timezone.utc),
            call_session_id=sessions[0].session_id,
            evaluation_id=ev_1.evaluation_id
        )
        comp_2 = TrainingCompletionStatus(
            training_report_id=report.training_report_id,
            simulation_prompt_id=prompts[1].simulation_prompt_id,
            hubspot_owner_id="real_agent_fernanda_123",
            status="completed",
            completed_at=datetime.now(timezone.utc),
            call_session_id=sessions[1].session_id,
            evaluation_id=ev_2.evaluation_id
        )
        comp_3 = TrainingCompletionStatus(
            training_report_id=report.training_report_id,
            simulation_prompt_id=prompts[2].simulation_prompt_id,
            hubspot_owner_id="real_agent_fernanda_123",
            status="completed",
            completed_at=datetime.now(timezone.utc),
            call_session_id=sessions[2].session_id,
            evaluation_id=ev_3.evaluation_id
        )
        comp_4 = TrainingCompletionStatus(
            training_report_id=report.training_report_id,
            simulation_prompt_id=prompts[3].simulation_prompt_id,
            hubspot_owner_id="real_agent_fernanda_123",
            status="completed",
            completed_at=datetime.now(timezone.utc),
            call_session_id=sessions[3].session_id,
            evaluation_id=ev_4.evaluation_id
        )
        db.add_all([comp_1, comp_2, comp_3, comp_4])
        await db.commit()

        # 9. HTTP Assertions
        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            try:
                # PRUEBA A: Agente consulta su informe específico por ID
                headers_f = {"Authorization": f"Bearer {token_fernanda}"}
                res = await client.get(f"/bm/me/analysis-results?training_report_id={report.training_report_id}", headers=headers_f)
                assert res.status_code == 200, f"Error: {res.status_code} - {res.text}"
                data = res.json()
                
                # A.1. Ningún objetivo evaluado aparece pendiente y se devuelven correctamente
                assert len(data["general_objectives_json"]) == 3
                assert len(data["specific_objectives_json"]) == 3
                
                for obj in data["general_objectives_json"] + data["specific_objectives_json"]:
                    assert obj["status"] in ["SUPERADO", "NO SUPERADO"]
                    assert obj["is_evaluated"] is True
                    assert obj["score"] == 8.25
                    assert obj["base_score"] == 7.045
                    assert obj["improvement_delta"] == 1.2
                    assert obj["justification"] is not None
                    assert obj["evaluated_at"] is not None

                print("  -> Objetivos validados correctamente.")

                # A.2. Se devuelven 4 simulaciones y todas están completadas
                simulations = data["simulations"]
                assert len(simulations) == 4
                assert data["progress_completed"] == 4
                assert data["progress_total"] == 4
                assert float(data["progress_percentage"]) == 100.0
                
                for i, sim in enumerate(simulations):
                    assert sim["status"] == "completed"
                    assert sim["evaluation_id"] is not None
                    assert sim["score"] is not None
                    assert sim["feedback"] is not None
                    assert sim["criteria"] is not None
                    assert sim["transcription_turns"] is not None
                    
                    # Check fallback for strengths/weaknesses (met/missed criteria mapping)
                    if sim["prompt_number"] == 1:
                        assert "Agendar cita" in sim["strengths"]
                        assert "Manejo de objeciones" in sim["strengths"]
                    elif sim["prompt_number"] == 2:
                        assert "Empatía" in sim["strengths"]
                        assert "Flujo de llamada" in sim["strengths"]
                    elif sim["prompt_number"] == 3:
                        assert "Confirmación de información del paciente" in sim["strengths"]
                    elif sim["prompt_number"] == 4:
                        assert "Muestra de empatía y escucha activa" in sim["strengths"]
                        assert "Provided solutions" in sim["weaknesses"]

                print("  -> Simulaciones validadas correctamente.")

                # A.3. El agente NO puede consultar informes de otros agentes (403)
                headers_other = {"Authorization": f"Bearer {token_other}"}
                res_403 = await client.get(f"/bm/me/analysis-results?training_report_id={report.training_report_id}", headers=headers_other)
                assert res_403.status_code == 403, f"Se esperaba 403, obtenido: {res_403.status_code}"
                assert "No tienes permisos" in res_403.json()["detail"]
                print("  -> Seguridad 403 validada correctamente.")
                
                # A.4. Sanitización: El prompt_text debe venir vacío para el agente
                for p in data["prompts"]:
                    assert p["prompt_text"] == "", "El prompt_text no fue sanitizado para el agente!"
                print("  -> Sanitización de prompts validada correctamente.")
                
                print("=== ¡PRUEBA DE REGRESIÓN DE INFORME REAL COMPLETADA CON ÉXITO! ===")

            finally:
                # 10. Limpieza final de datos de prueba
                print("Limpiando base de datos...")
                await db.execute(delete(TrainingCompletionStatus).where(TrainingCompletionStatus.hubspot_owner_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
                await db.execute(delete(TrainingCallEvaluation).where(TrainingCallEvaluation.agent_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
                await db.execute(delete(TrainingCallSession).where(TrainingCallSession.agent_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
                await db.execute(delete(TrainingSimulationPrompt).where(TrainingSimulationPrompt.hubspot_owner_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
                await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
                await db.execute(delete(TrainingEvaluationPrompt).where(TrainingEvaluationPrompt.id == eval_prompt.id))
                await db.execute(delete(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id.in_(["real_agent_fernanda_123", "real_agent_other_456"])))
                await db.execute(delete(User).where(User.username.in_(["real_agent_fernanda", "real_agent_other", "real_admin"])))
                await db.commit()
                print("Base de datos limpia.")

if __name__ == "__main__":
    asyncio.run(test_agent_real_report_mapping())
