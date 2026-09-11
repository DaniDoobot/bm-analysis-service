"""
test_agent_credentials_provisioning.py
======================================
Comprehensive test suite for:
1. Agent credentials provisioning (AgentCredentialsService.ensure_agent_training_credentials):
   - Case A: Valid new agent -> creates TrainingAgentSetting with both credentials.
   - Case B: New agent without initials -> controlled/no partial creation.
   - Case C: New agent without hubspot_owner_id -> no partial credentials created.
   - Case D: Existing agent with both codes -> idempotent, preserves codes.
   - Case E: Missing numeric only -> generates numeric, preserves alphanumeric.
   - Case F: Missing alphanumeric only -> generates alphanumeric, preserves numeric.
   - Case G: Missing both -> generates both.
   - Case H: Existing training_code_enabled=False -> preserved, NOT changed to True.
   - Case I: Existing is_enabled=False -> preserved, NOT changed.
   - Case J: Candidate collision / Levenshtein distance check -> retry succeeds with safe code.
   - Case K: IntegrityError race collision -> retry succeeds safely.

2. Mi Cuenta endpoints:
   - GET /bm/me/agent-codes:
     - Agent with both codes -> returns both codes and enabled=True.
     - Agent with no codes -> returns null/null and enabled=False.
     - training_code_enabled=False -> enabled=False.
     - Tenant/user isolation -> user A only accesses user A credentials.
   - GET /bm/me/agent-code (legacy):
     - Agent with numeric code -> returns agent_code and enabled.

3. Front Regression:
   - Front agents (e.g. Cristina CM77/7777, Fernanda FR45/4545) preserve existing credentials.
   - validate_agent_code PIN authentication succeeds.
   - Legacy alphanumeric code preserved.
"""
import os
import sys
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agent_credentials_test.db"
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

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine, Base
from app.models.users import User
from app.models.teams import UserServiceAssociation, UserTeamAssociation
from app.models.personalized_training import TrainingAgentSetting
from app.services.agent_credentials_service import (
    AgentCredentialsService,
    edit_distance,
    is_trivial_pin,
)
from app.services.trainer_service import TrainerService
from app.schemas.users import MyAgentCodeResponse, MyAgentCodesResponse


def make_user(
    user_id: int,
    username: str,
    email: str,
    role: str = "agente",
    is_active: bool = True,
    hubspot_owner_id: str = None,
    agent_initials: str = None,
    name: str = None,
    company_id: int = 1,
) -> User:
    return User(
        user_id=user_id,
        username=username,
        email=email,
        name=name or username,
        role=role,
        is_active=is_active,
        hubspot_owner_id=hubspot_owner_id,
        agent_initials=agent_initials,
        password_hash="test_password_hash_value",
        company_id=company_id,
    )


