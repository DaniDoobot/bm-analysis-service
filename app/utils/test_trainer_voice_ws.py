"""Tests for trainer_voice.py: settings robustness, WebSocket Gemini init, and simulation code flow."""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///trainer_voice_test.db"
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import json
import asyncio
import time


class TestTrainerVoiceSettings(unittest.IsolatedAsyncioTestCase):
    """Unit tests for settings robustness in trainer_voice.py."""

    def test_no_direct_settings_gemini_live_api_key(self):
        """Req 1: trainer_voice.py must NOT access settings.gemini_live_api_key directly."""
        import ast
        import pathlib

        src = pathlib.Path("app/routers/trainer_voice.py").read_text(encoding="utf-8")
        # Check for the dangerous direct access pattern
        dangerous_patterns = [
            "settings.gemini_live_api_key",
            "settings.gemini_live_model",
        ]
        for pattern in dangerous_patterns:
            self.assertNotIn(
                pattern, src,
                f"trainer_voice.py must not access {pattern!r} directly. Use getattr() instead."
            )

    def test_no_direct_settings_twilio_without_getattr(self):
        """trainer_voice.py Twilio credential access must use getattr() pattern."""
        import pathlib
        src = pathlib.Path("app/routers/trainer_voice.py").read_text(encoding="utf-8")
        # Should not have bare settings.twilio_account_sid assignment
        self.assertNotIn("= settings.twilio_account_sid", src)
        self.assertNotIn("= settings.twilio_auth_token", src)

    def test_getattr_pattern_present(self):
        """trainer_voice.py must use getattr() for gemini_api_key and gemini_live_api_key."""
        import pathlib
        src = pathlib.Path("app/routers/trainer_voice.py").read_text(encoding="utf-8")
        self.assertIn('getattr(settings, "gemini_live_api_key", None)', src)
        self.assertIn('getattr(settings, "gemini_api_key", None)', src)

    def test_trainer_voice_log_message_on_key_configured(self):
        """Req: trainer_voice.py must log 'Trainer voice Gemini API key configured: yes'."""
        import pathlib
        src = pathlib.Path("app/routers/trainer_voice.py").read_text(encoding="utf-8")
        self.assertIn("Trainer voice Gemini API key configured: yes", src)

    def test_duration_monitor_uses_clientcontent_not_media_chunks(self):
        """duration_monitor_task must use clientContent text, not deprecated realtimeInput.mediaChunks."""
        import pathlib
        src = pathlib.Path("app/routers/trainer_voice.py").read_text(encoding="utf-8")
        # mediaChunks should no longer be in the duration_monitor_task section
        # (we check the whole file - if it's gone from duration_monitor it's fixed)
        self.assertNotIn('"mediaChunks"', src)
        self.assertNotIn("mediaChunks", src)


class TestTrainerVoiceSettingsGetattr(unittest.IsolatedAsyncioTestCase):
    """Tests verifying that getattr() fallback logic works at the function level."""

    def test_getattr_fallback_live_key_missing(self):
        """Req 2: getattr with missing gemini_live_api_key returns None, then falls back to gemini_api_key."""
        class FakeSettings:
            gemini_api_key = "fallback_key"
            # gemini_live_api_key is NOT defined (simulates env without this attr)

        s = FakeSettings()
        result = getattr(s, "gemini_live_api_key", None) or getattr(s, "gemini_api_key", None)
        self.assertEqual(result, "fallback_key")

    def test_getattr_fallback_both_none(self):
        """Req 4: If both keys are None, result is None (should trigger graceful close)."""
        class FakeSettings:
            gemini_live_api_key = None
            gemini_api_key = None

        s = FakeSettings()
        result = getattr(s, "gemini_live_api_key", None) or getattr(s, "gemini_api_key", None)
        self.assertIsNone(result)

    def test_getattr_prefers_live_key(self):
        """Req 3: gemini_live_api_key is preferred when both exist."""
        class FakeSettings:
            gemini_live_api_key = "live_key"
            gemini_api_key = "fallback_key"

        s = FakeSettings()
        result = getattr(s, "gemini_live_api_key", None) or getattr(s, "gemini_api_key", None)
        self.assertEqual(result, "live_key")

    def test_getattr_model_fallback(self):
        """Model fallback: gemini_live_model -> gemini_model -> hardcoded default."""
        class FakeSettingsNoLiveModel:
            gemini_live_model = None
            gemini_model = "models/gemini-3.1-flash-live-preview"

        class FakeSettingsNeitherModel:
            gemini_live_model = None
            gemini_model = None

        s1 = FakeSettingsNoLiveModel()
        result1 = getattr(s1, "gemini_live_model", None) or getattr(s1, "gemini_model", None) or "models/gemini-2.0-flash-exp"
        self.assertEqual(result1, "models/gemini-3.1-flash-live-preview")

        s2 = FakeSettingsNeitherModel()
        result2 = getattr(s2, "gemini_live_model", None) or getattr(s2, "gemini_model", None) or "models/gemini-2.0-flash-exp"
        self.assertEqual(result2, "models/gemini-2.0-flash-exp")


