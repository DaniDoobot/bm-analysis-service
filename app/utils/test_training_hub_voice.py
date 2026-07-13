import os
import sys
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///training_hub_voice_test.db"
os.environ["GEMINI_API_KEY"] = "mock_key"

# Safety Confirmation Check
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution was blocked because DATABASE_URL points to production!")

# Setup path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# SQLite Type Compilers for Compatibility
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.db import get_engine, Base
from app.models.personalized_training import TrainingAgentSetting, TrainingAgentReport, TrainingSimulationPrompt, TrainingCompletionStatus
from app.models.trainer import TrainerSimulation, TrainerEvaluationConfig
from app.services.trainer_service import TrainerService
from sqlalchemy import text, delete
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Request


class TestTrainingHubVoice(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"
        
        # Clean old DB file if exists
        if os.path.exists("training_hub_voice_test.db"):
            try:
                os.remove("training_hub_voice_test.db")
            except Exception:
                pass

        # Create all tables in SQLite
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA busy_timeout=5000;"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
            # Create attempts table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS bm_training_call_attempts (
                    call_sid TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """))

        # Setup mock active agent
        async with AsyncSession(engine) as db:
            agent = TrainingAgentSetting(
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                is_enabled=True,
                training_code="CM77",
                training_numeric_code="7777",
                training_code_enabled=True,
            )
            db.add(agent)
            
            # Setup a mock simulation
            sim = TrainerSimulation(
                name="Mock Simulation",
                code="SIM101",
                service_id=1,
                roleplay_prompt="Test roleplay prompt",
                objective="Agendar cita",
                status="published",
            )
            db.add(sim)
            await db.commit()

    async def asyncTearDown(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        if os.path.exists("training_hub_voice_test.db"):
            try:
                os.remove("training_hub_voice_test.db")
            except Exception:
                pass

    def mock_request(self, form_data=None, headers=None):
        req = MagicMock(spec=Request)
        async def mock_form():
            return form_data or {}
        req.form = mock_form
        req.headers = headers or {"host": "localhost"}
        return req

    @patch("app.routers.training_hub_voice.settings")
    async def test_incoming_call(self, mock_settings):
        mock_settings.gemini_api_key = "mock_key"
        mock_settings.gemini_live_api_key = None
        from app.routers.training_hub_voice import incoming_call
        req = self.mock_request(headers={"host": "test-host.com"})
        resp = await incoming_call(req)
        body = resp.body.decode("utf-8")
        self.assertIn("wss://test-host.com/bm/training/hub/media-stream", body)
        self.assertIn('<Parameter name="flow" value="hub"', body)

    @patch("app.routers.training_hub_voice.settings")
    async def test_incoming_call_missing_api_key(self, mock_settings):
        mock_settings.gemini_api_key = None
        mock_settings.gemini_live_api_key = None
        from app.routers.training_hub_voice import incoming_call
        req = self.mock_request(headers={"host": "test-host.com"})
        resp = await incoming_call(req)
        body = resp.body.decode("utf-8")
        self.assertIn("clave de API de Gemini no está configurada", body)
        self.assertIn("<Hangup/>", body)

    async def test_collect_agent_dtmf(self):
        from app.routers.training_hub_voice import collect_agent_dtmf
        req = self.mock_request()
        resp = await collect_agent_dtmf(req, call_sid="call_123")
        body = resp.body.decode("utf-8")
        self.assertIn("/bm/training/hub/verify-agent-dtmf?call_sid=call_123", body)
        self.assertIn("No he podido identificar tu código por voz", body)

    async def test_verify_agent_dtmf_success(self):
        from app.routers.training_hub_voice import verify_agent_dtmf
        engine = get_engine()
        async with AsyncSession(engine) as db:
            req = self.mock_request(form_data={"Digits": "7777"}, headers={"host": "test-host.com"})
            resp = await verify_agent_dtmf(req, call_sid="call_123", db=db)
            body = resp.body.decode("utf-8")
            self.assertIn("/bm/training/hub/select-mode-menu?agent_id=7777&amp;call_sid=call_123", body)

    async def test_verify_agent_dtmf_failure(self):
        from app.routers.training_hub_voice import verify_agent_dtmf
        engine = get_engine()
        async with AsyncSession(engine) as db:
            req = self.mock_request(form_data={"Digits": "9999"})
            resp = await verify_agent_dtmf(req, call_sid="call_123", db=db)
            body = resp.body.decode("utf-8")
            self.assertIn("Código de agente incorrecto", body)
            self.assertIn("<Hangup/>", body)

    async def test_select_mode_menu(self):
        from app.routers.training_hub_voice import select_mode_menu
        req = self.mock_request()
        resp = await select_mode_menu(req, agent_id="7777", call_sid="call_123")
        body = resp.body.decode("utf-8")
        self.assertIn("/bm/training/hub/verify-mode-dtmf?agent_id=7777&amp;call_sid=call_123", body)
        self.assertIn("Pulsa 1 para Trainer o pulsa 2 para continuar", body)

    async def test_verify_mode_dtmf_trainer(self):
        from app.routers.training_hub_voice import verify_mode_dtmf
        req = self.mock_request(form_data={"Digits": "1"})
        resp = await verify_mode_dtmf(req, agent_id="7777", call_sid="call_123")
        body = resp.body.decode("utf-8")
        self.assertIn("/bm/training/hub/trainer-init?agent_id=7777&amp;call_sid=call_123", body)

    async def test_verify_mode_dtmf_cycles(self):
        from app.routers.training_hub_voice import verify_mode_dtmf
        req = self.mock_request(form_data={"Digits": "2"})
        resp = await verify_mode_dtmf(req, agent_id="7777", call_sid="call_123")
        body = resp.body.decode("utf-8")
        self.assertIn("/bm/training/hub/cycles-init?agent_id=7777&amp;call_sid=call_123", body)

    async def test_verify_mode_dtmf_invalid(self):
        from app.routers.training_hub_voice import verify_mode_dtmf
        req = self.mock_request(form_data={"Digits": "9"})
        resp = await verify_mode_dtmf(req, agent_id="7777", call_sid="call_123")
        body = resp.body.decode("utf-8")
        self.assertIn("Opción no válida", body)
        self.assertIn("/bm/training/hub/select-mode-menu?agent_id=7777&amp;call_sid=call_123", body)

    async def test_trainer_init(self):
        from app.routers.training_hub_voice import trainer_init
        req = self.mock_request(headers={"host": "test-host.com"})
        resp = await trainer_init(req, agent_id="7777", call_sid="call_123")
        body = resp.body.decode("utf-8")
        self.assertIn("wss://test-host.com/bm/training/hub/media-stream", body)
        self.assertIn('<Parameter name="flow" value="trainer_code"', body)
        self.assertIn('<Parameter name="agent_id" value="7777"', body)

    async def test_collect_simulation_dtmf(self):
        from app.routers.training_hub_voice import collect_simulation_dtmf
        req = self.mock_request()
        resp = await collect_simulation_dtmf(req, agent_id="7777", call_sid="call_123")
        body = resp.body.decode("utf-8")
        self.assertIn("/bm/training/hub/verify-simulation-dtmf?agent_id=7777&amp;call_sid=call_123", body)
        self.assertIn("introduce el código numérico de la simulación", body)

    async def test_verify_simulation_dtmf_success(self):
        from app.routers.training_hub_voice import verify_simulation_dtmf
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # We mock redirect_trainer_call to avoid making real HTTP calls to Twilio
            with patch("app.routers.training_hub_voice.redirect_trainer_call", new_callable=AsyncMock) as mock_redirect:
                req = self.mock_request(form_data={"Digits": "SIM101"}, headers={"host": "test-host.com"})
                resp = await verify_simulation_dtmf(req, agent_id="7777", call_sid="call_123", db=db)
                body = resp.body.decode("utf-8")
                self.assertIn("Código verificado. Iniciando simulación", body)
                mock_redirect.assert_called_once_with("call_123", "test-host.com", "7777", 1)

    async def test_verify_simulation_dtmf_failure(self):
        from app.routers.training_hub_voice import verify_simulation_dtmf
        engine = get_engine()
        async with AsyncSession(engine) as db:
            req = self.mock_request(form_data={"Digits": "INVALID"})
            resp = await verify_simulation_dtmf(req, agent_id="7777", call_sid="call_123", db=db)
            body = resp.body.decode("utf-8")
            self.assertIn("Código de simulación incorrecto", body)
            self.assertIn("<Hangup/>", body)

    async def test_cycles_init_no_active(self):
        from app.routers.training_hub_voice import cycles_init
        engine = get_engine()
        async with AsyncSession(engine) as db:
            req = self.mock_request()
            resp = await cycles_init(req, agent_id="7777", call_sid="call_123", db=db)
            body = resp.body.decode("utf-8")
            self.assertIn("/bm/training/hub/no-active-cycles", body)

    async def test_cycles_init_single_active(self):
        from app.routers.training_hub_voice import cycles_init
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # Seed a single active cycle
            cycle = TrainingAgentReport(
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                period_start=datetime.now(),
                period_end=datetime.now(),
                status="pending",
                is_current=True,
            )
            db.add(cycle)
            await db.flush()

            # Seed simulation prompt
            sim_prompt = TrainingSimulationPrompt(
                training_report_id=cycle.training_report_id,
                hubspot_owner_id="7777",
                prompt_number=1,
                title="Mock Prompt Title",
                scenario_type="audio",
                prompt_text="Test prompt text",
            )
            db.add(sim_prompt)
            await db.flush()

            # Seed completion status
            comp_status = TrainingCompletionStatus(
                training_report_id=cycle.training_report_id,
                simulation_prompt_id=sim_prompt.simulation_prompt_id,
                hubspot_owner_id="7777",
                status="pending",
            )
            db.add(comp_status)
            await db.commit()
            
            with patch("app.routers.training_hub_voice.redirect_twilio_call", new_callable=AsyncMock) as mock_redirect:
                req = self.mock_request(headers={"host": "test-host.com"})
                resp = await cycles_init(req, agent_id="7777", call_sid="call_123", db=db)
                body = resp.body.decode("utf-8")
                self.assertIn("Código verificado. Iniciando entrenamiento", body)
                mock_redirect.assert_called_once_with("call_123", "test-host.com", "7777", cycle.training_report_id)

    async def test_cycles_init_multiple_active(self):
        from app.routers.training_hub_voice import cycles_init
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # Seed two active cycles
            cycle1 = TrainingAgentReport(
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                period_start=datetime.now(),
                period_end=datetime.now(),
                status="pending",
                is_current=True,
            )
            cycle2 = TrainingAgentReport(
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                period_start=datetime.now(),
                period_end=datetime.now(),
                status="pending",
                is_current=True,
            )
            db.add_all([cycle1, cycle2])
            await db.flush()

            # Seed simulation prompts and completion statuses for both cycles
            sp1 = TrainingSimulationPrompt(
                training_report_id=cycle1.training_report_id,
                hubspot_owner_id="7777",
                prompt_number=1,
                title="P1",
                scenario_type="audio",
                prompt_text="Prompt 1",
            )
            sp2 = TrainingSimulationPrompt(
                training_report_id=cycle2.training_report_id,
                hubspot_owner_id="7777",
                prompt_number=1,
                title="P2",
                scenario_type="audio",
                prompt_text="Prompt 2",
            )
            db.add_all([sp1, sp2])
            await db.flush()

            cs1 = TrainingCompletionStatus(
                training_report_id=cycle1.training_report_id,
                simulation_prompt_id=sp1.simulation_prompt_id,
                hubspot_owner_id="7777",
                status="pending",
            )
            cs2 = TrainingCompletionStatus(
                training_report_id=cycle2.training_report_id,
                simulation_prompt_id=sp2.simulation_prompt_id,
                hubspot_owner_id="7777",
                status="pending",
            )
            db.add_all([cs1, cs2])
            await db.commit()
            
            req = self.mock_request(headers={"host": "test-host.com"})
            resp = await cycles_init(req, agent_id="7777", call_sid="call_123", db=db)
            body = resp.body.decode("utf-8")
            self.assertIn("/bm/training/voice/twilio/select-cycle-menu?agent_id=7777&amp;call_sid=call_123", body)

    @patch("app.routers.training_hub_voice.settings")
    async def test_media_stream_websocket_connect(self, mock_settings):
        from fastapi.testclient import TestClient
        from app.main import app
        import json
        import asyncio
        import time
        
        mock_settings.gemini_api_key = "mock_key"
        mock_settings.gemini_live_api_key = None
        mock_settings.gemini_model = "models/gemini-3.1-flash-live-preview"
        mock_settings.gemini_live_model = None
        
        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()
        
        async def mock_async_iter(*args, **kwargs):
            yield json.dumps({"setupComplete": {}})
            await asyncio.sleep(0.5)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect) as mock_websockets_connect:
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=hub") as websocket:
                websocket.send_json({"event": "connected"})
                websocket.send_json({
                    "event": "start",
                    "start": {
                        "streamSid": "stream_123",
                        "callSid": "call_ws_test_hub",
                    }
                })
                # Send a mock base64 ulaw media payload
                websocket.send_json({
                    "event": "media",
                    "media": {
                        "payload": "f39/f39/f39/f39/"
                    }
                })
                time.sleep(0.5)

            # Verify websockets.connect call arguments
            mock_websockets_connect.assert_called_once()
            called_url = mock_websockets_connect.call_args[0][0]
            self.assertIn("v1beta", called_url)
            self.assertIn("key=mock_key", called_url)
            
            # Verify the model passed to setup message
            send_calls = mock_gemini_ws.send.call_args_list
            setup_payload = None
            audio_payload = None
            
            for call in send_calls:
                payload_str = call[0][0]
                payload = json.loads(payload_str)
                if "setup" in payload:
                    setup_payload = payload["setup"]
                elif "realtimeInput" in payload:
                    realtime_input = payload["realtimeInput"]
                    self.assertNotIn("mediaChunks", realtime_input)
                    self.assertNotIn("media_chunks", realtime_input)
                    if "audio" in realtime_input:
                        audio_payload = realtime_input["audio"]
                        
            self.assertIsNotNone(setup_payload)
            self.assertEqual(setup_payload["model"], "models/gemini-3.1-flash-live-preview")
            
            self.assertIsNotNone(audio_payload)
            self.assertEqual(audio_payload["mimeType"], "audio/pcm;rate=16000")
            self.assertTrue(len(audio_payload["data"]) > 0)


if __name__ == "__main__":
    unittest.main()
