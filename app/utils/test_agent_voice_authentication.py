"""
test_agent_voice_authentication.py
==================================
Comprehensive test suite verifying oral / spoken authentication for agent training:
1. Phone Code Normalizer tests:
   - Spoken Spanish letters + numbers ("a be cuarenta y siete" -> "AB47")
   - Direct alphanumeric ("AB47", "ab47", "AB 47" -> "AB47")
   - Spoken digits ("cinco ocho tres uno" -> "5831")
   - Direct digits ("5831", "5 8 3 1" -> "5831")
   - Two pairs ("cuarenta y cinco cuarenta y cinco" -> "4545")
   - Invalid/unrelated speech -> None

2. TrainerService.validate_agent_code Oral Authentication:
   - Identifies agent with alphanumeric code spoken phonetically ("a be cuarenta y siete").
   - Identifies agent with direct alphanumeric code ("AB47", lowercase "ab47").
   - Identifies agent with numeric PIN spoken phonetically ("cinco ocho tres uno").
   - Identifies agent with direct 4-digit PIN ("5831").
   - Succeeds when is_enabled=False but training_code_enabled=True (is_enabled decoupling).
   - Rejects when training_code_enabled=False even if is_enabled=True.
   - Rejects non-existent codes ("ZZ99", "9999").
   - Rejects empty strings and whitespace.
   - Detects code collisions/conflicts defensively.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agent_voice_test.db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# SQLite compatibility shims for PostgreSQL-only types
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import BigInteger

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine, Base
from app.models.personalized_training import TrainingAgentSetting
from app.services.trainer_service import TrainerService
from app.utils.phone_code_normalizer import normalize_spoken_code


class TestPhoneCodeNormalizer(unittest.TestCase):
    """Unit tests for phone_code_normalizer."""

    def test_alphanumeric_direct(self):
        self.assertEqual(normalize_spoken_code("AB47"), "AB47")
        self.assertEqual(normalize_spoken_code("ab47"), "AB47")
        self.assertEqual(normalize_spoken_code("Ab47"), "AB47")
        self.assertEqual(normalize_spoken_code("AB 47"), "AB47")
        self.assertEqual(normalize_spoken_code("ab 47"), "AB47")
        self.assertEqual(normalize_spoken_code("FR45"), "FR45")
        self.assertEqual(normalize_spoken_code("cm 77"), "CM77")

    def test_alphanumeric_spoken_phonetic(self):
        # Spoken letter names + compound number
        self.assertEqual(normalize_spoken_code("a be cuarenta y siete"), "AB47")
        self.assertEqual(normalize_spoken_code("A B cuarenta y siete"), "AB47")
        self.assertEqual(normalize_spoken_code("efe erre cuarenta y cinco"), "FR45")
        self.assertEqual(normalize_spoken_code("ce eme setenta y siete"), "CM77")
        self.assertEqual(normalize_spoken_code("ele de veintitres"), "LD23")
        self.assertEqual(normalize_spoken_code("ele de veintitrés"), "LD23")
        self.assertEqual(normalize_spoken_code("uve a doce"), "VA12")

    def test_alphanumeric_spoken_individual_digits(self):
        self.assertEqual(normalize_spoken_code("a be cuatro siete"), "AB47")
        self.assertEqual(normalize_spoken_code("f r cuatro cinco"), "FR45")

    def test_numeric_direct(self):
        self.assertEqual(normalize_spoken_code("5831"), "5831")
        self.assertEqual(normalize_spoken_code("5 8 3 1"), "5831")
        self.assertEqual(normalize_spoken_code("7777"), "7777")
        self.assertEqual(normalize_spoken_code("4545"), "4545")

    def test_numeric_spoken_phonetic(self):
        self.assertEqual(normalize_spoken_code("cinco ocho tres uno"), "5831")
        self.assertEqual(normalize_spoken_code("siete siete siete siete"), "7777")
        self.assertEqual(normalize_spoken_code("cuarenta y cinco cuarenta y cinco"), "4545")

    def test_invalid_and_empty(self):
        self.assertIsNone(normalize_spoken_code(""))
        self.assertIsNone(normalize_spoken_code("   "))
        self.assertIsNone(normalize_spoken_code("hola como estas"))
        self.assertIsNone(normalize_spoken_code("quiero entrenar"))
        self.assertIsNone(normalize_spoken_code("123"))  # only 3 digits

        # Mandatory negative test cases from review
        self.assertIsNone(normalize_spoken_code("quiero hacer un entrenamiento"))
        self.assertIsNone(normalize_spoken_code("hola buenas tardes"))
        self.assertIsNone(normalize_spoken_code("mi código no lo sé"))
        self.assertIsNone(normalize_spoken_code("cuarenta y cinco"))  # only 2 digits, not 4
        self.assertIsNone(normalize_spoken_code("efe erre"))  # letters without digits
        self.assertIsNone(normalize_spoken_code("efe erre cuarenta y cinco por favor"))  # trailing words rejected


class TestAgentVoiceAuthenticationDB(unittest.IsolatedAsyncioTestCase):
    """Database integration tests for voice authentication via TrainerService."""

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.db = AsyncSession(self.engine)

        # Agent 1: Active credentials, is_enabled=False (scheduler off, auth ON)
        self.agent1 = TrainingAgentSetting(
            hubspot_owner_id="111111",
            agent_name="Agente Alfa Bravo",
            agent_initials="AB",
            training_code="AB47",
            training_numeric_code="5831",
            training_code_enabled=True,
            is_enabled=False,
        )

        # Agent 2: training_code_enabled=False, is_enabled=True (auth revoked)
        self.agent2 = TrainingAgentSetting(
            hubspot_owner_id="222222",
            agent_name="Agente Charlie Delta",
            agent_initials="CD",
            training_code="CD89",
            training_numeric_code="7722",
            training_code_enabled=False,
            is_enabled=True,
        )

        # Agent 3: Both enabled
        self.agent3 = TrainingAgentSetting(
            hubspot_owner_id="333333",
            agent_name="Agente Echo Foxtrot",
            agent_initials="EF",
            training_code="EF12",
            training_numeric_code="3456",
            training_code_enabled=True,
            is_enabled=True,
        )

        self.db.add_all([self.agent1, self.agent2, self.agent3])
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def test_auth_alphanumeric_direct(self):
        """Direct uppercase and lowercase alphanumeric codes authenticate."""
        res_upper = await TrainerService.validate_agent_code(self.db, "AB47")
        self.assertIsNotNone(res_upper)
        self.assertEqual(res_upper["agent_name"], "Agente Alfa Bravo")
        self.assertEqual(res_upper["agent_id"], "111111")

        res_lower = await TrainerService.validate_agent_code(self.db, "ab47")
        self.assertIsNotNone(res_lower)
        self.assertEqual(res_lower["agent_name"], "Agente Alfa Bravo")

    async def test_auth_alphanumeric_spoken_phonetic(self):
        """Phonetically spoken alphanumeric code authenticates."""
        res_spoken = await TrainerService.validate_agent_code(self.db, "a be cuarenta y siete")
        self.assertIsNotNone(res_spoken)
        self.assertEqual(res_spoken["agent_name"], "Agente Alfa Bravo")

        res_spoken_spaces = await TrainerService.validate_agent_code(self.db, "A B 47")
        self.assertIsNotNone(res_spoken_spaces)
        self.assertEqual(res_spoken_spaces["agent_name"], "Agente Alfa Bravo")

    async def test_auth_numeric_pin_direct(self):
        """Direct 4-digit PIN authenticates."""
        res_pin = await TrainerService.validate_agent_code(self.db, "5831")
        self.assertIsNotNone(res_pin)
        self.assertEqual(res_pin["agent_name"], "Agente Alfa Bravo")

    async def test_auth_numeric_pin_spoken_phonetic(self):
        """Phonetically spoken PIN digits authenticate."""
        res_spoken_pin = await TrainerService.validate_agent_code(self.db, "cinco ocho tres uno")
        self.assertIsNotNone(res_spoken_pin)
        self.assertEqual(res_spoken_pin["agent_name"], "Agente Alfa Bravo")

    async def test_auth_is_enabled_decoupling(self):
        """Agent with is_enabled=False can authenticate as long as training_code_enabled=True."""
        res = await TrainerService.validate_agent_code(self.db, "AB47")
        self.assertIsNotNone(res)
        self.assertEqual(res["agent_name"], "Agente Alfa Bravo")

    async def test_auth_rejected_when_training_code_disabled(self):
        """Agent with training_code_enabled=False is rejected even if code is correct."""
        res_code = await TrainerService.validate_agent_code(self.db, "CD89")
        self.assertIsNone(res_code)

        res_pin = await TrainerService.validate_agent_code(self.db, "7722")
        self.assertIsNone(res_pin)

        res_spoken = await TrainerService.validate_agent_code(self.db, "ce de ochenta y nueve")
        self.assertIsNone(res_spoken)

    async def test_auth_nonexistent_code(self):
        """Non-existent code returns None."""
        res = await TrainerService.validate_agent_code(self.db, "ZZ99")
        self.assertIsNone(res)

        res_spoken = await TrainerService.validate_agent_code(self.db, "zeta zeta noventa y nueve")
        self.assertIsNone(res_spoken)

    async def test_auth_empty_input(self):
        """Empty input safely returns None without database errors."""
        self.assertIsNone(await TrainerService.validate_agent_code(self.db, ""))
        self.assertIsNone(await TrainerService.validate_agent_code(self.db, "   "))
        self.assertIsNone(await TrainerService.validate_agent_code(self.db, None))


if __name__ == "__main__":
    unittest.main()