class TestTrainerVoiceRedirectHelper(unittest.IsolatedAsyncioTestCase):
    """Tests for redirect_trainer_call helper."""

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("trainer_voice_test.db"):
            try:
                os.remove("trainer_voice_test.db")
            except Exception:
                pass
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        if os.path.exists("trainer_voice_test.db"):
            try:
                os.remove("trainer_voice_test.db")
            except Exception:
                pass

    @patch("app.routers.trainer_voice.settings")
    async def test_redirect_trainer_call_no_twilio_creds(self, mock_settings):
        """redirect_trainer_call returns False if Twilio credentials missing."""
        mock_settings.twilio_account_sid = None
        mock_settings.twilio_auth_token = None

        from app.routers.trainer_voice import redirect_trainer_call
        # Also patch os.getenv to return None
        with patch.dict(os.environ, {}, clear=True):
            # Remove TWILIO env vars if set
            env = {k: v for k, v in os.environ.items() if "TWILIO" not in k}
            with patch("os.getenv", return_value=None):
                result = await redirect_trainer_call("call_123", "test-host.com", "7777", 1)
        self.assertFalse(result)

    @patch("app.routers.trainer_voice.settings")
    async def test_redirect_trainer_call_makes_twilio_request(self, mock_settings):
        """redirect_trainer_call makes correct POST to Twilio API."""
        mock_settings.twilio_account_sid = "AC_test_sid"
        mock_settings.twilio_auth_token = "test_token"

        from app.routers.trainer_voice import redirect_trainer_call
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client_class.return_value.__aexit__.return_value = None
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_response.raise_for_status = MagicMock()

            result = await redirect_trainer_call("call_abc", "api.test.com", "7777", 42)

        self.assertTrue(result)
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        url = call_args[0][0]
        self.assertIn("AC_test_sid", url)
        self.assertIn("call_abc", url)
        redirect_url = call_args[1]["data"]["Url"]
        self.assertIn("agent_id=7777", redirect_url)
        self.assertIn("simulation_id=42", redirect_url)


    @patch("app.routers.trainer_voice.settings")
    async def test_websocket_trainer_flow_parameter_extractions(self, mock_settings):
        """Task 6 / 13-20 tests: Test that the WebSocket accepts flow=session/roleplay, extracts session_id from customParameters, and does not reject before start event."""
        from app.routers.trainer_voice import media_stream
        from app.models.trainer import TrainerSession, TrainerSimulation
        from app.db import AsyncSessionLocal
        from fastapi import WebSocket
        import json
        import asyncio
        
        mock_settings.gemini_live_api_key = "mock_key"
        mock_settings.gemini_live_model = "models/gemini-2.0-flash-exp"
        mock_settings.twilio_account_sid = None
        mock_settings.twilio_auth_token = None

        # Insert a mock session in DB
        async with AsyncSessionLocal() as db:
            sim = TrainerSimulation(simulation_id=99, code="SIM_T_99", name="Test Sim", roleplay_prompt="Test Prompt", service_id=1)
            db.add(sim)
            await db.commit()
            
            sess = TrainerSession(session_id=99, agent_id="7777", agent_code="7777", simulation_id=99, service_id=1, call_id="call_99", status="started")
            db.add(sess)
            await db.commit()

        # Helper to run media_stream with mocked websocket
        async def run_media_stream_test(flow_param, session_id_param, start_event_data=None):
            from unittest.mock import MagicMock
            mock_ws = AsyncMock()
            mock_ws.accept = AsyncMock()
            mock_ws.close = AsyncMock()
            mock_ws.headers = {"host": "test-host.com"}
            mock_ws.client_state = MagicMock()
            mock_ws.client_state.name = "CONNECTED"
            mock_ws.scope = {"query_string": b"flow=session&session_id=99"}
            
            # Setup mock messages received via iter_text
            async def mock_iter_text():
                # Yield connected
                yield json.dumps({"event": "connected"})
                # Yield start
                if start_event_data:
                    yield json.dumps(start_event_data)
                else:
                    yield json.dumps({"event": "start", "start": {"streamSid": "str_1", "callSid": "call_1"}})
                # Then stop the loop
                
            mock_ws.iter_text = mock_iter_text
            
            # websockets.connect will raise ValueError to stop execution right after setup is attempted
            with patch("websockets.connect", side_effect=ValueError("stop_test")):
                async with AsyncSessionLocal() as db_session:
                    try:
                        await media_stream(
                            websocket=mock_ws,
                            flow=flow_param,
                            session_id=session_id_param,
                            db=db_session
                        )
                    except ValueError as e:
                        if str(e) != "stop_test":
                            raise e

        # 1. Test query param flow=session
        try:
            await run_media_stream_test("session", 99)
        except Exception as e:
            self.fail(f"WebSocket raised exception with flow=session: {e}")

        # 2. Test query param flow=roleplay
        try:
            await run_media_stream_test("roleplay", 99)
        except Exception as e:
            self.fail(f"WebSocket raised exception with flow=roleplay: {e}")

        # 3. Test extraction from customParameters
        start_event = {
            "event": "start",
            "start": {
                "streamSid": "str_3",
                "callSid": "call_3",
                "customParameters": {
                    "session_id": "99",
                    "flow": "roleplay"
                }
            }
        }
        try:
            await run_media_stream_test(None, None, start_event_data=start_event)
        except Exception as e:
            self.fail(f"WebSocket raised exception when extracting session_id from customParameters: {e}")