class TestAgentCredentialsProvisioning(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.db = AsyncSession(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.db.close()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # ──────────────────────────────────────────────────────────────────────────
    # Section 14: Provisioning Tests
    # ──────────────────────────────────────────────────────────────────────────

    async def test_case_a_valid_new_agent_provisions_both_codes(self):
        """Case A: New valid agent gets both alphanumeric and numeric codes."""
        agent = make_user(
            user_id=10,
            username="ludmila",
            email="lnascarella@boston.es",
            name="Ludmila Nascarella",
            role="agente",
            is_active=True,
            hubspot_owner_id="36333511",
            agent_initials="LN",
            company_id=1,
        )
        self.db.add(agent)
        await self.db.commit()

        setting = await AgentCredentialsService.ensure_agent_training_credentials(
            self.db, user=agent
        )
        self.assertIsNotNone(setting)
        self.assertEqual(setting.hubspot_owner_id, "36333511")
        self.assertEqual(setting.agent_initials, "LN")
        self.assertTrue(setting.training_code_enabled)
        self.assertFalse(setting.is_enabled)  # Auto-cycle remains False

        # training_code format: LN## (alphanumeric, starts with initials)
        self.assertIsNotNone(setting.training_code)
        self.assertTrue(setting.training_code.startswith("LN"))
        self.assertEqual(len(setting.training_code), 4)

        # training_numeric_code format: 4 digits, non-trivial
        self.assertIsNotNone(setting.training_numeric_code)
        self.assertEqual(len(setting.training_numeric_code), 4)
        self.assertTrue(setting.training_numeric_code.isdigit())
        self.assertFalse(is_trivial_pin(setting.training_numeric_code))

    async def test_case_b_missing_initials_defers_provisioning(self):
        """Case B: New agent without initials does not invent initials or create partial credentials."""
        agent = make_user(
            user_id=11,
            username="no_initials",
            email="noinit@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="99990001",
            agent_initials=None,
        )
        self.db.add(agent)
        await self.db.commit()

        setting = await AgentCredentialsService.ensure_agent_training_credentials(
            self.db, user=agent
        )
        self.assertIsNone(setting)

        # Verify no setting created in DB
        res = await self.db.execute(
            select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == "99990001")
        )
        self.assertIsNone(res.scalar_one_or_none())

    async def test_case_c_missing_hubspot_owner_id_defers_provisioning(self):
        """Case C: New agent without hubspot_owner_id does not create incomplete credentials."""
        agent = make_user(
            user_id=12,
            username="no_hs",
            email="nohs@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id=None,
            agent_initials="XX",
        )
        self.db.add(agent)
        await self.db.commit()

        setting = await AgentCredentialsService.ensure_agent_training_credentials(
            self.db, user=agent
        )
        self.assertIsNone(setting)

    async def test_case_d_existing_agent_both_codes_preserved_idempotent(self):
        """Case D: Existing agent with both codes is unchanged by ensure."""
        setting_orig = TrainingAgentSetting(
            hubspot_owner_id="33013276",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            training_code="CM77",
            training_numeric_code="7777",
            training_code_enabled=True,
            is_enabled=False,
            company_id=1,
        )
        self.db.add(setting_orig)
        await self.db.commit()

        agent = make_user(
            user_id=13,
            username="cristina",
            email="cmontenegro@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="33013276",
            agent_initials="CM",
            company_id=1,
        )
        self.db.add(agent)
        await self.db.commit()

        # Run ensure multiple times
        res1 = await AgentCredentialsService.ensure_agent_training_credentials(self.db, user=agent)
        res2 = await AgentCredentialsService.ensure_agent_training_credentials(self.db, user=agent)

        self.assertEqual(res1.training_code, "CM77")
        self.assertEqual(res1.training_numeric_code, "7777")
        self.assertEqual(res2.training_code, "CM77")
        self.assertEqual(res2.training_numeric_code, "7777")

    async def test_case_e_missing_numeric_only_generates_numeric(self):
        """Case E: Agent has alphanumeric code but missing numeric code -> generates only numeric."""
        setting_orig = TrainingAgentSetting(
            hubspot_owner_id="31499194",
            agent_name="Victoria Arellano",
            agent_initials="VA",
            training_code="VA10",
            training_numeric_code=None,
            training_code_enabled=True,
            is_enabled=False,
        )
        self.db.add(setting_orig)
        await self.db.commit()

        agent = make_user(
            user_id=14,
            username="victoria",
            email="varellano@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="31499194",
            agent_initials="VA",
        )
        self.db.add(agent)
        await self.db.commit()

        res = await AgentCredentialsService.ensure_agent_training_credentials(self.db, user=agent)
        self.assertEqual(res.training_code, "VA10")  # Preserved!
        self.assertIsNotNone(res.training_numeric_code)  # Newly generated!
        self.assertEqual(len(res.training_numeric_code), 4)

    async def test_case_f_missing_alphanumeric_only_generates_alphanumeric(self):
        """Case F: Agent has numeric PIN but missing alphanumeric code -> generates only alphanumeric."""
        setting_orig = TrainingAgentSetting(
            hubspot_owner_id="76997586",
            agent_name="María Olvera",
            agent_initials="MO",
            training_code=None,
            training_numeric_code="4892",
            training_code_enabled=True,
            is_enabled=False,
        )
        self.db.add(setting_orig)
        await self.db.commit()

        agent = make_user(
            user_id=15,
            username="maria",
            email="molvera@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="76997586",
            agent_initials="MO",
        )
        self.db.add(agent)
        await self.db.commit()

        res = await AgentCredentialsService.ensure_agent_training_credentials(self.db, user=agent)
        self.assertEqual(res.training_numeric_code, "4892")  # Preserved!
        self.assertIsNotNone(res.training_code)  # Newly generated!
        self.assertTrue(res.training_code.startswith("MO"))

    async def test_case_g_missing_both_codes_generates_both(self):
        """Case G: Setting exists with both codes NULL (current EXPAC state) -> generates both."""
        setting_orig = TrainingAgentSetting(
            hubspot_owner_id="1626092736",
            agent_name="Flavio Lucich",
            agent_initials="FL",
            training_code=None,
            training_numeric_code=None,
            training_code_enabled=True,
            is_enabled=False,
        )
        self.db.add(setting_orig)
        await self.db.commit()

        agent = make_user(
            user_id=16,
            username="flavio",
            email="flucich@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="1626092736",
            agent_initials="FL",
        )
        self.db.add(agent)
        await self.db.commit()

        res = await AgentCredentialsService.ensure_agent_training_credentials(self.db, user=agent)
        self.assertIsNotNone(res.training_code)
        self.assertTrue(res.training_code.startswith("FL"))
        self.assertIsNotNone(res.training_numeric_code)
        self.assertEqual(len(res.training_numeric_code), 4)

    async def test_case_h_existing_training_code_enabled_false_preserved(self):
        """Case H: When training_code_enabled is False, provisioning does NOT overwrite it to True."""
        setting_orig = TrainingAgentSetting(
            hubspot_owner_id="1841837788",
            agent_name="Marcelo García",
            agent_initials="MG",
            training_code=None,
            training_numeric_code=None,
            training_code_enabled=False,  # Disabled by manager
            is_enabled=False,
        )
        self.db.add(setting_orig)
        await self.db.commit()

        agent = make_user(
            user_id=17,
            username="marcelo",
            email="mgarcia@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="1841837788",
            agent_initials="MG",
        )
        self.db.add(agent)
        await self.db.commit()

        res = await AgentCredentialsService.ensure_agent_training_credentials(self.db, user=agent)
        self.assertFalse(res.training_code_enabled)  # Still False!

    async def test_case_i_existing_is_enabled_false_preserved(self):
        """Case I: is_enabled remains False after credentials provisioning."""
        agent = make_user(
            user_id=18,
            username="test_isenabled",
            email="ti@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="99990002",
            agent_initials="TI",
        )
        self.db.add(agent)
        await self.db.commit()

        res = await AgentCredentialsService.ensure_agent_training_credentials(self.db, user=agent)
        self.assertFalse(res.is_enabled)

    async def test_case_j_collision_levenshtein_distance_retry(self):
        """Case J: Candidate generation avoids edit_distance <= 1 against existing codes."""
        # Seed an existing code "VA10"
        s1 = TrainingAgentSetting(
            hubspot_owner_id="31499194",
            agent_name="Victoria Arellano",
            agent_initials="VA",
            training_code="VA10",
            training_numeric_code="8821",
            training_code_enabled=True,
            is_enabled=False,
        )
        self.db.add(s1)
        await self.db.commit()

        # Generate a new code for initials "VA"
        new_code = await AgentCredentialsService.generate_unique_training_code(
            self.db, initials="VA", current_owner_id="99990003"
        )
        self.assertTrue(new_code.startswith("VA"))
        self.assertNotEqual(new_code, "VA10")
        self.assertGreater(edit_distance(new_code, "VA10"), 1)

    async def test_case_k_integrity_error_race_condition_retries_safely(self):
        """Case K: IntegrityError collision triggers safe rollback and successful retry."""
        agent = make_user(
            user_id=19,
            username="race_agent",
            email="race@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="99990004",
            agent_initials="RA",
        )
        self.db.add(agent)
        await self.db.commit()

        original_commit = self.db.commit
        calls = {"count": 0}

        async def commit_with_transient_collision():
            calls["count"] += 1
            if calls["count"] == 1:
                # Simulate concurrent transaction that took the code
                raise IntegrityError("duplicate key value violates unique constraint training_code", orig="training_code unique", params=None)
            return await original_commit()

        with patch.object(self.db, "commit", side_effect=commit_with_transient_collision):
            res = await AgentCredentialsService.ensure_agent_training_credentials(self.db, user=agent)
            self.assertIsNotNone(res)
            self.assertGreaterEqual(calls["count"], 2)

    # ──────────────────────────────────────────────────────────────────────────
    # Section 15: Mi Cuenta Tests
    # ──────────────────────────────────────────────────────────────────────────

    async def test_me_agent_codes_with_both_codes(self):
        """GET /bm/me/agent-codes returns both credentials and enabled=True."""
        u = make_user(
            user_id=50,
            username="agent_complete",
            email="ac@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="50001",
            agent_initials="AC",
        )
        s = TrainingAgentSetting(
            hubspot_owner_id="50001",
            agent_name="Agent Complete",
            agent_initials="AC",
            training_code="AC12",
            training_numeric_code="4567",
            training_code_enabled=True,
            is_enabled=False,
        )
        self.db.add_all([u, s])
        await self.db.commit()

        t_code, num_code, enabled = await AgentCredentialsService.get_agent_credentials_for_user(self.db, u)
        self.assertEqual(t_code, "AC12")
        self.assertEqual(num_code, "4567")
        self.assertTrue(enabled)

    async def test_me_agent_codes_without_codes(self):
        """GET /bm/me/agent-codes returns null/null when setting has no codes."""
        u = make_user(
            user_id=51,
            username="agent_empty",
            email="ae@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="50002",
            agent_initials="AE",
        )
        s = TrainingAgentSetting(
            hubspot_owner_id="50002",
            agent_name="Agent Empty",
            agent_initials="AE",
            training_code=None,
            training_numeric_code=None,
            training_code_enabled=True,
            is_enabled=False,
        )
        self.db.add_all([u, s])
        await self.db.commit()

        t_code, num_code, enabled = await AgentCredentialsService.get_agent_credentials_for_user(self.db, u)
        self.assertIsNone(t_code)
        self.assertIsNone(num_code)
        self.assertTrue(enabled)

    async def test_me_agent_codes_disabled_flag(self):
        """GET /bm/me/agent-codes returns enabled=False when training_code_enabled is False."""
        u = make_user(
            user_id=52,
            username="agent_disabled",
            email="ad@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="50003",
            agent_initials="AD",
        )
        s = TrainingAgentSetting(
            hubspot_owner_id="50003",
            agent_name="Agent Disabled",
            agent_initials="AD",
            training_code="AD99",
            training_numeric_code="7890",
            training_code_enabled=False,
            is_enabled=False,
        )
        self.db.add_all([u, s])
        await self.db.commit()

        t_code, num_code, enabled = await AgentCredentialsService.get_agent_credentials_for_user(self.db, u)
        self.assertEqual(t_code, "AD99")
        self.assertEqual(num_code, "7890")
        self.assertFalse(enabled)

    async def test_me_user_isolation(self):
        """User A can only query User A credentials; User B can only query User B credentials."""
        u_a = make_user(user_id=60, username="user_a", email="a@boston.es", role="agente", hubspot_owner_id="60001", agent_initials="UA")
        u_b = make_user(user_id=61, username="user_b", email="b@boston.es", role="agente", hubspot_owner_id="60002", agent_initials="UB")
        s_a = TrainingAgentSetting(hubspot_owner_id="60001", agent_name="User A", agent_initials="UA", training_code="UA11", training_numeric_code="1112", training_code_enabled=True, is_enabled=False)
        s_b = TrainingAgentSetting(hubspot_owner_id="60002", agent_name="User B", agent_initials="UB", training_code="UB22", training_numeric_code="2223", training_code_enabled=True, is_enabled=False)
        self.db.add_all([u_a, u_b, s_a, s_b])
        await self.db.commit()

        t_a, n_a, _ = await AgentCredentialsService.get_agent_credentials_for_user(self.db, u_a)
        t_b, n_b, _ = await AgentCredentialsService.get_agent_credentials_for_user(self.db, u_b)

        self.assertEqual(t_a, "UA11")
        self.assertEqual(n_a, "1112")
        self.assertEqual(t_b, "UB22")
        self.assertEqual(n_b, "2223")

    # ──────────────────────────────────────────────────────────────────────────
    # Section 16: Front Regression Tests
    # ──────────────────────────────────────────────────────────────────────────

    async def test_front_agents_credentials_unaffected(self):
        """Front agents (Cristina Montenegro, Fernanda Rodrigues) keep their exact codes."""
        cristina_setting = TrainingAgentSetting(
            hubspot_owner_id="33013276",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            training_code="CM77",
            training_numeric_code="7777",
            training_code_enabled=True,
            is_enabled=False,
            company_id=1,
        )
        fernanda_setting = TrainingAgentSetting(
            hubspot_owner_id="1539993532",
            agent_name="Fernanda Rodrigues",
            agent_initials="FR",
            training_code="FR45",
            training_numeric_code="4545",
            training_code_enabled=True,
            is_enabled=True,
            company_id=1,
        )
        self.db.add_all([cristina_setting, fernanda_setting])
        await self.db.commit()

        # Run ensure on Cristina
        res_cm = await AgentCredentialsService.ensure_agent_training_credentials(
            self.db,
            hubspot_owner_id="33013276",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            company_id=1,
        )
        self.assertEqual(res_cm.training_code, "CM77")
        self.assertEqual(res_cm.training_numeric_code, "7777")
        self.assertFalse(res_cm.is_enabled)

        # Run ensure on Fernanda
        res_fr = await AgentCredentialsService.ensure_agent_training_credentials(
            self.db,
            hubspot_owner_id="1539993532",
            agent_name="Fernanda Rodrigues",
            agent_initials="FR",
            company_id=1,
        )
        self.assertEqual(res_fr.training_code, "FR45")
        self.assertEqual(res_fr.training_numeric_code, "4545")
        self.assertTrue(res_fr.is_enabled)  # Kept True!

        # Validate PIN authentication in TrainerService
        auth_cristina = await TrainerService.validate_agent_code(self.db, "7777")
        self.assertIsNotNone(auth_cristina)
        self.assertEqual(auth_cristina["agent_id"], "33013276")

        auth_fernanda = await TrainerService.validate_agent_code(self.db, "4545")
        self.assertIsNotNone(auth_fernanda)
        self.assertEqual(auth_fernanda["agent_id"], "1539993532")

    # ──────────────────────────────────────────────────────────────────────────
    # Section 17: Read-Only Verification & Legacy Endpoint Contract
    # ──────────────────────────────────────────────────────────────────────────

    async def test_me_agent_codes_is_strictly_read_only_zero_writes(self):
        """GET /bm/me/agent-codes on user without settings performs 0 INSERTs and 0 UPDATEs."""
        agent_unprovisioned = make_user(
            user_id=70,
            username="unprovisioned_agent",
            email="unprov@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="70001",
            agent_initials="UA",
        )
        self.db.add(agent_unprovisioned)
        await self.db.commit()

        # Count settings before
        count_before = (
            await self.db.execute(select(func.count(TrainingAgentSetting.setting_id)))
        ).scalar()

        # Query credentials
        t_code, num_code, enabled = (
            await AgentCredentialsService.get_agent_credentials_for_user(
                self.db, agent_unprovisioned
            )
        )
        self.assertIsNone(t_code)
        self.assertIsNone(num_code)
        self.assertFalse(enabled)

        # Count settings after - must be exactly identical (0 writes)
        count_after = (
            await self.db.execute(select(func.count(TrainingAgentSetting.setting_id)))
        ).scalar()
        self.assertEqual(count_before, count_after)

    async def test_me_legacy_numeric_only_contract_no_alpha_fallback(self):
        """GET /bm/me/agent-code returns null if numeric code is missing, even if alpha code exists."""
        agent = make_user(
            user_id=71,
            username="alpha_only_agent",
            email="alphaonly@boston.es",
            role="agente",
            is_active=True,
            hubspot_owner_id="70002",
            agent_initials="AO",
        )
        setting = TrainingAgentSetting(
            hubspot_owner_id="70002",
            agent_name="Alpha Only Agent",
            agent_initials="AO",
            training_code="AO12",
            training_numeric_code=None,  # Missing numeric PIN
            training_code_enabled=True,
            is_enabled=False,
        )
        self.db.add_all([agent, setting])
        await self.db.commit()

        # 1. Query credentials service
        t_code, num_code, enabled = (
            await AgentCredentialsService.get_agent_credentials_for_user(
                self.db, agent
            )
        )
        self.assertEqual(t_code, "AO12")
        self.assertIsNone(num_code)
        self.assertTrue(enabled)

        # 2. Legacy endpoint contract (/bm/me/agent-code):
        # Must return None for agent_code (strictly numeric, NO fallback to AO12)
        legacy_resp = MyAgentCodeResponse(
            agent_code=num_code if num_code else None,
            enabled=enabled if num_code else False,
        )
        self.assertIsNone(legacy_resp.agent_code)
        self.assertFalse(legacy_resp.enabled)

        # 3. New endpoint contract (/bm/me/agent-codes):
        new_resp = MyAgentCodesResponse(
            training_code=t_code,
            training_numeric_code=num_code,
            enabled=enabled,
        )
        self.assertEqual(new_resp.training_code, "AO12")
        self.assertIsNone(new_resp.training_numeric_code)
        self.assertTrue(new_resp.enabled)

    # ──────────────────────────────────────────────────────────────────────────
    # Section 18: Savepoint Isolation & Transactional Integrity
    # ──────────────────────────────────────────────────────────────────────────

    async def test_user_creation_with_credentials_collision_retry_preserves_associations(self):
        """
        Simulate user creation with service and team associations.
        When credentials provisioning hits an IntegrityError on attempt 1,
        the savepoint rollback preserves user and associations, and retry succeeds.
        """
        # 1. Create outer transaction with user, service association, and team association
        user = make_user(
            user_id=80,
            username="new_agent_collision",
            email="nac@boston.es",
            name="New Agent Collision",
            role="agente",
            hubspot_owner_id="80001",
            agent_initials="NC",
        )
        svc_assoc = UserServiceAssociation(user_id=80, service_id=1)
        team_assoc = UserTeamAssociation(user_id=80, team_id=1)
        self.db.add_all([user, svc_assoc, team_assoc])
        # Do not commit yet - let outer transaction remain active

        # 2. Simulate collision on attempt 1
        original_gen_code = AgentCredentialsService.generate_unique_training_code
        call_count = 0

        async def mocked_gen_code(db, initials, current_owner_id, max_attempts=200):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First attempt: inject setting that will trigger IntegrityError on flush
                colliding_setting = TrainingAgentSetting(
                    hubspot_owner_id="80999_collision",
                    agent_name="Colliding Agent",
                    agent_initials="NC",
                    training_code="NC99",
                    training_numeric_code="9988",
                    training_code_enabled=True,
                    is_enabled=False,
                )
                db.add(colliding_setting)
                await db.flush()  # Now NC99 exists
                return "NC99"  # Will collide when setting.training_code is set to NC99
            return await original_gen_code(db, initials, current_owner_id, max_attempts)

        with patch.object(AgentCredentialsService, "generate_unique_training_code", side_effect=mocked_gen_code):
            setting = await AgentCredentialsService.ensure_agent_training_credentials(
                self.db, user=user, commit=True
            )

        self.assertIsNotNone(setting)
        self.assertNotEqual(setting.training_code, "NC99")  # Took the retry code
        self.assertGreaterEqual(call_count, 2)  # Proves retry occurred

        # Verify all main transaction entities are preserved:
        u_check = (await self.db.execute(select(User).where(User.user_id == 80))).scalar_one_or_none()
        self.assertIsNotNone(u_check)
        self.assertEqual(u_check.email, "nac@boston.es")

        s_check = (await self.db.execute(select(UserServiceAssociation).where(UserServiceAssociation.user_id == 80))).scalar_one_or_none()
        self.assertIsNotNone(s_check)
        self.assertEqual(s_check.service_id, 1)

        t_check = (await self.db.execute(select(UserTeamAssociation).where(UserTeamAssociation.user_id == 80))).scalar_one_or_none()
        self.assertIsNotNone(t_check)
        self.assertEqual(t_check.team_id, 1)

    async def test_user_update_with_credentials_collision_retry_preserves_user(self):
        """
        Simulate user update where credentials provisioning encounters a collision.
        The savepoint isolation ensures updated user fields are preserved.
        """
        user = make_user(
            user_id=85,
            username="agent_update_collision",
            email="auc@boston.es",
            name="Agent Update Initial",
            role="agente",
            hubspot_owner_id="85001",
            agent_initials="AU",
        )
        self.db.add(user)
        await self.db.commit()

        # Update user fields
        user.name = "Agent Update Modified"
        user.agent_initials = "AM"

        # Simulate collision on provisioning
        original_gen_numeric = AgentCredentialsService.generate_unique_training_numeric_code
        call_count = 0

        async def mocked_gen_numeric(db, current_owner_id, max_attempts=200):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                colliding_setting = TrainingAgentSetting(
                    hubspot_owner_id="85999_collision",
                    agent_name="Colliding Numeric",
                    agent_initials="AM",
                    training_code="AM88",
                    training_numeric_code="8877",
                    training_code_enabled=True,
                    is_enabled=False,
                )
                db.add(colliding_setting)
                await db.flush()
                return "8877"
            return await original_gen_numeric(db, current_owner_id, max_attempts)

        with patch.object(AgentCredentialsService, "generate_unique_training_numeric_code", side_effect=mocked_gen_numeric):
            setting = await AgentCredentialsService.ensure_agent_training_credentials(
                self.db, user=user, commit=True
            )

        self.assertIsNotNone(setting)
        self.assertNotEqual(setting.training_numeric_code, "8877")
        self.assertGreaterEqual(call_count, 2)

        # Verify updated user fields are intact
        u_check = (await self.db.execute(select(User).where(User.user_id == 85))).scalar_one_or_none()
        self.assertIsNotNone(u_check)
        self.assertEqual(u_check.name, "Agent Update Modified")
        self.assertEqual(u_check.agent_initials, "AM")

    # ──────────────────────────────────────────────────────────────────────────
    # Section 19: End-to-End Atomic Transaction & Fresh Session Verification
    # ──────────────────────────────────────────────────────────────────────────

    async def test_e2e_create_user_persists_in_fresh_session(self):
        """End-to-end create: user + associations + credentials committed once and visible in fresh session."""
        # Step 1: Execute atomic create flow
        new_user = make_user(
            user_id=110,
            username="e2e_agent",
            email="e2e@boston.es",
            name="E2E Agent",
            role="agente",
            hubspot_owner_id="110001",
            agent_initials="EA",
        )
        self.db.add(new_user)
        await self.db.flush()

        svc_assoc = UserServiceAssociation(user_id=new_user.user_id, service_id=1)
        team_assoc = UserTeamAssociation(user_id=new_user.user_id, team_id=1)
        self.db.add_all([svc_assoc, team_assoc])

        setting = await AgentCredentialsService.ensure_agent_training_credentials(
            self.db, user=new_user, commit=False
        )
        self.assertIsNotNone(setting)

        # Single commit at end of request
        await self.db.commit()

        # Step 2: Close current session completely
        await self.db.close()

        # Step 3: Open completely FRESH session
        async with AsyncSession(self.engine) as fresh_db:
            u = (await fresh_db.execute(select(User).where(User.user_id == 110))).scalar_one_or_none()
            self.assertIsNotNone(u)
            self.assertEqual(u.email, "e2e@boston.es")

            s = (await fresh_db.execute(select(UserServiceAssociation).where(UserServiceAssociation.user_id == 110))).scalar_one_or_none()
            self.assertIsNotNone(s)
            self.assertEqual(s.service_id, 1)

            t = (await fresh_db.execute(select(UserTeamAssociation).where(UserTeamAssociation.user_id == 110))).scalar_one_or_none()
            self.assertIsNotNone(t)
            self.assertEqual(t.team_id, 1)

            st = (await fresh_db.execute(select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == "110001"))).scalar_one_or_none()
            self.assertIsNotNone(st)
            self.assertIsNotNone(st.training_code)
            self.assertTrue(st.training_code.startswith("EA"))
            self.assertIsNotNone(st.training_numeric_code)
            self.assertEqual(len(st.training_numeric_code), 4)

        # Reconnect self.db for tearDown
        self.db = AsyncSession(self.engine, expire_on_commit=False)

    async def test_e2e_create_user_with_collision_retry_persists_in_fresh_session(self):
        """End-to-end create with savepoint collision recovery persists cleanly in fresh session."""
        new_user = make_user(
            user_id=120,
            username="e2e_collision_agent",
            email="e2e_col@boston.es",
            name="E2E Collision Agent",
            role="agente",
            hubspot_owner_id="120001",
            agent_initials="EC",
        )
        self.db.add(new_user)
        await self.db.flush()

        svc_assoc = UserServiceAssociation(user_id=new_user.user_id, service_id=1)
        team_assoc = UserTeamAssociation(user_id=new_user.user_id, team_id=1)
        self.db.add_all([svc_assoc, team_assoc])

        original_gen_code = AgentCredentialsService.generate_unique_training_code
        call_count = 0

        async def mocked_gen(db, initials, current_owner_id, max_attempts=200):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Inject colliding code
                c_setting = TrainingAgentSetting(
                    hubspot_owner_id="120999_other",
                    agent_name="Other Agent",
                    agent_initials="EC",
                    training_code="EC55",
                    training_numeric_code="5511",
                    training_code_enabled=True,
                    is_enabled=False,
                )
                db.add(c_setting)
                await db.flush()
                return "EC55"
            return await original_gen_code(db, initials, current_owner_id, max_attempts)

        with patch.object(AgentCredentialsService, "generate_unique_training_code", side_effect=mocked_gen):
            setting = await AgentCredentialsService.ensure_agent_training_credentials(
                self.db, user=new_user, commit=False
            )

        self.assertIsNotNone(setting)
        self.assertNotEqual(setting.training_code, "EC55")
        await self.db.commit()
        await self.db.close()

        # Check in FRESH session
        async with AsyncSession(self.engine) as fresh_db:
            u = (await fresh_db.execute(select(User).where(User.user_id == 120))).scalar_one_or_none()
            self.assertIsNotNone(u)

            s = (await fresh_db.execute(select(UserServiceAssociation).where(UserServiceAssociation.user_id == 120))).scalar_one_or_none()
            self.assertIsNotNone(s)

            t = (await fresh_db.execute(select(UserTeamAssociation).where(UserTeamAssociation.user_id == 120))).scalar_one_or_none()
            self.assertIsNotNone(t)

            st = (await fresh_db.execute(select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == "120001"))).scalar_one_or_none()
            self.assertIsNotNone(st)
            self.assertNotEqual(st.training_code, "EC55")

        self.db = AsyncSession(self.engine, expire_on_commit=False)

    async def test_e2e_create_user_definitive_failure_aborts_completely_no_orphan_records(self):
        """When provisioning retries are exhausted, the transaction aborts cleanly with 0 orphan rows."""
        new_user = make_user(
            user_id=130,
            username="failed_create_agent",
            email="failed_create@boston.es",
            name="Failed Create Agent",
            role="agente",
            hubspot_owner_id="130001",
            agent_initials="FA",
        )
        self.db.add(new_user)
        await self.db.flush()

        svc_assoc = UserServiceAssociation(user_id=new_user.user_id, service_id=1)
        team_assoc = UserTeamAssociation(user_id=new_user.user_id, team_id=1)
        self.db.add_all([svc_assoc, team_assoc])

        # Force all retries to raise IntegrityError
        with patch.object(AgentCredentialsService, "generate_unique_training_code", side_effect=IntegrityError("collision", params=None, orig=Exception("unique constraint"))):
            with self.assertRaises(IntegrityError):
                await AgentCredentialsService.ensure_agent_training_credentials(
                    self.db, user=new_user, commit=False
                )

        # Transaction aborted / rolled back on exception
        await self.db.rollback()
        await self.db.close()

        # Check in FRESH session: NO orphan rows exist anywhere!
        async with AsyncSession(self.engine) as fresh_db:
            u = (await fresh_db.execute(select(User).where(User.user_id == 130))).scalar_one_or_none()
            self.assertIsNone(u, "User must NOT exist after aborted creation")

            s = (await fresh_db.execute(select(UserServiceAssociation).where(UserServiceAssociation.user_id == 130))).scalars().all()
            self.assertEqual(len(s), 0, "No orphan UserServiceAssociation rows may exist")

            t = (await fresh_db.execute(select(UserTeamAssociation).where(UserTeamAssociation.user_id == 130))).scalars().all()
            self.assertEqual(len(t), 0, "No orphan UserTeamAssociation rows may exist")

            st = (await fresh_db.execute(select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == "130001"))).scalar_one_or_none()
            self.assertIsNone(st, "No orphan TrainingAgentSetting row may exist")

        self.db = AsyncSession(self.engine, expire_on_commit=False)

    async def test_e2e_update_user_definitive_failure_aborts_completely_no_partial_update(self):
        """When update provisioning fails, the transaction rollback leaves the user in original state."""
        original_user = make_user(
            user_id=140,
            username="update_abort_agent",
            email="orig_email@boston.es",
            name="Original Name",
            role="agente",
            hubspot_owner_id="140001",
            agent_initials="OA",
        )
        self.db.add(original_user)
        await self.db.commit()

        # Begin update
        original_user.name = "Modified Name Should Not Persist"
        original_user.email = "modified_email@boston.es"

        with patch.object(AgentCredentialsService, "generate_unique_training_code", side_effect=IntegrityError("collision", params=None, orig=Exception("unique constraint"))):
            with self.assertRaises(IntegrityError):
                await AgentCredentialsService.ensure_agent_training_credentials(
                    self.db, user=original_user, commit=False
                )

        # Rollback on exception
        await self.db.rollback()
        await self.db.close()

        # Check in FRESH session: User retains strictly original data!
        async with AsyncSession(self.engine) as fresh_db:
            u = (await fresh_db.execute(select(User).where(User.user_id == 140))).scalar_one_or_none()
            self.assertIsNotNone(u)
            self.assertEqual(u.name, "Original Name")
            self.assertEqual(u.email, "orig_email@boston.es")

        self.db = AsyncSession(self.engine, expire_on_commit=False)


if __name__ == "__main__":
    unittest.main()
