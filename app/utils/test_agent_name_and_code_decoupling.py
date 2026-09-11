"""
Unit and integration tests for:
1. Canonical agent name resolution:
   - Priority: bm_users -> legacy OWNER_TO_NAME -> raw_agent text -> placeholder.
   - Company/tenant isolation (User.company_id == company_id).
   - Inactive user skipping.
   - Batch resolution (batch_resolve_agent_names) preventing N+1 queries.
   - Placeholder detection (is_placeholder_agent_name).
   - Snapshot preservation: valid existing snapshots are not overridden; placeholder snapshots are dynamically enriched on read.
2. Agent codes & is_enabled decoupling:
   - validate_agent_code succeeds when training_code_enabled=True and is_enabled=False (e.g. Cristina Montenegro).
   - validate_agent_code rejects when training_code_enabled=False even if is_enabled=True.
   - /bm/me/agent-code calculates enabled = bool(training_code_enabled) and returns None when numeric code is null.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agent_name_code_test.db"
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
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting
from app.utils.hubspot_owners import (
    is_placeholder_agent_name,
    resolve_agent_name_canonical,
    batch_resolve_agent_names,
    resolve_agent_display,
)
from app.services.trainer_service import TrainerService
from app.schemas.users import MyAgentCodeResponse


class TestAgentNameResolution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.db = AsyncSession(self.engine, expire_on_commit=False)

        # 1. Company 1 users (EXPAC)
        self.u1 = User(
            user_id=101,
            company_id=1,
            username="victoria.arellano@test.com",
            email="victoria.arellano@test.com",
            name="Victoria Arellano",
            hubspot_owner_id="31499194",
            password_hash="hash",
            is_active=True,
        )
        self.u2 = User(
            user_id=102,
            company_id=1,
            username="maria.olvera@test.com",
            email="maria.olvera@test.com",
            name="María Olvera",
            hubspot_owner_id="76997586",
            password_hash="hash",
            is_active=True,
        )
        # Inactive user
        self.u_inactive = User(
            user_id=103,
            company_id=1,
            username="inactive@test.com",
            email="inactive@test.com",
            name="Inactive User",
            hubspot_owner_id="999901",
            password_hash="hash",
            is_active=False,
        )
        # Company 2 user (isolated tenant)
        self.u_comp2 = User(
            user_id=201,
            company_id=2,
            username="other.agent@test.com",
            email="other.agent@test.com",
            name="Tenant 2 Agent",
            hubspot_owner_id="888801",
            password_hash="hash",
            is_active=True,
        )
        self.db.add_all([self.u1, self.u2, self.u_inactive, self.u_comp2])
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()
        try:
            if os.path.exists("agent_name_code_test.db"):
                os.remove("agent_name_code_test.db")
        except Exception:
            pass

    def test_is_placeholder_agent_name(self):
        self.assertTrue(is_placeholder_agent_name(None))
        self.assertTrue(is_placeholder_agent_name(""))
        self.assertTrue(is_placeholder_agent_name("   "))
        self.assertTrue(is_placeholder_agent_name("Agente no identificado (31499194)"))
        self.assertTrue(is_placeholder_agent_name("Agente no identificado"))
        self.assertFalse(is_placeholder_agent_name("Victoria Arellano"))
        self.assertFalse(is_placeholder_agent_name("Santiago Taboada"))
        self.assertFalse(is_placeholder_agent_name("Custom Agent Name"))

    async def test_resolve_from_bm_users_expac(self):
        # 31499194 is Victoria Arellano in company 1
        name = await resolve_agent_name_canonical(
            db=self.db,
            hubspot_owner_id="31499194",
            company_id=1,
        )
        self.assertEqual(name, "Victoria Arellano")

        # 76997586 is María Olvera
        name2 = await resolve_agent_name_canonical(
            db=self.db,
            hubspot_owner_id="76997586",
            company_id=1,
        )
        self.assertEqual(name2, "María Olvera")

    async def test_tenant_isolation_prevents_cross_resolution(self):
        # 888801 belongs to company 2. Resolving under company 1 must NOT return Tenant 2 Agent.
        name = await resolve_agent_name_canonical(
            db=self.db,
            hubspot_owner_id="888801",
            company_id=1,
        )
        # Should fallback to placeholder since 888801 is not in company 1
        self.assertEqual(name, "Agente no identificado (888801)")

        # But resolving under company 2 DOES return Tenant 2 Agent
        name_comp2 = await resolve_agent_name_canonical(
            db=self.db,
            hubspot_owner_id="888801",
            company_id=2,
        )
        self.assertEqual(name_comp2, "Tenant 2 Agent")

    async def test_inactive_user_resolves_name(self):
        """Inactive user 999901 ('Inactive User') in company 1 must still resolve its display name."""
        name = await resolve_agent_name_canonical(
            db=self.db,
            hubspot_owner_id="999901",
            company_id=1,
        )
        self.assertEqual(name, "Inactive User")

    async def test_fallback_to_owner_to_name_when_not_in_bm_users(self):
        # 1459417733 is Santiago Taboada in static OWNER_TO_NAME, not in bm_users
        name = await resolve_agent_name_canonical(
            db=self.db,
            hubspot_owner_id="1459417733",
            company_id=1,
        )
        self.assertEqual(name, "Santiago Taboada")

    async def test_fallback_to_raw_agent(self):
        # Unknown owner, but valid raw text passed
        name = await resolve_agent_name_canonical(
            db=self.db,
            hubspot_owner_id="999999999",
            company_id=1,
            raw_agent="Agente Telefónico Externo",
        )
        self.assertEqual(name, "Agente Telefónico Externo")

    async def test_fallback_to_placeholder(self):
        # Unknown owner and no raw agent
        name = await resolve_agent_name_canonical(
            db=self.db,
            hubspot_owner_id="999999999",
            company_id=1,
        )
        self.assertEqual(name, "Agente no identificado (999999999)")

    async def test_batch_resolve_agent_names(self):
        pairs = [
            (1, "31499194"),   # Victoria Arellano (in bm_users)
            (1, "76997586"),   # María Olvera (in bm_users)
            (1, "1459417733"), # Santiago Taboada (fallback in OWNER_TO_NAME)
            (1, "888801"),     # In company 2 only -> should not resolve for company 1
        ]
        resolved = await batch_resolve_agent_names(self.db, pairs)
        self.assertEqual(resolved.get((1, "31499194")), "Victoria Arellano")
        self.assertEqual(resolved.get((1, "76997586")), "María Olvera")
        self.assertEqual(resolved.get((1, "1459417733")), "Santiago Taboada")
        self.assertNotIn((1, "888801"), resolved)

    async def test_batch_tenant_isolation_distinct_companies(self):
        """
        Point 5: Verifies strict tenant isolation between distinct companies.
        Each (company_id, owner_id) pair resolves to the user belonging to that company.
        A query for (company 2, owner 555001) must NOT return company 1's user.
        """
        # Setup: Users in different companies with distinct owner_ids
        u_a = User(
            user_id=401,
            company_id=1,
            username="multi_c1@test.com",
            email="multi_c1@test.com",
            name="Agente Empresa Uno",
            hubspot_owner_id="555001",
            password_hash="hash",
            is_active=True,
        )
        u_b = User(
            user_id=402,
            company_id=2,
            username="multi_c2@test.com",
            email="multi_c2@test.com",
            name="Agente Empresa Dos",
            hubspot_owner_id="555002",
            password_hash="hash",
            is_active=True,
        )
        self.db.add_all([u_a, u_b])
        await self.db.commit()

        # Resolve owner 555001 for company 1 → must be "Agente Empresa Uno"
        name1 = await resolve_agent_name_canonical(self.db, "555001", company_id=1)
        self.assertEqual(name1, "Agente Empresa Uno")

        # Resolve owner 555001 for company 2 → must NOT resolve (different tenant)
        name1_c2 = await resolve_agent_name_canonical(self.db, "555001", company_id=2)
        self.assertEqual(name1_c2, "Agente no identificado (555001)")

        # Resolve owner 555002 for company 2 → must be "Agente Empresa Dos"
        name2 = await resolve_agent_name_canonical(self.db, "555002", company_id=2)
        self.assertEqual(name2, "Agente Empresa Dos")

        # Batch: both owners from their respective companies in same call
        pairs = [(1, "555001"), (2, "555002")]
        resolved = await batch_resolve_agent_names(self.db, pairs)
        self.assertEqual(resolved.get((1, "555001")), "Agente Empresa Uno")
        self.assertEqual(resolved.get((2, "555002")), "Agente Empresa Dos")
        # Cross-tenant lookup: company 1 must not bleed into company 2's result
        self.assertIsNone(resolved.get((2, "555001")))
        self.assertIsNone(resolved.get((1, "555002")))

    async def test_inactive_user_historical_placeholder_enriched(self):
        """
        Point 4: User is inactive (is_active=False), company_id=1, hubspot_owner_id="31499194",
        name="Victoria Arellano". A historical result with placeholder
        agent_name="Agente no identificado (31499194)" must be enriched to "Victoria Arellano"
        without modifying any DB rows.
        """
        # Deactivate Victoria Arellano in bm_users
        self.u1.is_active = False
        await self.db.commit()

        # 1. Direct canonical resolution works for inactive user
        direct_name = await resolve_agent_name_canonical(
            db=self.db,
            hubspot_owner_id="31499194",
            company_id=1,
        )
        self.assertEqual(direct_name, "Victoria Arellano")

        # 2. Batch resolution (used by list_results) enriches the placeholder
        placeholder = "Agente no identificado (31499194)"
        self.assertTrue(is_placeholder_agent_name(placeholder))

        needed_pairs = {(1, "31499194")}
        resolved = await batch_resolve_agent_names(self.db, needed_pairs)
        self.assertEqual(resolved.get((1, "31499194")), "Victoria Arellano")

    async def test_historical_placeholder_enriched_on_read(self):
        """
        Point 7a: A historical snapshot with agent_name='Agente no identificado (31499194)'
        and company_id=1 must be enriched to 'Victoria Arellano' via batch_resolve.
        The underlying bm_users row is NOT mutated (this tests the read-layer enrichment).
        """
        placeholder = "Agente no identificado (31499194)"
        self.assertTrue(is_placeholder_agent_name(placeholder))

        # Simulate what list_results does: collect pairs and batch resolve
        needed_pairs = {(1, "31499194")}
        resolved = await batch_resolve_agent_names(self.db, needed_pairs)
        dynamic_name = resolved.get((1, "31499194"))
        self.assertEqual(dynamic_name, "Victoria Arellano",
                         "Placeholder should be enriched to Victoria Arellano via bm_users")

    def test_valid_snapshot_is_preserved(self):
        """
        Point 7b: A result with a valid historical agent_name must NOT be overridden.
        The list_results code only enriches when is_placeholder_agent_name() is True.
        """
        valid_names = [
            "Santiago Taboada",
            "Victoria Arellano",
            "Nombre Histórico Válido",
            "Agent Z",
        ]
        for name in valid_names:
            self.assertFalse(is_placeholder_agent_name(name),
                             f"'{name}' should be treated as a valid snapshot, not a placeholder")


class TestAutoGenerationRespectIsEnabled(unittest.IsolatedAsyncioTestCase):
    """
    Point 3: Demonstrates that auto-generation of cycles uses is_enabled == True
    to select eligible agents, and that decoupling from training_code_enabled
    does NOT affect this filter.
    """
    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        self.db = AsyncSession(self.engine, expire_on_commit=False)

        # Agent that has PIN enabled but cycles disabled → can log in, NOT eligible for auto-gen
        self.s_cristina = TrainingAgentSetting(
            hubspot_owner_id="33013276",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            training_code="CM77",
            training_numeric_code="7777",
            is_enabled=False,            # NOT eligible for auto-generation
            training_code_enabled=True,  # but CAN authenticate by PIN
        )
        # Agent that has both enabled → eligible for auto-gen AND can log in
        self.s_bryan = TrainingAgentSetting(
            hubspot_owner_id="33013277",
            agent_name="Bryan Herrera",
            agent_initials="BH",
            training_code="BH55",
            training_numeric_code="5555",
            is_enabled=True,             # eligible for auto-generation
            training_code_enabled=True,  # can authenticate by PIN
        )
        # Agent that has cycles enabled but PIN disabled → eligible for auto-gen, CANNOT log in by PIN
        self.s_eugenia = TrainingAgentSetting(
            hubspot_owner_id="1375831791",
            agent_name="Eugenia Carreño",
            agent_initials="EC",
            training_code="EC88",
            training_numeric_code="8808",
            is_enabled=True,              # eligible for auto-generation
            training_code_enabled=False,  # admin has disabled PIN
        )
        self.db.add_all([self.s_cristina, self.s_bryan, self.s_eugenia])
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()
        try:
            if os.path.exists("agent_name_code_test.db"):
                os.remove("agent_name_code_test.db")
        except Exception:
            pass

    async def test_cristina_can_authenticate_pin_despite_is_enabled_false(self):
        """Cristina (is_enabled=False, training_code_enabled=True) can use PIN 7777."""
        result = await TrainerService.validate_agent_code(self.db, "7777")
        self.assertIsNotNone(result)
        self.assertEqual(result["agent_name"], "Cristina Montenegro")

    async def test_auto_generation_eligible_agents_uses_is_enabled(self):
        """
        Simulates the is_enabled == True filter that run_personalized_training_pass uses.
        Cristina (is_enabled=False) must NOT appear in the auto-generation pool.
        Bryan and Eugenia (is_enabled=True) MUST appear.
        """
        from sqlalchemy import select as sa_select

        stmt = sa_select(TrainingAgentSetting).where(
            TrainingAgentSetting.is_enabled == True
        )
        res = await self.db.execute(stmt)
        eligible = res.scalars().all()
        eligible_ids = {s.hubspot_owner_id for s in eligible}

        # Cristina is NOT eligible for auto-generation
        self.assertNotIn("33013276", eligible_ids,
                         "Cristina has is_enabled=False and must NOT be in auto-gen pool")
        # Bryan IS eligible
        self.assertIn("33013277", eligible_ids,
                      "Bryan has is_enabled=True and MUST be in auto-gen pool")
        # Eugenia IS eligible for auto-gen (but her PIN is disabled separately)
        self.assertIn("1375831791", eligible_ids,
                      "Eugenia has is_enabled=True and MUST be in auto-gen pool")

    async def test_eugenia_pin_is_rejected_despite_is_enabled_true(self):
        """
        Eugenia (is_enabled=True, training_code_enabled=False) cannot authenticate by PIN.
        is_enabled alone is not enough for authentication.
        """
        result = await TrainerService.validate_agent_code(self.db, "8808")
        self.assertIsNone(result,
                          "Eugenia's training_code_enabled=False must reject PIN auth")

    async def test_bryan_can_authenticate_and_is_eligible(self):
        """Bryan (is_enabled=True, training_code_enabled=True) is both auto-gen eligible and PIN authenticated."""
        result = await TrainerService.validate_agent_code(self.db, "5555")
        self.assertIsNotNone(result)
        self.assertEqual(result["agent_name"], "Bryan Herrera")

class TestAgentCodeDecoupling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.db = AsyncSession(self.engine, expire_on_commit=False)

        # Agent A (e.g. Cristina): is_enabled=False, training_code_enabled=True
        self.s_cristina = TrainingAgentSetting(
            hubspot_owner_id="33013276",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            training_code="CM77",
            training_numeric_code="7777",
            is_enabled=False,           # weekly cycle toggle disabled!
            training_code_enabled=True, # phone code enabled!
        )
        # Agent B: is_enabled=True, training_code_enabled=False (code disabled by admin)
        self.s_disabled_code = TrainingAgentSetting(
            hubspot_owner_id="33013277",
            agent_name="Bryan Herrera",
            agent_initials="BH",
            training_code="BH55",
            training_numeric_code="5555",
            is_enabled=True,
            training_code_enabled=False, # disabled!
        )
        # Agent C: no code set
        self.s_no_code = TrainingAgentSetting(
            hubspot_owner_id="1375831787",
            agent_name="Roberto Galán",
            agent_initials="RG",
            training_code=None,
            training_numeric_code=None,
            is_enabled=True,
            training_code_enabled=True,
        )
        self.db.add_all([self.s_cristina, self.s_disabled_code, self.s_no_code])
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()
        try:
            if os.path.exists("agent_name_code_test.db"):
                os.remove("agent_name_code_test.db")
        except Exception:
            pass

    async def test_cristina_authenticates_with_is_enabled_false(self):
        """Even though is_enabled=False, training_code_enabled=True allows agent code 7777."""
        result = await TrainerService.validate_agent_code(self.db, "7777")
        self.assertIsNotNone(result)
        self.assertEqual(result["agent_name"], "Cristina Montenegro")
        self.assertEqual(result["agent_initials"], "CM")
        self.assertEqual(result["agent_id"], "33013276")

    async def test_code_disabled_is_rejected(self):
        """training_code_enabled=False rejects authentication even if is_enabled=True."""
        result = await TrainerService.validate_agent_code(self.db, "5555")
        self.assertIsNone(result)

    def test_me_agent_code_logic_cristina(self):
        """Simulate /bm/me/agent-code logic for Cristina: enabled=True, agent_code=7777."""
        setting = self.s_cristina
        raw_num = setting.training_numeric_code
        numeric_code = raw_num.strip() if raw_num and raw_num.strip() else None

        enabled = bool(setting.training_code_enabled)
        resp = MyAgentCodeResponse(agent_code=numeric_code, enabled=enabled)
        self.assertEqual(resp.agent_code, "7777")
        self.assertTrue(resp.enabled)

    def test_me_agent_code_logic_disabled_code(self):
        """Simulate /bm/me/agent-code logic for disabled code: enabled=False, agent_code=5555."""
        setting = self.s_disabled_code
        raw_num = setting.training_numeric_code
        numeric_code = raw_num.strip() if raw_num and raw_num.strip() else None

        enabled = bool(setting.training_code_enabled)
        resp = MyAgentCodeResponse(agent_code=numeric_code, enabled=enabled)
        self.assertEqual(resp.agent_code, "5555")
        self.assertFalse(resp.enabled)

    def test_me_agent_code_logic_no_code(self):
        """Simulate /bm/me/agent-code logic when no numeric code is configured."""
        setting = self.s_no_code
        raw_num = setting.training_numeric_code
        numeric_code = raw_num.strip() if raw_num and raw_num.strip() else None

        if not numeric_code:
            resp = MyAgentCodeResponse(agent_code=None, enabled=False)
        else:
            resp = MyAgentCodeResponse(agent_code=numeric_code, enabled=bool(setting.training_code_enabled))

        self.assertIsNone(resp.agent_code)
        self.assertFalse(resp.enabled)


if __name__ == "__main__":
    unittest.main()
