import os
import sys

# Override DATABASE_URL to use a test SQLite database before imports
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///trainer_test.db"

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from sqlalchemy import BigInteger

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select, delete

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine, SessionLocal, Base
from app.models.services import Service
from app.models.typologies import Typology
from app.models.prompts import Prompt, PromptVersion
from app.models.personalized_training import TrainingAgentSetting
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
    TrainerEvaluation,
)
from app.schemas.trainer import (
    TrainerEvaluationConfigCreate,
    TrainerSimulationCreate,
    TrainerSimulationUpdate,
    AIPromptGenerateRequest,
    AIPromptImproveRequest,
)
from app.services.trainer_service import TrainerService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class pytest:
    class raises:
        def __init__(self, expected_exception, match=None):
            self.expected_exception = expected_exception
            self.match = match
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                raise AssertionError(f"Expected {self.expected_exception} but no exception was raised.")
            if not issubclass(exc_type, self.expected_exception):
                return False
            if self.match and self.match not in str(exc_val):
                raise AssertionError(f"Exception message '{str(exc_val)}' did not match pattern '{self.match}'")
            return True


async def test_trainer_module_logic():
    print("Cleaning old test database file...")
    try:
        if os.path.exists("trainer_test.db"):
            os.remove("trainer_test.db")
    except Exception as e:
        print("Failed to remove old test DB file:", e)

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 1. Setup Mock OpenAI Services
    import app.services.trainer_service as ts
    
    class MockOpenAIService:
        async def complete_text(self, messages, response_format=None, model=None, temperature=0.2):
            if response_format == "json_object":
                import json
                return json.dumps({
                    "score": 8.5,
                    "feedback": "Excelente interacción con objeciones.",
                    "puntos_fuertes": {"empatias": "Muy empático al inicio"},
                    "puntos_mejora": {"cierre": "Podría presionar un poco más el reagendo"}
                })
            return "Prompt de roleplay generado por IA mockeada."

        async def transcribe_audio(self, audio_bytes, filename="audio.wav"):
            return {
                "text": "Hola, buenas tardes, llamo para pedir una cita médica con el doctor.",
                "model": "whisper-1",
                "provider": "openai"
            }

    # Replace modules
    ts.openai_service = MockOpenAIService()

    async with SessionLocal() as db:
        print("\n--- 1. SEED DATA SETUP ---")
        service = Service(service_key="clinica_srv", service_name="Clinica Service")
        db.add(service)
        await db.flush()

        prompt = Prompt(prompt_name="Clinica Eval Structure", prompt_type="audio", is_active=True, service_id=service.service_id)
        db.add(prompt)
        await db.flush()

        prompt_version = PromptVersion(prompt_id=prompt.prompt_id, prompt="Estructura base de evaluación de Clinica. Criterio score.", is_current=True)
        db.add(prompt_version)
        await db.flush()

        agent_setting = TrainingAgentSetting(
            hubspot_owner_id="agent_owner_1",
            agent_name="Fernanda Rodrigues",
            agent_initials="FR",
            training_code="FR45",
            training_numeric_code="1234",
            is_enabled=True,
            training_code_enabled=True,
        )
        db.add(agent_setting)
        await db.commit()
        print("[OK] Seed data created.")

        print("\n--- 2. CONFIGURATIONS CREATION ---")
        config_payload = TrainerEvaluationConfigCreate(
            name="Clinica Eval Config",
            service_id=service.service_id,
            speech_structure_id=prompt.prompt_id,
            extra_instructions="Foco especial en el saludo y despedida.",
            is_active=True
        )
        config = await TrainerService.create_evaluation_config(db, config_payload, created_by="admin@speech.com")
        assert config.config_id is not None
        assert config.name == "Clinica Eval Config"
        print("[OK] Evaluation Config created.")

        print("\n--- 3. SIMULATIONS STATES & VALIDATIONS ---")
        # Validate unique code
        sim_payload = TrainerSimulationCreate(
            name="Cita Clinica Inmersiva",
            code="SIM101",
            service_id=service.service_id,
            roleplay_prompt="Eres un paciente de 45 años con dolor de espalda.",
            evaluation_config_id=config.config_id,
            objective="Agendar cita",
            difficulty="media"
        )
        sim = await TrainerService.create_simulation(db, sim_payload, created_by="admin@speech.com")
        assert sim.simulation_id is not None
        assert sim.status == "draft"

        # Try to create another simulation with same code (should fail)
        with pytest.raises(ValueError, match="ya existe de manera global"):
            await TrainerService.create_simulation(db, sim_payload, created_by="admin@speech.com")

        # Try to publish simulation (OK)
        sim_pub = await TrainerService.publish_simulation(db, sim.simulation_id, user_email="admin@speech.com")
        assert sim_pub.status == "published"
        assert sim_pub.published_at is not None

        # Check version 1 generated
        stmt_v = select(TrainerSimulationVersion).where(TrainerSimulationVersion.simulation_id == sim.simulation_id)
        res_v = await db.execute(stmt_v)
        versions = list(res_v.scalars().all())
        assert len(versions) == 1
        assert versions[0].version_number == 1
        assert versions[0].roleplay_prompt_snapshot == "Eres un paciente de 45 años con dolor de espalda."
        print("[OK] Simulation created, published, and versioned.")

        print("\n--- 4. EDIT PUBLISHED SIMULATION (VERSIONING HISTORICITY) ---")
        # Edit published simulation prompt
        await TrainerService.update_simulation(
            db, sim.simulation_id, TrainerSimulationUpdate(roleplay_prompt="Eres un paciente de 50 años muy impaciente."), updated_by="admin@speech.com"
        )
        
        # Verify version 2 generated
        res_v2 = await db.execute(select(TrainerSimulationVersion).where(TrainerSimulationVersion.simulation_id == sim.simulation_id))
        versions2 = list(res_v2.scalars().all())
        assert len(versions2) == 2
        assert versions2[1].version_number == 2
        assert versions2[1].roleplay_prompt_snapshot == "Eres un paciente de 50 años muy impaciente."
        print("[OK] Editing published simulation successfully generated new version.")

        print("\n--- 5. PHONE WEBHOOKS VALIDATIONS ---")
        # Validate agent code
        agent_res = await TrainerService.validate_agent_code(db, "FR45")
        assert agent_res is not None
        assert agent_res["agent_id"] == "agent_owner_1"

        agent_fail = await TrainerService.validate_agent_code(db, "INVALID")
        assert agent_fail is None

        # Validate simulation code
        sim_res = await TrainerService.validate_simulation_code(db, "SIM101")
        assert sim_res is not None
        assert sim_res.simulation_id == sim.simulation_id

        sim_fail = await TrainerService.validate_simulation_code(db, "DRAFT_CODE")
        assert sim_fail is None
        print("[OK] Agent/simulation telephone validations verified.")

        print("\n--- 6. PHONE SESSION & BACKGROUND EVALUATION ---")
        # Start phone session
        session = await TrainerService.start_phone_session(
            db, agent_code="FR45", simulation_code="SIM101", call_id="twilio_call_sid_123"
        )
        assert session.session_id is not None
        assert session.status == "started"
        assert session.evaluation_status == "started"
        assert session.simulation_version_id == versions2[1].version_id # Points to version 2

        # Complete session (which triggers background evaluation)
        # Mock Twilio download
        from app.services.twilio_service import TwilioService
        async def mock_download_audio(self, url):
            return b"fake_bytes"
        TwilioService.download_audio = mock_download_audio
        
        await TrainerService.complete_phone_session(
            db,
            session_id=session.session_id,
            transcript=None,
            recording_url="http://twilio.com/recording.mp3",
            duration_seconds=120
        )

        # Wait a moment for background task to finish evaluation
        for _ in range(10):
            await db.refresh(session)
            if session.evaluation_status in ["evaluated", "evaluation_error", "failed"]:
                break
            await asyncio.sleep(0.5)

        assert session.status == "completed"
        assert session.evaluation_status == "evaluated", f"Expected evaluated status, got {session.evaluation_status}"

        # Fetch evaluation details
        stmt_ev = select(TrainerEvaluation).where(TrainerEvaluation.session_id == session.session_id)
        res_ev = await db.execute(stmt_ev)
        evaluation = res_ev.scalars().first()
        assert evaluation is not None
        assert evaluation.score == Decimal("8.5")
        assert evaluation.summary == "Excelente interacción con objeciones."
        print("[OK] Phone session complete and background evaluation verified.")

        print("\n--- 7. SESSIONS LIST & FILTERS ---")
        sessions, total = await TrainerService.list_sessions(
            db, agent_id="agent_owner_1", service_id=service.service_id, limit=10
        )
        assert total == 1
        assert sessions[0].session_id == session.session_id

        session_detail = await TrainerService.get_session_detail(db, session.session_id)
        assert session_detail.simulation.name == "Cita Clinica Inmersiva"
        assert session_detail.evaluation.evaluation_id == evaluation.evaluation_id
        print("[OK] Session detail and query filters verified.")

        # Cleanup
        print("\nCleaning up test trainer tables...")
        await db.execute(delete(TrainerEvaluation).where(TrainerEvaluation.session_id == session.session_id))
        await db.execute(delete(TrainerSession).where(TrainerSession.session_id == session.session_id))
        await db.execute(delete(TrainerSimulationVersion).where(TrainerSimulationVersion.simulation_id == sim.simulation_id))
        await db.execute(delete(TrainerSimulation).where(TrainerSimulation.simulation_id == sim.simulation_id))
        await db.execute(delete(TrainerEvaluationConfig).where(TrainerEvaluationConfig.config_id == config.config_id))
        await db.execute(delete(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == "agent_owner_1"))
        await db.execute(delete(PromptVersion).where(PromptVersion.prompt_id == prompt.prompt_id))
        await db.execute(delete(Prompt).where(Prompt.prompt_id == prompt.prompt_id))
        await db.execute(delete(Service).where(Service.service_id == service.service_id))
        await db.commit()
        print("=== TODAS LAS PRUEBAS DE TRAINER HAN PASADO CON EXITO ===")


if __name__ == "__main__":
    asyncio.run(test_trainer_module_logic())
