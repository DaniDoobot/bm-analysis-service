import os
import sys
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///training_voice_test.db"
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
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
)
from app.routers.training_voice import (
    normalize_training_code_input,
    get_attempts_count,
    increment_attempts_count,
    clear_attempts_count,
    handle_verify_agent_code,
    get_active_cycles_for_agent,
    SPANISH_VOICE_RULES,
    IDENTIFICATION_SYSTEM_INSTRUCTION,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


class TestTrainingVoiceIdentification(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Safety Check: double-confirm engine URL is safe
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"
        assert "speechbm_test" not in db_url_str or "sqlite" in db_url_str, "CRITICAL: Database engine points to production database speechbm_test!"
        
        # Mock Gemini API key to avoid websocket early close in tests
        from app.routers.training_voice import settings as voice_settings
        voice_settings.gemini_api_key = "mock_key"

        # Clean old DB file if exists
        if os.path.exists("training_voice_test.db"):
            try:
                os.remove("training_voice_test.db")
            except Exception:
                pass

        # Create all tables in SQLite
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA busy_timeout=5000;"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
            # Create the bm_training_call_attempts table manually (since it is not in metadata anymore)
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
            await db.commit()

    async def asyncTearDown(self):
        import asyncio
        # Give background tasks a brief moment to release DB connections after ws close
        await asyncio.sleep(0.5)
        engine = get_engine()
        await engine.dispose()
        # Clean up database
        if os.path.exists("training_voice_test.db"):
            try:
                os.remove("training_voice_test.db")
            except Exception:
                pass

    def test_normalization_positive_cases(self):
        """Verify the 19 positive phonetic/numeric normalization targets."""
        positives = [
            # Cristina Montenegro
            ("7777", "7777"),
            ("siete siete siete siete", "7777"),
            ("siete mil setecientos setenta y siete", "7777"),
            ("setenta y siete setenta y siete", "7777"),
            # Fernanda Rodrigues
            ("4545", "4545"),
            ("cuatro cinco cuatro cinco", "4545"),
            ("cuarenta y cinco cuarenta y cinco", "4545"),
            ("cuatro mil quinientos cuarenta y cinco", "4545"),
            # Luci Dos Santos
            ("2323", "2323"),
            ("dos tres dos tres", "2323"),
            ("veintitrés veintitrés", "2323"),
            ("dos mil trescientos veintitrés", "2323"),
            # Bryan Herrera
            ("5555", "5555"),
            # Eugenia Carreno
            ("8808", "8808"),
            ("ocho ocho cero ocho", "8808"),
            ("ocho mil ochocientos ocho", "8808"),
            # Santiago Taboada
            ("9909", "9909"),
            ("nueve nueve cero nueve", "9909"),
            ("nueve mil novecientos nueve", "9909"),
        ]
        for raw, expected in positives:
            self.assertEqual(normalize_training_code_input(raw), expected)

    def test_normalization_negative_cases(self):
        """Verify negative normalization inputs return None without padding."""
        negatives = [
            "",
            "siete",
            "setenta y siete",
            "setecientos setenta y siete",
            "7",
            "77",
            "777",
            "77777",
            "código incorrecto",
            "uno dos tres",
        ]
        for raw in negatives:
            self.assertEqual(normalize_training_code_input(raw), None)

    async def test_attempts_database_atomic_operations(self):
        """Test database attempts atomic operations (get, increment, clear)."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # Check initially 0
            count = await get_attempts_count(db, "call_123")
            self.assertEqual(count, 0)
            
            # Increment once
            count = await increment_attempts_count(db, "call_123")
            self.assertEqual(count, 1)
            
            # Increment twice
            count = await increment_attempts_count(db, "call_123")
            self.assertEqual(count, 2)
            
            # Clear count
            await clear_attempts_count(db, "call_123")
            count = await get_attempts_count(db, "call_123")
            self.assertEqual(count, 0)

    async def test_voice_verification_cycle(self):
        """Test voice flow verify_agent_code retry and termination logic."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            mock_ws = MagicMock()
            mock_ws.headers = {"x-forwarded-host": "localhost"}
            mock_ws.identified = False

            # Test 1: Code valid (but no active cycles)
            res = await handle_verify_agent_code("7777", "call_voice", mock_ws, 0)
            self.assertEqual(res["result"]["status"], "no_active_cycles")
            self.assertTrue(mock_ws.identified)
            
            # Reset state
            mock_ws.identified = False
            await clear_attempts_count(db, "call_voice")

            # Test 2: Code invalid (first failure)
            res = await handle_verify_agent_code("9999", "call_voice", mock_ws, 0)
            self.assertEqual(res["result"]["status"], "invalid")
            self.assertEqual(res["attempts"], 1)
            self.assertIn("Dilo dígito a dígito", res["result"]["message"])
            self.assertFalse(mock_ws.identified)

            # Test 3: Code invalid (second failure)
            res = await handle_verify_agent_code("9999", "call_voice", mock_ws, 1)
            self.assertEqual(res["result"]["status"], "invalid")
            self.assertEqual(res["attempts"], 2)
            self.assertIn("Inténtalo de nuevo diciendo los cuatro dígitos", res["result"]["message"])

            # Test 4: Code invalid (third failure and termination)
            res = await handle_verify_agent_code("9999", "call_voice", mock_ws, 2)
            self.assertEqual(res["result"]["status"], "terminate")
            self.assertEqual(res["attempts"], 3)
            self.assertIn("No he podido identificarte después de varios intentos", res["result"]["message"])
            
            # DB attempts should be cleared after final failure
            count = await get_attempts_count(db, "call_voice")
            self.assertEqual(count, 0)

    async def test_disabled_agent(self):
        """Verify that disabled agent codes are rejected."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # Disable Cristina
            stmt = select(TrainingAgentSetting).where(TrainingAgentSetting.training_numeric_code == "7777")
            res = await db.execute(stmt)
            agent = res.scalars().first()
            agent.is_enabled = False
            await db.commit()
            
            mock_ws = MagicMock()
            mock_ws.headers = {"x-forwarded-host": "localhost"}
            
            # Verify code is treated as invalid
            res = await handle_verify_agent_code("7777", "call_disabled", mock_ws, 0)
            self.assertEqual(res["result"]["status"], "invalid")

    async def test_active_in_progress_cycles_retrieval(self):
        """Verify that get_active_cycles_for_agent retrieves in_progress cycles with pending simulations."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # 1. Create a mock report for Cristina (in_progress)
            report = TrainingAgentReport(
                training_report_id=156,
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                status="in_progress",
                period_start=datetime.now(timezone.utc),
                period_end=datetime.now(timezone.utc),
                is_current=True,
            )
            db.add(report)
            await db.commit()

            # 2. Add a simulation prompt and a pending completion status
            prompt = TrainingSimulationPrompt(
                simulation_prompt_id=318,
                training_report_id=156,
                hubspot_owner_id="7777",
                prompt_number=1,
                title="Simulacion 1",
                scenario_type="roleplay",
                prompt_text="Tension alta",
            )
            db.add(prompt)
            await db.commit()

            comp = TrainingCompletionStatus(
                completion_id=310,
                training_report_id=156,
                simulation_prompt_id=318,
                hubspot_owner_id="7777",
                status="pending"
            )
            db.add(comp)
            await db.commit()

            # 3. Call get_active_cycles_for_agent
            active_cycles = await get_active_cycles_for_agent(db, "7777")
            self.assertEqual(len(active_cycles), 1)
            self.assertEqual(active_cycles[0].training_report_id, 156)

    async def test_completed_cycle_not_retrieved(self):
        """Verify that a cycle in 'completed' status is not returned by get_active_cycles_for_agent even if it has a pending completion status (data inconsistency)."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # 1. Create a mock report for Cristina (completed)
            report = TrainingAgentReport(
                training_report_id=999,
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                status="completed",
                period_start=datetime.now(timezone.utc),
                period_end=datetime.now(timezone.utc),
                is_current=True,
            )
            db.add(report)
            await db.commit()

            # 2. Add a simulation prompt and a pending completion status
            prompt = TrainingSimulationPrompt(
                simulation_prompt_id=9999,
                training_report_id=999,
                hubspot_owner_id="7777",
                prompt_number=1,
                title="Simulacion 1",
                scenario_type="roleplay",
                prompt_text="Tension alta",
            )
            db.add(prompt)
            await db.commit()

            comp = TrainingCompletionStatus(
                completion_id=99999,
                training_report_id=999,
                simulation_prompt_id=9999,
                hubspot_owner_id="7777",
                status="pending"
            )
            db.add(comp)
            await db.commit()

            # 3. Call get_active_cycles_for_agent -> should NOT return it
            active_cycles = await get_active_cycles_for_agent(db, "7777")
            matching = [c for c in active_cycles if c.training_report_id == 999]
            self.assertEqual(len(matching), 0)

    def test_dtmf_webhooks_integration(self):
        """Test DTMF verify-numeric-code webhooks and retry limits using TestClient."""
        from fastapi.testclient import TestClient
        from app.main import app
        
        client = TestClient(app)
        
        # 1. Incoming call should initialize/clear attempts
        res = client.post("/bm/training/voice/twilio/incoming-call", data={"CallSid": "call_dtmf_test"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("Connect", res.text)
        
        # 2. Valid DTMF code input (returns no active cycles initially)
        res = client.post("/bm/training/voice/twilio/verify-numeric-code", data={"Digits": "7777", "CallSid": "call_dtmf_test"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("no tienes", res.text)
        self.assertIn("ciclo de entrenamiento", res.text)
        
        # 3. Invalid DTMF code (1st failure)
        res = client.post("/bm/training/voice/twilio/verify-numeric-code", data={"Digits": "9999", "CallSid": "call_dtmf_test2"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("teclado", res.text)
        
        # 4. Invalid DTMF code (2nd failure)
        res = client.post("/bm/training/voice/twilio/verify-numeric-code", data={"Digits": "9999", "CallSid": "call_dtmf_test2"})
        self.assertIn("cuatro", res.text)

        # 5. Invalid DTMF code (3rd failure and hangup)
        res = client.post("/bm/training/voice/twilio/verify-numeric-code", data={"Digits": "9999", "CallSid": "call_dtmf_test2"})
        self.assertIn("Finalizamos la llamada", res.text)
        self.assertIn("Hangup", res.text)

    async def test_name_confirmation_and_redirect_flow(self):
        """Verify that starting a cycle's roleplay returns TwiML with clean connect/stream (no TTS say)."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # Create a mock report for Cristina (in_progress)
            report = TrainingAgentReport(
                training_report_id=200,
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                status="in_progress",
                period_start=datetime.now(timezone.utc),
                period_end=datetime.now(timezone.utc),
                is_current=True,
            )
            db.add(report)
            
            prompt = TrainingSimulationPrompt(
                simulation_prompt_id=500,
                training_report_id=200,
                hubspot_owner_id="7777",
                prompt_number=1,
                title="Simulacion 1",
                scenario_type="roleplay",
                prompt_text="Prompt voice bot",
            )
            db.add(prompt)
            
            comp = TrainingCompletionStatus(
                completion_id=600,
                training_report_id=200,
                simulation_prompt_id=500,
                hubspot_owner_id="7777",
                status="pending"
            )
            db.add(comp)
            await db.commit()

        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        
        # Call verify-numeric-code DTMF endpoint (which should now redirect since we have 1 active cycle!)
        res = client.post("/bm/training/voice/twilio/verify-numeric-code", data={"Digits": "7777", "CallSid": "call_name_test"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("Redirect", res.text)
        self.assertIn("cycle_id=200", res.text)
        
        # Follow redirect manually to start-roleplay
        res_roleplay = client.post("/bm/training/voice/twilio/start-roleplay?agent_id=7777&cycle_id=200&call_sid=call_name_test")
        self.assertEqual(res_roleplay.status_code, 200)
        # Should NOT contain robot TTS Say tag to prevent voice jump
        self.assertNotIn("<Say", res_roleplay.text)
        # Should connect directly to media stream
        self.assertIn("Stream", res_roleplay.text)
        self.assertIn("session_id", res_roleplay.text)

    def test_pronunciation_rules_present(self):
        """Verify that brand name pronunciation guidelines are correctly present in system rules."""
        self.assertIn("Doobot", SPANISH_VOICE_RULES)
        self.assertIn("Dubot", SPANISH_VOICE_RULES)
        self.assertIn("pronunciarse SIEMPRE exactamente como \"Dubot\"", SPANISH_VOICE_RULES)
        
        self.assertIn("Doobot (marca pronunciada siempre exactamente como \"Dubot\")", IDENTIFICATION_SYSTEM_INSTRUCTION)
        self.assertIn("PRONUNCIACIÓN OBLIGATORIA DEL NOMBRE DEL AGENTE", IDENTIFICATION_SYSTEM_INSTRUCTION)

    async def test_combined_voice_and_dtmf_flow(self):
        """Verify alternating voice and DTMF failures use the same database counter."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            mock_ws = MagicMock()
            mock_ws.headers = {"x-forwarded-host": "localhost"}
            mock_ws.identified = False
            
            # 1. First failure via voice
            res = await handle_verify_agent_code("9999", "call_combined", mock_ws, 0)
            self.assertEqual(res["attempts"], 1)
            
            # Check counter in DB is indeed 1
            count = await get_attempts_count(db, "call_combined")
            self.assertEqual(count, 1)
            
            # 2. Second failure via DTMF simulation
            # We mock the verify_numeric_code handler logic on the same session/database
            count = await increment_attempts_count(db, "call_combined")
            self.assertEqual(count, 2)
            
            # 3. Third failure via voice should terminate the call
            res = await handle_verify_agent_code("9999", "call_combined", mock_ws, 2)
            self.assertEqual(res["result"]["status"], "terminate")
            
            # DB attempts should be cleared after 3rd failure
            count = await get_attempts_count(db, "call_combined")
            self.assertEqual(count, 0)

    async def test_status_cleanup(self):
        """Test attempts records are cleared upon call entry, success, or abandonment."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # Setup initial attempts
            await increment_attempts_count(db, "call_cleanup")
            self.assertEqual(await get_attempts_count(db, "call_cleanup"), 1)
            
            # 1. Starting a new call clears old attempts
            from fastapi.testclient import TestClient
            from app.main import app
            client = TestClient(app)
            client.post("/bm/training/voice/twilio/incoming-call", data={"CallSid": "call_cleanup"})
            self.assertEqual(await get_attempts_count(db, "call_cleanup"), 0)
            
            # 2. Setup attempts again
            await increment_attempts_count(db, "call_cleanup")
            mock_ws = MagicMock()
            mock_ws.headers = {"x-forwarded-host": "localhost"}
            # Successful verification clears attempts
            await handle_verify_agent_code("7777", "call_cleanup", mock_ws, 1)
            self.assertEqual(await get_attempts_count(db, "call_cleanup"), 0)

    async def test_websocket_greeting_single_cycle(self):
        """Verify WebSocket greeting for agent with 1 unique active cycle contains Option B confirmation."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            report = TrainingAgentReport(
                training_report_id=201,
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                status="in_progress",
                period_start=datetime.now(timezone.utc),
                period_end=datetime.now(timezone.utc),
                is_current=True,
            )
            db.add(report)
            prompt = TrainingSimulationPrompt(
                simulation_prompt_id=501,
                training_report_id=201,
                hubspot_owner_id="7777",
                prompt_number=1,
                title="Simulacion 1",
                scenario_type="roleplay",
                prompt_text="Prompt voice bot",
            )
            db.add(prompt)
            comp = TrainingCompletionStatus(
                completion_id=601,
                training_report_id=201,
                simulation_prompt_id=501,
                hubspot_owner_id="7777",
                status="pending"
            )
            db.add(comp)
            
            session = TrainingCallSession(
                session_id=1001,
                call_sid="call_ws_test",
                agent_id="7777",
                cycle_id=201,
                conversation_id=501,
                status="in_progress"
            )
            db.add(session)
            await db.commit()

        from fastapi.testclient import TestClient
        from app.main import app
        import asyncio
        import time
        
        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()
        
        async def mock_async_iter(*args, **kwargs):
            yield json.dumps({"setupComplete": {}})
            await asyncio.sleep(2.0)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect):
            client = TestClient(app)
            with client.websocket_connect("/bm/training/voice/twilio/media-stream?session_id=1001") as websocket:
                websocket.send_json({"event": "connected"})
                websocket.send_json({
                    "event": "start",
                    "start": {
                        "streamSid": "stream_123",
                        "callSid": "call_ws_test",
                        "customParameters": {
                            "flow": "simulation",
                            "session_id": "1001"
                        }
                    }
                })
                
                # Give background thread time to run the WS loops
                time.sleep(1.0)
                
                # Check call details in mock send
                calls = mock_gemini_ws.send.call_args_list
                greeting_sent = False
                for c in calls:
                    arg_str = c[0][0]
                    if "clientContent" in arg_str:
                        # Should contain name Cristina
                        self.assertIn("Cristina", arg_str)
                        # Should state that she only has one active cycle
                        self.assertIn("Solo tienes un ciclo activo", arg_str)
                        # Should NOT say "pues vamos con ese"
                        self.assertNotIn("pues vamos con ese", arg_str)
                        greeting_sent = True
                self.assertTrue(greeting_sent)

    async def test_websocket_greeting_multiple_cycles(self):
        """Verify WebSocket greeting for agent with 2 active cycles uses the correct chose-cycle selection confirmation."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # Cycle 1
            report1 = TrainingAgentReport(
                training_report_id=201,
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                status="in_progress",
                period_start=datetime.now(timezone.utc),
                period_end=datetime.now(timezone.utc),
                is_current=True,
            )
            db.add(report1)
            prompt1 = TrainingSimulationPrompt(
                simulation_prompt_id=501,
                training_report_id=201,
                hubspot_owner_id="7777",
                prompt_number=1,
                title="Simulacion 1",
                scenario_type="roleplay",
                prompt_text="Prompt voice bot",
            )
            db.add(prompt1)
            comp1 = TrainingCompletionStatus(
                completion_id=601,
                training_report_id=201,
                simulation_prompt_id=501,
                hubspot_owner_id="7777",
                status="pending"
            )
            db.add(comp1)
            
            # Cycle 2 (forces eligible_cycles_count = 2)
            report2 = TrainingAgentReport(
                training_report_id=202,
                hubspot_owner_id="7777",
                agent_name="Cristina Montenegro",
                agent_initials="CM",
                status="in_progress",
                period_start=datetime.now(timezone.utc),
                period_end=datetime.now(timezone.utc),
                is_current=False,
            )
            db.add(report2)
            prompt2 = TrainingSimulationPrompt(
                simulation_prompt_id=502,
                training_report_id=202,
                hubspot_owner_id="7777",
                prompt_number=1,
                title="Simulacion 2",
                scenario_type="roleplay",
                prompt_text="Prompt voice bot 2",
            )
            db.add(prompt2)
            comp2 = TrainingCompletionStatus(
                completion_id=602,
                training_report_id=202,
                simulation_prompt_id=502,
                hubspot_owner_id="7777",
                status="pending"
            )
            db.add(comp2)
            
            session = TrainingCallSession(
                session_id=1002,
                call_sid="call_ws_test2",
                agent_id="7777",
                cycle_id=201,
                conversation_id=501,
                status="in_progress"
            )
            db.add(session)
            await db.commit()

        from fastapi.testclient import TestClient
        from app.main import app
        import asyncio
        import time
        
        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()
        
        async def mock_async_iter(*args, **kwargs):
            yield json.dumps({"setupComplete": {}})
            await asyncio.sleep(2.0)
            
        mock_gemini_ws.__aiter__ = mock_async_iter
        
        mock_connect = AsyncMock()
        mock_connect.__aenter__.return_value = mock_gemini_ws
        mock_connect.__aexit__.return_value = None
        
        with patch("websockets.connect", return_value=mock_connect):
            client = TestClient(app)
            with client.websocket_connect("/bm/training/voice/twilio/media-stream?session_id=1002") as websocket:
                websocket.send_json({"event": "connected"})
                websocket.send_json({
                    "event": "start",
                    "start": {
                        "streamSid": "stream_123",
                        "callSid": "call_ws_test2",
                        "customParameters": {
                            "flow": "simulation",
                            "session_id": "1002"
                        }
                    }
                })
                
                # Give background thread time to run the WS loops
                time.sleep(1.0)
                
                # Check call details in mock send
                calls = mock_gemini_ws.send.call_args_list
                greeting_sent = False
                for c in calls:
                    arg_str = c[0][0]
                    if "clientContent" in arg_str:
                        # Should contain name Cristina
                        self.assertIn("Cristina", arg_str)
                        # Should say "pues vamos con ese" (as they chose one of the multiple cycles)
                        self.assertIn("pues vamos con ese", arg_str)
                        # Should NOT say "Solo tienes un ciclo activo"
                        self.assertNotIn("Solo tienes un ciclo activo", arg_str)
                        greeting_sent = True
                self.assertTrue(greeting_sent)

if __name__ == "__main__":
    unittest.main()