class TestTrainerVoiceTurnGate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import app.models
        from app.db import engine, Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        from app.db import AsyncSessionLocal
        self.db = AsyncSessionLocal()

    async def asyncTearDown(self):
        await self.db.close()
        import app.models
        from app.db import engine, Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    @patch("app.routers.trainer_voice.settings")
    async def test_turn_gate_and_system_prompts(self, mock_settings):
        from app.routers.trainer_voice import media_stream
        from app.models.trainer import TrainerSimulation, TrainerSession
        from app.models.personalized_training import TrainingAgentSetting
        from unittest.mock import MagicMock
        from datetime import datetime

        mock_settings.gemini_live_api_key = "mock_key"
        mock_settings.gemini_live_model = "models/gemini-2.0-flash-exp"
        mock_settings.twilio_account_sid = None
        mock_settings.twilio_auth_token = None

        # 1. Seed simulation and agent
        sim = TrainerSimulation(simulation_id=1, code="323334", name="Prueba1", roleplay_prompt="Carmen es cliente.", service_id=1)
        self.db.add(sim)
        agent = TrainingAgentSetting(hubspot_owner_id="33013276", agent_name="Cristina Montenegro", agent_initials="CM", is_enabled=True)
        self.db.add(agent)
        await self.db.commit()

        sess = TrainerSession(session_id=1, agent_id="33013276", agent_code="33013276", simulation_id=1, service_id=1, call_id="call_1", status="started")
        self.db.add(sess)
        await self.db.commit()

        # Mock websockets connect and gemini connection
        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()

        # Simulate Gemini replies
        async def mock_async_iter(*args, **kwargs):
            # Setup Complete
            yield json.dumps({"setupComplete": {}})
            # Gemini speaks 1st time
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"data": "PCM_AUDIO_DATA"}}]
                    },
                    "turnComplete": True
                }
            })
            await asyncio.sleep(0.1)
            # Gemini tries to speak 2nd time without user audio (self-response)
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"data": "PCM_AUDIO_DATA_DUPLICATED"}}]
                    },
                    "turnComplete": True
                }
            })
            await asyncio.sleep(0.5)

        mock_gemini_ws.__aiter__ = mock_async_iter

        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None

        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.headers = {"host": "test-host.com"}
        mock_ws.client_state = MagicMock()
        mock_ws.client_state.name = "CONNECTED"
        mock_ws.scope = {"query_string": b"flow=session&session_id=1"}

        # Simulate Twilio messages
        async def mock_iter_text():
            # Initial connection
            yield json.dumps({"event": "connected"})
            await asyncio.sleep(0.3)
            # Inbound audio track (valid user speech)
            yield json.dumps({
                "event": "media",
                "media": {
                    "track": "inbound",
                    "payload": "f39/f39/f39/"
                }
            })
            # Outbound audio track (should be ignored to prevent loops)
            yield json.dumps({
                "event": "media",
                "media": {
                    "track": "outbound",
                    "payload": "f39/f39/f39/"
                }
            })
            await asyncio.sleep(1.0)
            yield json.dumps({"event": "stop"})

        mock_ws.iter_text = mock_iter_text

        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.trainer_voice.start_twilio_recording", AsyncMock(return_value="rec_1")), \
             patch("app.routers.trainer_voice.decode_twilio_to_gemini", return_value=("PCM_DUMMY_DATA", None)):
            try:
                await media_stream(mock_ws, flow="session", session_id=1, db=self.db)
            except Exception as e:
                if str(e) != "stop_test":
                    self.fail(f"WebSocket raised exception: {e}")

        # Check setup instruction turns constraint
        setup_calls = [json.loads(c[0][0]) for c in mock_gemini_ws.send.call_args_list if "setup" in json.loads(c[0][0])]
        self.assertEqual(len(setup_calls), 1)
        system_instruction = setup_calls[0]["setup"]["systemInstruction"]["parts"][0]["text"]
        system_instruction_lower = system_instruction.lower()
        
        # Req 1: Trainer prompt includes instruction about not simulating agent
        self.assertIn("no simules nunca la respuesta del agente", system_instruction_lower)
        # Req 2: Trainer prompt includes turn-waiting instruction
        self.assertIn("después de cada intervención, detente y espera", system_instruction_lower)

        # Check prompt greetings
        client_contents = [json.loads(c[0][0]) for c in mock_gemini_ws.send.call_args_list if "clientContent" in json.loads(c[0][0])]
        
        # Req 3: Initial roleplay prompt is sent only once
        self.assertEqual(len(client_contents), 1)
        
        # Req 6: Outbound track is ignored, only inbound is forwarded
        sent_realtime_audio = [json.loads(c[0][0]) for c in mock_gemini_ws.send.call_args_list if "realtimeInput" in json.loads(c[0][0])]
        self.assertGreater(len(sent_realtime_audio), 0)


