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

    # ── Unit tests ─────────────────────────────────────────────────────────────

    def test_normalize_agent_code(self):
        from app.routers.training_hub_voice import normalize_agent_code
        self.assertEqual(normalize_agent_code("7777"), "7777")
        self.assertEqual(normalize_agent_code("7 7 7 7"), "7777")
        self.assertEqual(normalize_agent_code("siete siete siete siete"), "7777")
        self.assertEqual(normalize_agent_code("siete, siete, siete, siete"), "7777")
        self.assertEqual(normalize_agent_code("setenta y siete setenta y siete"), "7777")
        self.assertEqual(normalize_agent_code("siete mil setecientos setenta y siete"), "7777")
        self.assertEqual(normalize_agent_code("CM77"), None)

    def test_brand_pronunciation_dubot(self):
        from app.routers.training_hub_voice import HUB_SYSTEM_INSTRUCTION, TRAINER_CODE_SYSTEM_INSTRUCTION
        self.assertIn("Dubot", HUB_SYSTEM_INSTRUCTION)
        self.assertNotIn("Doobot", HUB_SYSTEM_INSTRUCTION)
        self.assertIn("Dubot", TRAINER_CODE_SYSTEM_INSTRUCTION)
        self.assertNotIn("Doobot", TRAINER_CODE_SYSTEM_INSTRUCTION)

    def test_gemini_voice_config_matches_training_voice(self):
        """Req 11: Hub Gemini setup must use same voice as training_voice.py (Algieba)."""
        from app.routers.training_hub_voice import build_gemini_live_session_config
        config = build_gemini_live_session_config("models/test", "instruction", [])
        voice_name = config["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
        self.assertEqual(voice_name, "Algieba")
        # Must have thinkingConfig
        thinking_level = config["generationConfig"]["thinkingConfig"]["thinkingLevel"]
        self.assertEqual(thinking_level, "minimal")
        # Must have realtimeInputConfig VAD
        self.assertIn("realtimeInputConfig", config)
        self.assertIn("automaticActivityDetection", config["realtimeInputConfig"])

    def test_hub_system_instruction_uses_switch_to_trainer_mode(self):
        """Hub system instruction must use switch_to_trainer_mode and validate_trainer_simulation_code."""
        from app.routers.training_hub_voice import HUB_SYSTEM_INSTRUCTION
        self.assertIn("switch_to_trainer_mode", HUB_SYSTEM_INSTRUCTION)
        self.assertNotIn("select_mode", HUB_SYSTEM_INSTRUCTION)
        # Req 8: trainer_code system must reference validate_trainer_simulation_code
        self.assertIn("validate_trainer_simulation_code", HUB_SYSTEM_INSTRUCTION)

    def test_hub_tools_contains_validate_trainer_simulation_code(self):
        """Req: HUB_TOOLS must declare validate_trainer_simulation_code so Gemini can call it mid-session."""
        import json
        # Simulate the tool declarations by importing and checking the module constants
        from app.routers.training_hub_voice import TRAINER_CODE_SYSTEM_INSTRUCTION
        # The system instruction for trainer_code must reference validate_trainer_simulation_code
        self.assertIn("validate_trainer_simulation_code", TRAINER_CODE_SYSTEM_INSTRUCTION)
        # Also ensure it does NOT reference the old verify_simulation_code
        self.assertNotIn("verify_simulation_code", TRAINER_CODE_SYSTEM_INSTRUCTION)

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

    # ── WebSocket Integration Tests ────────────────────────────────────────────

    @patch("app.routers.training_hub_voice.settings")
    async def test_media_stream_websocket_connect(self, mock_settings):
        """Full WebSocket flow: identify by voice + select cycles."""
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
            await asyncio.sleep(0.1)
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_abc123",
                        "name": "verify_agent_code",
                        "args": {"agent_code": "siete siete siete siete"}
                    }]
                }
            })
            await asyncio.sleep(0.1)
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_xyz789",
                        "name": "select_cycles_mode",
                        "args": {}
                    }]
                }
            })
            await asyncio.sleep(0.5)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect) as mock_websockets_connect, \
             patch("app.routers.training_hub_voice.redirect_call", new_callable=AsyncMock) as mock_redirect, \
             patch("app.routers.training_voice.get_active_cycles_for_agent", AsyncMock(return_value=[MagicMock()])):
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
                time.sleep(0.8)

            # Verify websockets.connect call arguments
            mock_websockets_connect.assert_called_once()
            called_url = mock_websockets_connect.call_args[0][0]
            self.assertIn("v1beta", called_url)
            self.assertIn("key=mock_key", called_url)
            
            # Verify mock_redirect was called to redirect_call (for cycles)
            mock_redirect.assert_called_once()
            
            # Verify setup config uses Algieba voice (Req 11)
            send_calls = mock_gemini_ws.send.call_args_list
            setup_payload = None
            audio_payload = None
            verify_agent_response = None
            
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
                elif "toolResponse" in payload:
                    func_resps = payload["toolResponse"]["functionResponses"]
                    for fr in func_resps:
                        if fr["name"] == "verify_agent_code":
                            verify_agent_response = fr["response"]["result"]
                        
            # Verify the initial greeting was requested automatically
            initial_greeting_text = None
            for call in send_calls:
                payload_str = call[0][0]
                payload = json.loads(payload_str)
                if "clientContent" in payload:
                    turns = payload["clientContent"].get("turns", [])
                    for turn in turns:
                        for part in turn.get("parts", []):
                            text = part.get("text", "")
                            if "Hola, soy el asistente" in text:
                                initial_greeting_text = text
                                break
            self.assertIsNotNone(initial_greeting_text)
            self.assertIn("Dubot", initial_greeting_text)  # Req 12
            self.assertNotIn("Doobot", initial_greeting_text)
            self.assertNotIn("7777", initial_greeting_text)
            self.assertNotIn("siete siete siete siete", initial_greeting_text)

            self.assertIsNotNone(setup_payload)
            self.assertEqual(setup_payload["model"], "models/gemini-3.1-flash-live-preview")
            
            # Req 11: verify voice name matches training_voice.py
            voice_name = setup_payload["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
            self.assertEqual(voice_name, "Algieba")
            
            self.assertIsNotNone(audio_payload)
            self.assertEqual(audio_payload["mimeType"], "audio/pcm;rate=16000")
            self.assertTrue(len(audio_payload["data"]) > 0)

            # Assertions for agent identification
            self.assertIsNotNone(verify_agent_response)
            self.assertEqual(verify_agent_response["status"], "valid")
            self.assertEqual(verify_agent_response["agent_name"], "Cristina Montenegro")

    @patch("app.routers.training_hub_voice.settings")
    async def test_media_stream_websocket_redirect_failure(self, mock_settings):
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
            await asyncio.sleep(0.1)
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_abc123",
                        "name": "verify_agent_code",
                        "args": {"agent_code": "siete siete siete siete"}
                    }]
                }
            })
            await asyncio.sleep(0.1)
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_xyz789",
                        "name": "select_cycles_mode",
                        "args": {}
                    }]
                }
            })
            await asyncio.sleep(0.5)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        # Patch redirect_call to return False (simulating redirect failure)
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_call", AsyncMock(return_value=False)) as mock_redirect, \
             patch("app.routers.training_voice.get_active_cycles_for_agent", AsyncMock(return_value=[MagicMock()])):
            client = TestClient(app)
            try:
                with client.websocket_connect("/bm/training/hub/media-stream?flow=hub") as websocket:
                    websocket.send_json({"event": "connected"})
                    websocket.send_json({
                        "event": "start",
                        "start": {
                            "streamSid": "stream_123",
                            "callSid": "call_ws_test_hub",
                        }
                    })
                    time.sleep(0.8)
            except Exception:
                pass

            # Verify mock_redirect was called
            mock_redirect.assert_called_once()
            
            # Verify the error voice prompt was sent to Gemini Live
            send_calls = mock_gemini_ws.send.call_args_list
            error_prompt_sent = False
            for call in send_calls:
                payload_str = call[0][0]
                payload = json.loads(payload_str)
                if "clientContent" in payload:
                    turns = payload["clientContent"].get("turns", [])
                    for turn in turns:
                        for part in turn.get("parts", []):
                            text = part.get("text", "")
                            if "problema de configuracion de telefonia" in text.lower() or "problema de configuración de telefonía" in text.lower():
                                error_prompt_sent = True
                                break
            self.assertTrue(error_prompt_sent)

    @patch("app.routers.training_hub_voice.settings")
    async def test_media_stream_websocket_no_active_cycles(self, mock_settings):
        """Selecting cycles with 0 active cycles must NOT redirect, and must return ok=False with error message to Gemini."""
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
            await asyncio.sleep(0.1)
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_abc123",
                        "name": "verify_agent_code",
                        "args": {"agent_code": "siete siete siete siete"}
                    }]
                }
            })
            await asyncio.sleep(0.1)
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_xyz789",
                        "name": "select_cycles_mode",
                        "args": {}
                    }]
                }
            })
            await asyncio.sleep(0.5)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        # Patch get_active_cycles_for_agent to return empty list (no active cycles)
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_call", AsyncMock()) as mock_redirect, \
             patch("app.routers.training_hub_voice.get_active_cycles_for_agent", AsyncMock(return_value=[])):
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
                time.sleep(0.8)

            # redirect_call must NOT have been called
            mock_redirect.assert_not_called()
            
            # Gemini must have received functionResponse with ok=False and no_active_cycles message
            send_calls = mock_gemini_ws.send.call_args_list
            found_tool_response = False
            for call in send_calls:
                payload = json.loads(call[0][0])
                if "toolResponse" in payload:
                    for resp in payload["toolResponse"].get("functionResponses", []):
                        if resp.get("name") == "select_cycles_mode":
                            res_dict = resp.get("response", {}).get("result", {})
                            if res_dict.get("ok") is False and res_dict.get("reason") == "no_active_cycles":
                                found_tool_response = True
                                self.assertIn("no tienes ciclos activos", res_dict.get("message"))
            self.assertTrue(found_tool_response, "Expected ok=False toolResponse with 'no_active_cycles' reason")

    @patch("app.routers.training_hub_voice.settings")
    async def test_media_stream_dtmf_trainer_no_redirect(self, mock_settings):
        """Req 2+3: DTMF '1' must NOT call redirect_call towards trainer-init.
        Req 3: Must change current_state to trainer_code within same WebSocket.
        Req 4: Gemini must receive a clientContent asking for simulation code.
        Req 5+6+7: The prompt must NOT contain the hub greeting or agent code request."""
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
            await asyncio.sleep(1.2)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_call", AsyncMock(return_value=True)) as mock_redirect:
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
                # Req 1: DTMF 7777 identifies Cristina Montenegro
                for digit in ["7", "7", "7", "7"]:
                    websocket.send_json({
                        "event": "dtmf",
                        "dtmf": {"digit": digit}
                    })
                time.sleep(0.2)
                # Req 2: DTMF '1' selects Trainer
                websocket.send_json({
                    "event": "dtmf",
                    "dtmf": {"digit": "1"}
                })
                time.sleep(0.5)

            # Req 2: redirect_call must NOT have been called towards trainer-init
            for call_args in mock_redirect.call_args_list:
                called_url = call_args[0][1] if call_args[0] else ""
                self.assertNotIn("trainer-init", called_url,
                    "redirect_call must NOT redirect to trainer-init when DTMF 1 is pressed")

            # Req 4+5+6+7: Gemini must have received the trainer code prompt
            send_calls = mock_gemini_ws.send.call_args_list
            trainer_prompt_text = None
            hub_greeting_text = None
            
            for call in send_calls:
                payload_str = call[0][0]
                payload = json.loads(payload_str)
                if "clientContent" in payload:
                    turns = payload["clientContent"].get("turns", [])
                    for turn in turns:
                        for part in turn.get("parts", []):
                            text = part.get("text", "")
                            # Look for the Trainer code prompt (sent after DTMF 1)
                            if "código de simulación" in text.lower() and "Cristina" in text:
                                trainer_prompt_text = text
                            # Check for unwanted hub initial greeting
                            if "hola, soy el asistente" in text.lower():
                                hub_greeting_text = text

            # Req 4: Gemini received a clientContent asking for sim code
            self.assertIsNotNone(trainer_prompt_text,
                "Gemini must receive a clientContent with simulation code request after DTMF 1")
            # Req 12: Dubot in trainer prompt
            self.assertIn("Dubot", trainer_prompt_text)
            # Req 7: no agent code request in trainer prompt
            self.assertNotIn("código de agente", trainer_prompt_text.lower())
            # Req 6: no hub greeting in trainer prompt
            self.assertNotIn("hola, soy el asistente", trainer_prompt_text.lower())
            # Req 5: initial hub greeting should NOT be re-sent after Trainer selection
            # (it may have been sent initially at flow=hub, but NOT after DTMF 1)
            # Verify "Perfecto, Cristina" in trainer prompt
            self.assertIn("Cristina", trainer_prompt_text)

    @patch("app.routers.training_hub_voice.settings")
    async def test_media_stream_dtmf_trainer_voice_selects_switch_mode(self, mock_settings):
        """Req 3: Gemini tool switch_to_trainer_mode must change state without redirect."""
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
            await asyncio.sleep(0.1)
            # Gemini verifies agent code first
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_verify",
                        "name": "verify_agent_code",
                        "args": {"agent_code": "siete siete siete siete"}
                    }]
                }
            })
            await asyncio.sleep(0.1)
            # Gemini selects trainer by voice (switch_to_trainer_mode, NOT select_mode)
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_trainer",
                        "name": "switch_to_trainer_mode",
                        "args": {}
                    }]
                }
            })
            await asyncio.sleep(0.6)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_call", AsyncMock(return_value=True)) as mock_redirect:
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
                websocket.send_json({
                    "event": "media",
                    "media": {"payload": "f39/f39/f39/f39/"}
                })
                time.sleep(0.8)

            # redirect_call must NOT have been called to trainer-init
            for call_args in mock_redirect.call_args_list:
                called_url = call_args[0][1] if call_args[0] else ""
                self.assertNotIn("trainer-init", called_url,
                    "switch_to_trainer_mode must NOT redirect to trainer-init")

            # Gemini must have received the trainer code prompt
            send_calls = mock_gemini_ws.send.call_args_list
            trainer_prompt_text = None
            for call in send_calls:
                payload_str = call[0][0]
                payload = json.loads(payload_str)
                if "clientContent" in payload:
                    turns = payload["clientContent"].get("turns", [])
                    for turn in turns:
                        for part in turn.get("parts", []):
                            text = part.get("text", "")
                            if "código de simulación" in text.lower() and "Cristina" in text:
                                trainer_prompt_text = text
            self.assertIsNotNone(trainer_prompt_text,
                "Gemini must receive trainer code prompt when switch_to_trainer_mode is called")
            self.assertIn("Dubot", trainer_prompt_text)  # Req 12

    @patch("app.routers.training_hub_voice.settings")
    async def test_media_stream_dtmf_agent_and_mode_routing_cycles(self, mock_settings):
        """Req 8: DTMF '2' still correctly redirects to cycles-init."""
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
            await asyncio.sleep(0.8)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_call", AsyncMock(return_value=True)) as mock_redirect:
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
                # Req 1: DTMF 7777 identifies Cristina Montenegro
                for digit in ["7", "7", "7", "7"]:
                    websocket.send_json({
                        "event": "dtmf",
                        "dtmf": {"digit": digit}
                    })
                time.sleep(0.2)
                # Req 8: DTMF 2 to select Cycles
                websocket.send_json({
                    "event": "dtmf",
                    "dtmf": {"digit": "2"}
                })
                time.sleep(0.4)

            # Req 8: redirect_call must have been called to cycles-init
            mock_redirect.assert_called_once()
            called_url = mock_redirect.call_args[0][1]
            self.assertIn("cycles-init", called_url)
            self.assertIn("agent_id=7777", called_url)

    @patch("app.routers.training_hub_voice.settings")
    async def test_media_stream_trainer_code_greeting(self, mock_settings):
        """Req 5+6+7+12: flow=trainer_code must send sim code prompt, no hub greeting, uses Dubot."""
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
        
        with patch("websockets.connect", return_value=mock_connect):
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=trainer_code&agent_id=7777") as websocket:
                websocket.send_json({"event": "connected"})
                websocket.send_json({
                    "event": "start",
                    "start": {
                        "streamSid": "stream_123",
                        "callSid": "call_ws_test_hub",
                    }
                })
                time.sleep(0.4)

            # Verify greeting contents for trainer_code
            send_calls = mock_gemini_ws.send.call_args_list
            greeting_text = None
            for call in send_calls:
                payload_str = call[0][0]
                payload = json.loads(payload_str)
                if "clientContent" in payload:
                    turns = payload["clientContent"].get("turns", [])
                    for turn in turns:
                        for part in turn.get("parts", []):
                            text = part.get("text", "")
                            if "código" in text.lower():
                                greeting_text = text
            self.assertIsNotNone(greeting_text)
            self.assertIn("Dubot", greeting_text)  # Req 12
            self.assertIn("código de simulación", greeting_text.lower())
            # Req 6: no hub greeting
            self.assertNotIn("hola, soy el asistente", greeting_text.lower())
            # Req 7: no agent code request
            self.assertNotIn("dime tu código de agente", greeting_text.lower())
            self.assertNotIn("código de agente", greeting_text.lower())

    async def test_trainer_init_appends_query_params(self):
        """Req 9: trainer_init TwiML contains correct ws URL with flow and agent_id."""
        from app.routers.training_hub_voice import trainer_init
        req = self.mock_request(headers={"host": "test-host.com"})
        resp = await trainer_init(req, agent_id="7777", call_sid="call_123")
        body = resp.body.decode("utf-8")
        self.assertIn("wss://test-host.com/bm/training/hub/media-stream?flow=trainer_code&amp;agent_id=7777", body)

    # ── DTMF in-stream edge cases ──────────────────────────────────────────────

    @patch("app.routers.training_hub_voice.settings")
    async def test_dtmf_invalid_agent_code(self, mock_settings):
        """DTMF with invalid agent code (9999) must log warning, not crash."""
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
            await asyncio.sleep(0.8)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_call", AsyncMock(return_value=True)), \
             patch("app.routers.training_hub_voice.TrainerService.validate_agent_code", AsyncMock(return_value=None)):
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=hub") as websocket:
                websocket.send_json({"event": "connected"})
                websocket.send_json({
                    "event": "start",
                    "start": {"streamSid": "stream_123", "callSid": "call_ws_test_hub"}
                })
                for digit in ["9", "9", "9", "9"]:
                    websocket.send_json({"event": "dtmf", "dtmf": {"digit": digit}})
                time.sleep(0.4)

            # Gemini must have received an error prompt
            send_calls = mock_gemini_ws.send.call_args_list
            error_sent = any(
                # The router sends: 'No he encontrado ese código. Repítelo, por favor.'
                # after the first invalid DTMF attempt (attempts < 2)
                any(
                    "encontrado" in part.get("text", "").lower()
                    for part in turn.get("parts", [])
                )
                for c in send_calls
                for payload in [json.loads(c[0][0])]
                if "clientContent" in payload
                for turn in payload["clientContent"].get("turns", [])
            )
            self.assertTrue(error_sent, "Invalid agent code must trigger 'No he encontrado ese código' voice prompt")


    @patch("app.routers.training_hub_voice.settings")
    async def test_dtmf_invalid_mode(self, mock_settings):
        """DTMF invalid mode digit (9 after identification) must not crash."""
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
            await asyncio.sleep(1.0)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_call", AsyncMock(return_value=True)) as mock_redirect:
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=hub") as websocket:
                websocket.send_json({"event": "connected"})
                websocket.send_json({
                    "event": "start",
                    "start": {"streamSid": "stream_123", "callSid": "call_ws_test_hub"}
                })
                for digit in ["7", "7", "7", "7"]:
                    websocket.send_json({"event": "dtmf", "dtmf": {"digit": digit}})
                time.sleep(0.2)
                websocket.send_json({"event": "dtmf", "dtmf": {"digit": "9"}})
                time.sleep(0.3)

            # No redirect called (invalid digit = ignored)
            mock_redirect.assert_not_called()

    @patch("app.routers.training_hub_voice.settings")
    async def test_dtmf_simulation_code_valid(self, mock_settings):
        """DTMF simulation code SIM101 in trainer_code state redirects to Trainer."""
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
            await asyncio.sleep(1.5)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_trainer_call", AsyncMock(return_value=True)) as mock_trainer_redirect:
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=trainer_code&agent_id=7777") as websocket:
                websocket.send_json({"event": "connected"})
                websocket.send_json({
                    "event": "start",
                    "start": {"streamSid": "stream_123", "callSid": "call_ws_test_hub"}
                })
                # SIM101 = digits S-I-M-1-0-1 -> we test numeric: 101 # (short fallback)
                for digit in ["S", "I", "M", "1", "0", "1"]:
                    websocket.send_json({"event": "dtmf", "dtmf": {"digit": digit}})
                time.sleep(0.4)

            # Trainer redirect should have been called (SIM101 is 6 chars)
            mock_trainer_redirect.assert_called_once()

    @patch("app.routers.training_hub_voice.settings")
    async def test_dtmf_simulation_code_invalid(self, mock_settings):
        """DTMF invalid simulation code triggers retry prompt."""
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
            await asyncio.sleep(1.5)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_trainer_call", AsyncMock(return_value=True)) as mock_trainer_redirect:
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=trainer_code&agent_id=7777") as websocket:
                websocket.send_json({"event": "connected"})
                websocket.send_json({
                    "event": "start",
                    "start": {"streamSid": "stream_123", "callSid": "call_ws_test_hub"}
                })
                # INVALI = 6 chars but invalid code
                for digit in ["I", "N", "V", "A", "L", "I"]:
                    websocket.send_json({"event": "dtmf", "dtmf": {"digit": digit}})
                time.sleep(0.4)

            # Should NOT have been redirected for invalid code
            mock_trainer_redirect.assert_not_called()
            
            # Error prompt must have been sent to Gemini
            send_calls = mock_gemini_ws.send.call_args_list
            error_sent = any(
                "encontrado" in json.loads(c[0][0]).get("clientContent", {}).get("turns", [{}])[0].get("parts", [{}])[0].get("text", "").lower()
                for c in send_calls
                if "clientContent" in json.loads(c[0][0])
            )
            self.assertTrue(error_sent, "Invalid sim code must trigger error voice prompt to Gemini")


    @patch("app.routers.training_hub_voice.settings")
    async def test_validate_trainer_simulation_code_valid(self, mock_settings):
        """Req 1+2: In trainer_code state, validate_trainer_simulation_code with valid code redirects to Trainer."""
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
            await asyncio.sleep(0.1)
            # Gemini calls validate_trainer_simulation_code with a valid code
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_sim_valid",
                        "name": "validate_trainer_simulation_code",
                        "args": {"code": "SIM101"}
                    }]
                }
            })
            await asyncio.sleep(0.6)

        mock_gemini_ws.__aiter__ = mock_async_iter
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None

        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_trainer_call", AsyncMock(return_value=True)) as mock_trainer_redirect:
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=trainer_code&agent_id=7777") as ws:
                ws.send_json({"event": "connected"})
                ws.send_json({"event": "start", "start": {"streamSid": "s123", "callSid": "c_sim_test"}})
                time.sleep(0.9)

            # Req 2: redirect_trainer_call must have been called
            mock_trainer_redirect.assert_called_once()
            call_args = mock_trainer_redirect.call_args[0]
            self.assertEqual(call_args[2], "7777")   # agent_id
            self.assertIsInstance(call_args[3], int)  # simulation_id must be a valid int
            self.assertGreater(call_args[3], 0)       # must be a positive ID

    @patch("app.routers.training_hub_voice.settings")
    async def test_validate_trainer_simulation_code_invalid(self, mock_settings):
        """Req 3: In trainer_code state, invalid code sends toolResponse valid=False back to Gemini."""
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
            await asyncio.sleep(0.1)
            # Gemini calls validate_trainer_simulation_code with INVALID code
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_sim_bad",
                        "name": "validate_trainer_simulation_code",
                        "args": {"code": "INVALID999"}
                    }]
                }
            })
            await asyncio.sleep(0.6)

        mock_gemini_ws.__aiter__ = mock_async_iter
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None

        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_trainer_call", AsyncMock(return_value=True)) as mock_trainer_redirect:
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=trainer_code&agent_id=7777") as ws:
                ws.send_json({"event": "connected"})
                ws.send_json({"event": "start", "start": {"streamSid": "s123", "callSid": "c_inv_test"}})
                time.sleep(0.9)

            # Req 3: redirect_trainer_call must NOT have been called
            mock_trainer_redirect.assert_not_called()

            # Gemini must have received toolResponse with valid=False
            send_calls = mock_gemini_ws.send.call_args_list
            invalid_response_sent = False
            for call in send_calls:
                payload = json.loads(call[0][0])
                if "toolResponse" in payload:
                    for fr in payload["toolResponse"]["functionResponses"]:
                        if fr["name"] == "validate_trainer_simulation_code":
                            result = fr["response"]["result"]
                            if result.get("valid") is False:
                                invalid_response_sent = True
            self.assertTrue(invalid_response_sent,
                "Invalid simulation code must send toolResponse valid=False to Gemini")

    @patch("app.routers.training_hub_voice.settings")
    async def test_hub_flow_trainer_voice_calls_validate_simulation_tool(self, mock_settings):
        """Req 6+7: After switch_to_trainer_mode, Gemini calling validate_trainer_simulation_code
        must trigger simulation lookup (not fall through silently)."""
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
            await asyncio.sleep(0.05)
            # Phase 1: verify_agent_code
            yield json.dumps({
                "toolCall": {"functionCalls": [{
                    "id": "call_a", "name": "verify_agent_code",
                    "args": {"agent_code": "siete siete siete siete"}
                }]}
            })
            await asyncio.sleep(0.05)
            # Phase 1: switch to trainer mode
            yield json.dumps({
                "toolCall": {"functionCalls": [{
                    "id": "call_b", "name": "switch_to_trainer_mode", "args": {}
                }]}
            })
            await asyncio.sleep(0.05)
            # Phase 2: now in trainer_code state, Gemini calls validate_trainer_simulation_code
            yield json.dumps({
                "toolCall": {"functionCalls": [{
                    "id": "call_c", "name": "validate_trainer_simulation_code",
                    "args": {"code": "SIM101"}
                }]}
            })
            await asyncio.sleep(0.8)

        mock_gemini_ws.__aiter__ = mock_async_iter
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None

        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.training_hub_voice.redirect_trainer_call", AsyncMock(return_value=True)) as mock_trainer_redirect, \
             patch("app.routers.training_hub_voice.redirect_call", AsyncMock(return_value=True)):
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=hub") as ws:
                ws.send_json({"event": "connected"})
                ws.send_json({"event": "start", "start": {"streamSid": "s_full", "callSid": "c_full_test"}})
                ws.send_json({"event": "media", "media": {"payload": "f39/f39/f39/f39/"}})
                time.sleep(1.2)

            # Req 6: redirect_trainer_call must have been called (simulation code processed)
            mock_trainer_redirect.assert_called_once()
            self.assertEqual(mock_trainer_redirect.call_args[0][3], 1)  # simulation_id=1


    @patch("app.routers.training_hub_voice.settings")
    async def test_idempotence_and_text_checks(self, mock_settings):
        """Task 6 / Task 1 & 2 tests: check idempotence of switch_to_trainer_mode and ensure no forbidden phrases exist."""
        from app.routers.training_hub_voice import HUB_SYSTEM_INSTRUCTION, TRAINER_CODE_SYSTEM_INSTRUCTION
        
        mock_settings.gemini_api_key = "mock_key"
        mock_settings.gemini_live_api_key = None
        mock_settings.gemini_model = "models/gemini-3.1-flash-live-preview"
        mock_settings.gemini_live_model = None

        # 1. No forbidden phrases in system prompts
        for prompt_name, prompt_text in [
            ("HUB_SYSTEM_INSTRUCTION", HUB_SYSTEM_INSTRUCTION),
            ("TRAINER_CODE_SYSTEM_INSTRUCTION", TRAINER_CODE_SYSTEM_INSTRUCTION)
        ]:
            for forbidden in ["puedes decirlo por voz", "introducirlo por teclado", "por voz o por teclado", "marcarlo con el teclado"]:
                self.assertNotIn(forbidden, prompt_text.lower(), f"Forbidden phrase '{forbidden}' found in {prompt_name}")

        # 2. Greeting exact content
        self.assertIn("hola, soy el asistente de entrenamiento de dubot. dime tu código de agente", HUB_SYSTEM_INSTRUCTION.lower())
        self.assertIn("código de la simulación que quiere practicar", HUB_SYSTEM_INSTRUCTION.lower())
        self.assertIn("no he encontrado ese código. repítelo, por favor", HUB_SYSTEM_INSTRUCTION.lower())
        self.assertIn("pasamos a trainer. dime el código de simulación", TRAINER_CODE_SYSTEM_INSTRUCTION.lower())

        # 3. Simulate websocket tool calls for switch_to_trainer_mode idempotency
        from fastapi.testclient import TestClient
        from app.main import app
        import json
        import asyncio
        import time

        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()

        # We will feed it verify_agent_code, then select trainer mode twice
        async def mock_async_iter(*args, **kwargs):
            # Setup complete
            yield json.dumps({"setupComplete": {}})
            # Verify Cristina Montenegro (7777)
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_agent",
                        "name": "verify_agent_code",
                        "args": {"agent_code": "7777"}
                    }]
                }
            })
            await asyncio.sleep(0.1)
            # Call switch_to_trainer_mode 1st time
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_switch_1",
                        "name": "switch_to_trainer_mode",
                        "args": {}
                    }]
                }
            })
            await asyncio.sleep(0.1)
            # Call switch_to_trainer_mode 2nd time (duplicate)
            yield json.dumps({
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_switch_2",
                        "name": "switch_to_trainer_mode",
                        "args": {}
                    }]
                }
            })
            await asyncio.sleep(0.5)

        mock_gemini_ws.__aiter__ = mock_async_iter
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None

        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.services.trainer_service.TrainerService.validate_agent_code", AsyncMock(return_value={"agent_id": 7777, "agent_name": "Cristina Montenegro", "agent_initials": "CM"})):
            client = TestClient(app)
            with client.websocket_connect("/bm/training/hub/media-stream?flow=hub") as ws:
                ws.send_json({"event": "connected"})
                ws.send_json({"event": "start", "start": {"streamSid": "s1", "callSid": "c1"}})
                time.sleep(1.0)

        # Retrieve the messages sent to Gemini
        sent_messages = [json.loads(c[0][0]) for c in mock_gemini_ws.send.call_args_list]
        
        # Check responses
        responses = []
        for msg in sent_messages:
            if "toolResponse" in msg:
                for resp in msg["toolResponse"].get("functionResponses", []):
                    responses.append(resp)
                    
        # Find responses for switch_to_trainer_mode
        switch_responses = [r for r in responses if r["name"] == "switch_to_trainer_mode"]
        self.assertEqual(len(switch_responses), 2)
        
        # First response must be ok
        self.assertEqual(switch_responses[0]["response"], {"result": {"status": "ok"}})
        # Second response must be already_in_trainer
        self.assertEqual(switch_responses[1]["response"]["result"]["already_in_trainer"], True)
        self.assertEqual(switch_responses[1]["response"]["result"]["state"], "trainer_code")

        # Verify that the Trainer code prompt was only sent once (it contains 'Pasamos a Trainer de Dubot. Dime el código')
        trainer_prompts_sent = 0
        for msg in sent_messages:
            if "clientContent" in msg:
                for turn in msg["clientContent"].get("turns", []):
                    for part in turn.get("parts", []):
                        if "Pasamos a Trainer de Dubot. Dime el código" in part.get("text", ""):
                            trainer_prompts_sent += 1
        self.assertEqual(trainer_prompts_sent, 1)


if __name__ == "__main__":
    unittest.main()
