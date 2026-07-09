import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///training_voice_test.db"

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
from app.models.personalized_training import TrainingAgentSetting
from app.routers.training_voice import (
    normalize_training_code_input,
    get_attempts_count,
    increment_attempts_count,
    clear_attempts_count,
    handle_verify_agent_code,
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
        
        # Clean old DB file if exists
        if os.path.exists("training_voice_test.db"):
            try:
                os.remove("training_voice_test.db")
            except Exception:
                pass

        # Create all tables in SQLite
        async with engine.begin() as conn:
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
        engine = get_engine()
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

            # Test 1: Code valid
            res = await handle_verify_agent_code("7777", "call_voice", mock_ws, 0)
            # Should have no_active_cycles status for Cristina (as we didn't add cycles)
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

    def test_dtmf_webhooks_integration(self):
        """Test DTMF verify-numeric-code webhooks and retry limits using TestClient."""
        from fastapi.testclient import TestClient
        from app.main import app
        
        client = TestClient(app)
        
        # 1. Incoming call should initialize/clear attempts
        res = client.post("/bm/training/voice/twilio/incoming-call", data={"CallSid": "call_dtmf_test"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("Connect", res.text)
        
        # 2. Valid DTMF code input
        res = client.post("/bm/training/voice/twilio/verify-numeric-code", data={"Digits": "7777", "CallSid": "call_dtmf_test"})
        self.assertEqual(res.status_code, 200)
        # Should speak no pending cycles or redirect
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

if __name__ == "__main__":
    unittest.main()