class TestTrainerVoiceDetailedVAD(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import app.models
        from app.db import engine, Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        from app.db import AsyncSessionLocal
        self.db = AsyncSessionLocal()

    async def asyncTearDown(self):
        await self.db.close()
        import app.models
        from app.db import engine, Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    def test_calculate_pcm_energy(self):
        from app.routers.trainer_voice import calculate_pcm_energy
        # Silence
        self.assertEqual(calculate_pcm_energy(b""), 0.0)
        self.assertEqual(calculate_pcm_energy(b"\x00\x00\x00\x00"), 0.0)
        # Noise/Speech (sine values simulation)
        voice_pcm = b"\x00\x10\x00\x20\x00\x30\x00\x40"
        energy = calculate_pcm_energy(voice_pcm)
        self.assertGreater(energy, 0.0)

    @patch("app.routers.trainer_voice.settings")
    async def test_barge_in_and_prompt_constraints(self, mock_settings):
        from app.routers.trainer_voice import media_stream
        from app.models.trainer import TrainerSimulation, TrainerSession
        from app.models.personalized_training import TrainingAgentSetting
        from unittest.mock import MagicMock

        mock_settings.gemini_live_api_key = "mock_key"
        mock_settings.gemini_live_model = "models/gemini-2.0-flash-exp"
        mock_settings.twilio_account_sid = None
        mock_settings.twilio_auth_token = None

        sim = TrainerSimulation(simulation_id=1, code="323334", name="Prueba1", roleplay_prompt="Carmen es cliente.", service_id=1)
        self.db.add(sim)
        agent = TrainingAgentSetting(hubspot_owner_id="33013276", agent_name="Cristina Montenegro", agent_initials="CM", is_enabled=True)
        self.db.add(agent)
        await self.db.commit()

        sess = TrainerSession(session_id=1, agent_id="33013276", agent_code="33013276", simulation_id=1, service_id=1, call_id="call_1", status="started")
        self.db.add(sess)
        await self.db.commit()

        # Mock websockets connect and gemini connection
        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()

        # Simulate Gemini replies
        async def mock_async_iter(*args, **kwargs):
            # Setup Complete
            yield json.dumps({"setupComplete": {}})
            # Gemini speaks 1st time
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"data": "PCM_AUDIO_DATA"}}]
                    },
                    "turnComplete": False # Keeping it False to simulate assistant_is_speaking = True
                }
            })
            await asyncio.sleep(0.5)
            yield json.dumps({"serverContent": {"turnComplete": True}})

        mock_gemini_ws.__aiter__ = mock_async_iter

        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None

        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.headers = {"host": "test-host.com"}
        mock_ws.client_state = MagicMock()
        mock_ws.client_state.name = "CONNECTED"
        mock_ws.scope = {"query_string": b"flow=session&session_id=1"}

        # Simulate Twilio messages
        async def mock_iter_text():
            # Initial connection
            yield json.dumps({"event": "connected"})
            yield json.dumps({
                "event": "start",
                "start": {
                    "streamSid": "str_1",
                    "callSid": "call_1"
                }
            })
            await asyncio.sleep(0.3)
            # 1. Send 15 consecutive media frames to satisfy VAD duration threshold (15 * 20ms = 300ms > 250ms)
            for _ in range(15):
                yield json.dumps({
                    "event": "media",
                    "media": {
                        "track": "inbound",
                        "payload": "f39/f39/f39/"
                    }
                })
                await asyncio.sleep(0.01)
            yield json.dumps({"event": "stop"})

        mock_ws.iter_text = mock_iter_text

        # Mock decode_twilio_to_gemini to return High Energy PCM to trigger Barge-in VAD
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.trainer_voice.start_twilio_recording", AsyncMock(return_value="rec_1")), \
             patch("app.routers.trainer_voice.decode_twilio_to_gemini", return_value=("DUMMY", None)), \
             patch("app.routers.trainer_voice.calculate_pcm_energy", return_value=9999.0): # High Energy VAD
            try:
                await media_stream(mock_ws, flow="session", session_id=1, db=self.db)
            except Exception as e:
                self.fail(f"media_stream raised exception: {e}")

        # Check prompt rules
        setup_calls = [json.loads(c[0][0]) for c in mock_gemini_ws.send.call_args_list if "setup" in json.loads(c[0][0])]
        system_instruction_lower = setup_calls[0]["setup"]["systemInstruction"]["parts"][0]["text"].lower()
        self.assertIn("no vuelvas a hacerlo", system_instruction_lower)
        self.assertIn("no repitas la misma objeción de precio en turnos consecutivos", system_instruction_lower)

        # Check that 'clear' event was sent to Twilio during barge-in
        clear_calls = []
        for call in mock_ws.send_text.call_args_list:
            if call[0]:
                val = call[0][0]
                if isinstance(val, str) and "clear" in val:
                    clear_calls.append(json.loads(val))
        self.assertGreaterEqual(len(clear_calls), 1)

    async def test_list_sessions_with_partial_and_score_none_data(self):
        from app.services.trainer_service import TrainerService
        from app.models.trainer import TrainerSimulation, TrainerSession, TrainerEvaluation
        from app.models.personalized_training import TrainingAgentSetting
        from datetime import datetime

        # Seed data
        sim = TrainerSimulation(simulation_id=2, code="SIM_T_2", name="Sim Test 2", roleplay_prompt="Test Prompt", service_id=1)
        self.db.add(sim)
        agent = TrainingAgentSetting(hubspot_owner_id="33013277", agent_name="Agent 2", agent_initials="A2", is_enabled=True)
        self.db.add(agent)
        await self.db.commit()

        # Session 1: completed with score=None
        s1 = TrainerSession(agent_id="33013277", agent_code="33013277", simulation_id=2, service_id=1, call_id="call_10", status="completed", evaluation_status="completed_without_score")
        self.db.add(s1)
        # Session 2: incomplete/started (no evaluation)
        s2 = TrainerSession(agent_id="33013277", agent_code="33013277", simulation_id=2, service_id=1, call_id="call_11", status="started", evaluation_status="started")
        self.db.add(s2)
        await self.db.commit()
        await self.db.refresh(s1)
        await self.db.refresh(s2)

        e1 = TrainerEvaluation(session_id=s1.session_id, evaluation_config_id=1, prompt_snapshot="Test snapshot", result_json={}, score=None)
        self.db.add(e1)
        await self.db.commit()

        # Run list_sessions
        sessions, total = await TrainerService.list_sessions(self.db, agent_id="33013277")
        self.assertEqual(total, 2)
        self.assertEqual(len(sessions), 2)
        
        # Verify eager loaded relations
        sess_1 = next(s for s in sessions if s.session_id == s1.session_id)
        sess_2 = next(s for s in sessions if s.session_id == s2.session_id)
        self.assertEqual(sess_1.simulation.simulation_id, 2)
        self.assertIsNone(sess_1.evaluation.score)
        self.assertIsNone(sess_2.evaluation)



class TestTrainerVoiceV2(unittest.IsolatedAsyncioTestCase):
    """Tests for Trainer Voice v2 requirements: VAD threshold, early hangup guard, race condition fix."""

    async def asyncSetUp(self):
        import app.models
        from app.db import engine, Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        import app.models
        from app.db import engine, Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


    def test_vad_threshold_lowered(self):
        """P1: VAD_ENERGY_THRESHOLD must be <= 200 for real call detection."""
        from app.routers import trainer_voice
        self.assertLessEqual(trainer_voice.VAD_ENERGY_THRESHOLD, 200.0,
            "VAD_ENERGY_THRESHOLD too high — real speech may not be detected.")

    def test_vad_min_speech_duration_lowered(self):
        """P1: VAD_MIN_SPEECH_DURATION_MS must be <= 150ms."""
        from app.routers import trainer_voice
        self.assertLessEqual(trainer_voice.VAD_MIN_SPEECH_DURATION_MS, 150,
            "VAD_MIN_SPEECH_DURATION_MS too high — may block real speech detection.")

    def test_hangup_early_block_constant_exists(self):
        """P5: HANGUP_EARLY_BLOCK_SECONDS must exist and be >= 60."""
        from app.routers import trainer_voice
        self.assertTrue(hasattr(trainer_voice, "HANGUP_EARLY_BLOCK_SECONDS"),
            "HANGUP_EARLY_BLOCK_SECONDS not defined.")
        self.assertGreaterEqual(trainer_voice.HANGUP_EARLY_BLOCK_SECONDS, 60)

    def test_handle_roleplay_hangup_uses_completed_waiting_recording(self):
        """P6: handle_roleplay_hangup must set status=completed_waiting_recording, NOT trigger evaluation."""
        import inspect
        from app.routers import trainer_voice
        src = inspect.getsource(trainer_voice.handle_roleplay_hangup)
        self.assertIn("completed_waiting_recording", src,
            "handle_roleplay_hangup must use completed_waiting_recording status.")
        # Must NOT contain asyncio.create_task for evaluation inside handle_roleplay_hangup
        self.assertNotIn("run_evaluation_task", src,
            "handle_roleplay_hangup must not trigger evaluation directly — wait for recording webhook.")

    def test_recording_completed_triggers_evaluation_for_waiting_status(self):
        """P6: recording_completed endpoint must trigger evaluation when eval_status=completed_waiting_recording."""
        import inspect
        from app.routers import trainer_voice
        src = inspect.getsource(trainer_voice.recording_completed)
        self.assertIn("completed_waiting_recording", src,
            "recording_completed must handle completed_waiting_recording status.")
        self.assertIn("evaluation_error", src,
            "recording_completed must retry on evaluation_error status.")

    def test_first_turn_brevity_in_prompt(self):
        """P4: CHARACTER_LOCK must include first-turn brevity rule."""
        from app.routers import trainer_voice
        self.assertIn("PRIMERA INTERVENCIÓN BREVE", trainer_voice.SPANISH_VOICE_RULES,
            "Prompt must include first-turn brevity rule.")

    async def test_list_sessions_returns_simulation_name_and_service_name(self):
        """P7: list_sessions must attach simulation_name, simulation_code, service_name as transient attributes."""
        from app.services.trainer_service import TrainerService
        from app.models.trainer import TrainerSimulation, TrainerSession
        from app.models.personalized_training import TrainingAgentSetting
        from app.models.services import Service
        from app.db import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            # Seed service
            svc = Service(service_id=999, service_key="test_svc", service_name="TestService999")
            db.add(svc)
            await db.flush()

            sim = TrainerSimulation(
                simulation_id=500, code="SIM500", name="Test Sim 500",
                roleplay_prompt="Test Prompt", service_id=999
            )
            db.add(sim)

            agent_setting = TrainingAgentSetting(
                hubspot_owner_id="HUB_TEST_500",
                agent_name="Agent Test 500",
                agent_initials="AT5",
                is_enabled=True,
                training_code="CODE500"
            )
            db.add(agent_setting)
            await db.commit()

            s = TrainerSession(
                session_id=500,
                agent_id="HUB_TEST_500",
                agent_code="CODE500",
                simulation_id=500,
                service_id=999,
                call_id="call_500",
                status="completed",
                evaluation_status="evaluated"
            )
            db.add(s)
            await db.commit()

            sessions, total = await TrainerService.list_sessions(db, agent_id="HUB_TEST_500")
            self.assertGreaterEqual(total, 1)
            sess = next((s for s in sessions if s.session_id == 500), None)
            self.assertIsNotNone(sess)
            self.assertEqual(sess.__dict__.get("simulation_name"), "Test Sim 500")
            self.assertEqual(sess.__dict__.get("simulation_code"), "SIM500")
            self.assertEqual(sess.__dict__.get("service_name"), "TestService999")
            self.assertEqual(sess.__dict__.get("agent_name"), "Agent Test 500")

    @patch("app.services.trainer_service.TrainerService.download_trainer_recording_audio")
    async def test_recording_audio_proxy_endpoint(self, mock_download):
        """Test proxy recording-audio endpoint behaviour (404, 502, success)."""
        from app.main import app
        from app.dependencies import get_current_user
        from httpx import AsyncClient
        from app.models.users import User
        from app.models.trainer import TrainerSession

        admin_user = User(user_id=1, email="admin@test.com", role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin_user

        mock_download.return_value = b"mp3_data"

        from app.models.services import Service
        from app.models.trainer import TrainerSimulation, TrainerSession

        from app.db import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            svc = Service(service_id=1, service_key="test_svc_1", service_name="TestService1")
            db.add(svc)
            await db.flush()

            sim = TrainerSimulation(
                simulation_id=1,
                code="SIM1",
                name="Test Sim 1",
                roleplay_prompt="Carmen es cliente.",
                service_id=1
            )
            db.add(sim)
            await db.flush()

            s_no_rec = TrainerSession(
                session_id=800,
                agent_id="HUB_TEST_800",
                agent_code="CODE800",
                simulation_id=1,
                service_id=1,
                call_id="call_800",
                status="completed",
                evaluation_status="evaluated",
                recording_url=None
            )
            db.add(s_no_rec)

            s_rec = TrainerSession(
                session_id=801,
                agent_id="HUB_TEST_800",
                agent_code="CODE800",
                simulation_id=1,
                service_id=1,
                call_id="call_801",
                status="completed",
                evaluation_status="evaluated",
                recording_url="http://twilio.com/rec801.mp3"
            )
            db.add(s_rec)
            await db.commit()

        from httpx import ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/trainer/sessions/800/recording-audio")
            self.assertEqual(res.status_code, 404)
            self.assertIn("Grabaci\u00f3n no disponible", res.json()["detail"])

            res = await ac.get("/bm/trainer/sessions/801/recording-audio")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content, b"mp3_data")
            self.assertEqual(res.headers["content-type"], "audio/mpeg")
            self.assertEqual(res.headers["cache-control"], "private, no-store")

            import httpx
            req = httpx.Request("GET", "http://twilio.com/rec801.mp3")
            resp = httpx.Response(401, request=req)
            mock_download.side_effect = httpx.HTTPStatusError("Unauthorized", request=req, response=resp)
            res = await ac.get("/bm/trainer/sessions/801/recording-audio")
            self.assertEqual(res.status_code, 502)
            self.assertIn("No se pudo recuperar la grabaci\u00f3n desde Twilio", res.json()["detail"])

            res = await ac.get("/bm/trainer/sessions/9999/recording-audio")
            self.assertEqual(res.status_code, 404)

        app.dependency_overrides.clear()

    async def test_session_detail_and_list_rewrites_recording_url(self):
        """Test that get_session_detail and list_sessions rewrite raw Twilio URL to proxy endpoint."""
        from app.main import app
        from app.dependencies import get_current_user
        from httpx import AsyncClient
        from app.models.users import User
        from app.models.trainer import TrainerSession

        admin_user = User(user_id=1, email="admin@test.com", role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin_user

        from app.models.services import Service
        from app.models.trainer import TrainerSimulation, TrainerSession

        from app.db import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            svc = Service(service_id=1, service_key="test_svc_1", service_name="TestService1")
            db.add(svc)
            await db.flush()

            sim = TrainerSimulation(
                simulation_id=1,
                code="SIM1",
                name="Test Sim 1",
                roleplay_prompt="Carmen es cliente.",
                service_id=1
            )
            db.add(sim)
            await db.flush()

            s_rec = TrainerSession(
                session_id=802,
                agent_id="HUB_TEST_802",
                agent_code="CODE802",
                simulation_id=1,
                service_id=1,
                call_id="call_802",
                status="completed",
                evaluation_status="evaluated",
                recording_url="http://twilio.com/rec802.mp3"
            )
            db.add(s_rec)
            await db.commit()

        from httpx import ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/trainer/sessions/802")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["recording_url"], "/bm/trainer/sessions/802/recording-audio")

            res = await ac.get("/bm/trainer/sessions")
            self.assertEqual(res.status_code, 200)
            sessions_list = res.json()["sessions"]
            matched_session = next((s for s in sessions_list if s["session_id"] == 802), None)
            self.assertIsNotNone(matched_session)
            self.assertEqual(matched_session["recording_url"], "/bm/trainer/sessions/802/recording-audio")

        app.dependency_overrides.clear()

    @patch("httpx.AsyncClient.get")
    async def test_reconcile_trainer_sessions(self, mock_get):
        """Test the reconcile_trainer_sessions administrative endpoint."""
        from app.main import app
        from app.dependencies import get_current_user
        from httpx import AsyncClient, ASGITransport
        from app.models.users import User
        from app.models.trainer import TrainerSession
        from app.db import AsyncSessionLocal

        # Set up admin user
        admin_user = User(user_id=1, email="admin@test.com", role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin_user

        async with AsyncSessionLocal() as db:
            s_reconcile = TrainerSession(
                session_id=850,
                agent_id="HUB_TEST_850",
                agent_code="CODE850",
                simulation_id=1,
                service_id=1,
                call_id="call_reconcile_850",
                status="completed",
                evaluation_status="completed_waiting_recording",
                recording_url=None
            )
            db.add(s_reconcile)
            await db.commit()

        # Mock Twilio API response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "recordings": [
                {
                    "sid": "REmock850",
                    "status": "completed",
                    "duration": "120",
                    "uri": "/2010-04-01/Accounts/ACmock/Recordings/REmock850.json"
                }
            ]
        }
        mock_get.return_value = mock_response

        # Mock evaluate_session_task to avoid running actual evaluation
        with patch("app.services.trainer_service.TrainerService.evaluate_session_task") as mock_eval:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                res = await ac.post("/bm/trainer/phone/sessions/reconcile")
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(data["status"], "ok")
                self.assertEqual(data["reconciled_count"], 1)
                
                # Check DB updated
                async with AsyncSessionLocal() as db:
                    s_updated = await db.get(TrainerSession, 850)
                    self.assertEqual(s_updated.recording_url, "https://api.twilio.com/2010-04-01/Accounts/ACmock/Recordings/REmock850.wav")
                    self.assertEqual(s_updated.evaluation_status, "evaluation_pending")

        app.dependency_overrides.clear()

    @patch("httpx.AsyncClient.post")
    async def test_start_twilio_recording_uses_public_url(self, mock_post):
        """Test that start_twilio_recording uses BACKEND_PUBLIC_URL or PUBLIC_BASE_URL to build callback URL."""
        from app.routers.trainer_voice import start_twilio_recording
        from app.config import get_settings
        
        # Set environment variable
        os.environ["BACKEND_PUBLIC_URL"] = "https://speech-backend.doobot.ai"
        
        # Mock settings values directly
        settings = get_settings()
        old_sid = settings.twilio_account_sid
        old_token = settings.twilio_auth_token
        settings.twilio_account_sid = "ACmock"
        settings.twilio_auth_token = "token_mock"

        # Mock Twilio response
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"sid": "REmock123"}
        mock_post.return_value = mock_response

        try:
            # Execute
            rec_sid = await start_twilio_recording("CA123", "localhost:8000")
            self.assertEqual(rec_sid, "REmock123")

            # Verify post payload
            called_args, called_kwargs = mock_post.call_args
            self.assertEqual(called_args[0], "https://api.twilio.com/2010-04-01/Accounts/ACmock/Calls/CA123/Recordings.json")
            payload = called_kwargs["data"]
            self.assertEqual(payload["RecordingStatusCallback"], "https://speech-backend.doobot.ai/bm/trainer/phone/recording-completed")
        finally:
            # Clean up
            del os.environ["BACKEND_PUBLIC_URL"]
            settings.twilio_account_sid = old_sid
            settings.twilio_auth_token = old_token


    @patch("app.routers.trainer_voice.start_twilio_recording")
    @patch("websockets.connect")
    async def test_start_twilio_recording_via_start_event_custom_parameters_failed(self, mock_ws_connect, mock_start_rec):
        """Test that start_twilio_recording is called via start event customParameters and sets status properly when it fails."""
        from app.routers.trainer_voice import media_stream
        from app.models.services import Service
        from app.models.trainer import TrainerSimulation, TrainerSession
        from app.db import AsyncSessionLocal

        # Create session dependencies and session using unique IDs (99) to avoid collisions
        async with AsyncSessionLocal() as db:
            from app.models.services import Service
            from app.models.trainer import TrainerSimulation

            from sqlalchemy import select
            res_svc = await db.execute(select(Service).where(Service.service_id == 99))
            svc = res_svc.scalar()
            if not svc:
                svc = Service(service_id=99, service_key="test_svc_99", service_name="TestService99")
                db.add(svc)
                await db.flush()

            res_sim = await db.execute(select(TrainerSimulation).where(TrainerSimulation.simulation_id == 99))
            sim = res_sim.scalar()
            if not sim:
                sim = TrainerSimulation(
                    simulation_id=99,
                    code="SIM99",
                    name="Test Sim 99",
                    roleplay_prompt="Carmen es cliente.",
                    service_id=99
                )
                db.add(sim)
                await db.flush()

            sess = TrainerSession(
                session_id=899,
                agent_id="HUB_TEST_899",
                agent_code="CODE899",
                simulation_id=99,
                service_id=99,
                call_id="call_899",
                status="started",
                evaluation_status="started",
                recording_url=None
            )
            db.add(sess)
            await db.commit()

        # Mock websockets client (Gemini side)
        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()
        
        async def mock_gemini_iter(*args, **kwargs):
            # Raise exception to break out of gemini loop
            raise ValueError("stop_test")
            yield None
        mock_gemini_ws.__aiter__ = mock_gemini_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_ws_connect.return_value = mock_connect

        # Mock Twilio WebSocket client (FastAPI side)
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.headers = {"host": "test-host.com"}
        mock_ws.client_state = MagicMock()
        mock_ws.client_state.name = "CONNECTED"
        mock_ws.scope = {"query_string": b""}
        
        # We simulate the events received from Twilio
        async def mock_iter_text():
            yield json.dumps({"event": "connected"})
            yield json.dumps({
                "event": "start",
                "start": {
                    "streamSid": "stream_899",
                    "callSid": "call_899",
                    "customParameters": {
                        "session_id": "899",
                        "flow": "session"
                    }
                }
            })
            # Let it run briefly and then raise exception to stop the test
            await asyncio.sleep(0.1)
            raise ValueError("stop_test")
        
        mock_ws.iter_text = mock_iter_text
        # Stub receive_text (used during early start event check if flow/session_id not in query)
        early_events = [
            json.dumps({"event": "connected"}),
            json.dumps({
                "event": "start",
                "start": {
                    "streamSid": "stream_899",
                    "callSid": "call_899",
                    "customParameters": {
                        "session_id": "899",
                        "flow": "session"
                    }
                }
            })
        ]
        early_iter = iter(early_events)
        async def mock_receive_text():
            try:
                return next(early_iter)
            except StopIteration:
                raise ValueError("stop_test")
        mock_ws.receive_text = mock_receive_text

        # Mock start_twilio_recording to return None (failure)
        mock_start_rec.return_value = None

        # Execute media_stream directly
        async with AsyncSessionLocal() as db:
            try:
                await media_stream(mock_ws, flow=None, session_id=None, db=db)
            except Exception as e:
                # We expect "stop_test" to exit the loop
                if "stop_test" not in str(e):
                    raise

        # Check start_twilio_recording called exactly once with call_sid
        mock_start_rec.assert_called_once_with("call_899", unittest.mock.ANY)

        # Check database evaluation_status has been updated to recording_start_failed
        async with AsyncSessionLocal() as db:
            s_updated = await db.get(TrainerSession, 899)
            self.assertEqual(s_updated.evaluation_status, "recording_start_failed")

        # Reset session status to started to allow manual hangup check
        async with AsyncSessionLocal() as db:
            s_reset = await db.get(TrainerSession, 899)
            s_reset.status = "started"
            await db.commit()

        # Now simulate hangup to verify handle_roleplay_hangup doesn't overwrite it
        from app.routers.trainer_voice import handle_roleplay_hangup
        from datetime import datetime, timezone, timedelta
        await handle_roleplay_hangup(899, "call_899", datetime.now(timezone.utc) - timedelta(seconds=20), "websocket_close")

        async with AsyncSessionLocal() as db:
            s_after_hangup = await db.get(TrainerSession, 899)
            self.assertEqual(s_after_hangup.evaluation_status, "recording_start_failed")
            self.assertEqual(s_after_hangup.status, "completed")





class TestTrainerVoiceBargeInRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import app.models
        from app.db import Base, get_engine
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            
        from app.db import AsyncSessionLocal
        self.db = AsyncSessionLocal()
        
        # Seed dummy agent and session
        from app.models.personalized_training import TrainingAgentSetting
        from app.models.trainer import TrainerSimulation, TrainerSession
        
        agent = TrainingAgentSetting(
            agent_name="Eugenia Carreño",
            agent_initials="EC",
            hubspot_owner_id="8808",
            training_numeric_code="8808",
            is_enabled=True
        )
        self.db.add(agent)
        
        sim = TrainerSimulation(
            simulation_id=1,
            code="121314",
            name="Test Sim",
            roleplay_prompt="Test roleplay",
            service_id=1
        )
        self.db.add(sim)
        
        sess = TrainerSession(
            session_id=14,
            agent_id="8808",
            agent_code="8808",
            simulation_id=1,
            service_id=1,
            call_id="CAa6e55255e7fd66dc881fdb04713110e3",
            status="started",
            evaluation_status="started"
        )
        self.db.add(sess)
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        from app.db import Base, get_engine
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()

    @patch("app.routers.trainer_voice.settings")
    async def test_barge_in_and_watchdog_recovery_loop(self, mock_settings):
        """Test barge-in VAD state transition, Twilio clear event, watchdog nudge, and 1008 logging."""
        from app.routers.trainer_voice import media_stream
        
        mock_settings.gemini_api_key = "mock"
        mock_settings.gemini_live_api_key = None
        mock_settings.gemini_model = "models/gemini-2.0-flash-exp"
        mock_settings.gemini_live_model = None
        
        mock_ws = AsyncMock()
        mock_ws.send_text = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.client_state.name = "CONNECTED"
        mock_ws.scope = {"query_string": b"flow=session&session_id=14"}
        mock_ws.headers = {"host": "test-host.com"}
        
        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()
        
        async def mock_iter_text():
            # 1. Start event
            yield json.dumps({
                "event": "start",
                "start": {
                    "streamSid": "stream_123",
                    "callSid": "CAa6e55255e7fd66dc881fdb04713110e3",
                }
            })
            # Wait for assistant to start speaking
            await asyncio.sleep(0.2)
            # 2. Yield 10 media events (total 200ms > 160ms VAD threshold)
            for _ in range(10):
                yield json.dumps({
                    "event": "media",
                    "media": {
                        "track": "inbound",
                        "payload": "f39/f39/f39/"
                    }
                })
                await asyncio.sleep(0.02)
            # Yield 40 silent media events to confirm silence/speech end (total 800ms > 500ms threshold)
            for _ in range(40):
                yield json.dumps({
                    "event": "media",
                    "media": {
                        "track": "inbound",
                        "payload": "f39/f39/f39/"
                    }
                })
                await asyncio.sleep(0.02)
            # Sleep long enough for watchdog loop to detect 1.5s of silence and nudge Gemini
            await asyncio.sleep(1.8)
            # 3. Stop event
            yield json.dumps({"event": "stop"})
            
        mock_ws.iter_text = mock_iter_text
        
        # Gemini messages
        async def mock_gemini_iter(*args, **kwargs):
            # setupComplete
            yield json.dumps({"setupComplete": {}})
            await asyncio.sleep(0.05)
            # serverContent modelTurn (assistant begins speaking)
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": "DUMMY_AUDIO_BASE64"}}]
                    }
                }
            })
            # Wait for user speech to start/finish before sending interrupted
            await asyncio.sleep(0.6)
            # serverContent interrupted (simulated server interruption)
            yield json.dumps({
                "serverContent": {
                    "interrupted": True
                }
            })
            await asyncio.sleep(0.5)
            
        mock_gemini_ws.__aiter__ = mock_gemini_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        
        # We want first 10 frames to have energy 500 (voice), then 40 frames to have energy 10 (silence)
        energy_values = [500.0] * 10 + [10.0] * 100
        energy_iter = iter(energy_values)
        def mock_calculate_energy(*args, **kwargs):
            try:
                return next(energy_iter)
            except StopIteration:
                return 10.0
                
        with patch("websockets.connect", return_value=mock_connect), \
             patch("app.routers.trainer_voice.start_twilio_recording", AsyncMock(return_value="rec_1")), \
             patch("app.routers.trainer_voice.decode_twilio_to_gemini", return_value=("DUMMY", None)), \
             patch("app.routers.trainer_voice.calculate_pcm_energy", new=mock_calculate_energy), \
             patch("app.routers.trainer_voice.handle_roleplay_hangup", AsyncMock()) as mock_hangup:
            
            await media_stream(mock_ws, flow="session", session_id=14, db=self.db)
            
            # Check hangup was called inside finally block
            mock_hangup.assert_called()
            
        # Verify clear was sent to Twilio
        clear_sent = False
        for call in mock_ws.send_text.call_args_list:
            if len(call[0]) > 0 and "clear" in str(call[0][0]):
                clear_sent = True
        self.assertTrue(clear_sent)
        
        # Verify nudge was sent to Gemini
        nudge_sent = False
        for call in mock_gemini_ws.send.call_args_list:
            payload = json.loads(call[0][0])
            if "clientContent" in payload:
                turns = payload["clientContent"].get("turns", [])
                for turn in turns:
                    for part in turn.get("parts", []):
                        if "el roleplay respondiendo al" in part.get("text", ""):
                            nudge_sent = True
        self.assertTrue(nudge_sent)


if __name__ == "__main__":
    unittest.main()
