"""
test_agent_selectors_and_unique_names.py
=========================================
Unit tests for agent selector improvements & unique agent_code generation:
1. AgentInfo schema exposes user_id, id, agent_code, team_id, team_name.
2. 60 demo agents generate exact, unique agent_codes (AC-F01..10, AC-B01..20, VT-C01..10, VT-R01..20).
3. No duplicate codes across all 60 demo agents.
4. Real companies (Boston Medical) preserve name and fallback correctly without demo codes.
5. Formatted label: "AC-F01 · Agente Demo 01" for demo; "Nombre · Equipo" for real companies.
6. resolve_agent_identifiers_to_owner_ids resolves user_ids -> owner_ids.
7. Personalized training schemas expose agent_code, name, team_name.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.utils.agent_resolvers import (
    calculate_demo_agent_code,
    get_demo_agent_index,
    resolve_agent_code,
    resolve_agent_initials,
    resolve_demo_agent_code,
    resolve_agent_identifiers_to_owner_ids,
)


def _run(coro):
    return asyncio.run(coro)


class TestAgentInfoSchema(unittest.TestCase):
    """AgentInfo in both analytics and dashboard schemas must expose user_id, id, agent_code, team_id, team_name."""

    def test_analytics_agent_info_has_new_fields(self):
        from app.schemas.analytics import AgentInfo
        info = AgentInfo(
            user_id=42,
            id=42,
            hubspot_owner_id="demo_owner_01",
            agent_name="Agente Demo 01",
            name="Agente Demo 01",
            agent_code="AC-F01",
            agent_initials="AC-F01",
            initials="AC-F01",
            label="AC-F01 · Agente Demo 01",
            team_id=1,
            team_name="Front Atención",
        )
        self.assertEqual(info.user_id, 42)
        self.assertEqual(info.id, 42)
        self.assertEqual(info.agent_code, "AC-F01")
        self.assertEqual(info.team_id, 1)
        self.assertEqual(info.team_name, "Front Atención")
        self.assertEqual(info.label, "AC-F01 · Agente Demo 01")

    def test_dashboard_agent_info_has_new_fields(self):
        from app.schemas.dashboard import AgentInfo
        info = AgentInfo(
            user_id=11,
            id=11,
            hubspot_owner_id="demo_owner_11",
            agent_name="Agente Demo 11",
            name="Agente Demo 11",
            agent_code="AC-B01",
            agent_initials="AC-B01",
            initials="AC-B01",
            label="AC-B01 · Agente Demo 11",
            team_id=2,
            team_name="Backoffice Atención",
        )
        self.assertEqual(info.user_id, 11)
        self.assertEqual(info.id, 11)
        self.assertEqual(info.agent_code, "AC-B01")
        self.assertEqual(info.team_id, 2)
        self.assertEqual(info.team_name, "Backoffice Atención")


class TestAgentCodeResolution(unittest.TestCase):
    """Test the generation and resolution of unique visible agent_codes."""

    def test_60_demo_agents_have_exact_expected_codes(self):
        """Verify all 60 demo agents map to their exact designated code."""
        # Front Atención (01..10) -> AC-F01..AC-F10
        for i in range(1, 11):
            expected = f"AC-F{i:02d}"
            code_by_idx = calculate_demo_agent_code(i)
            code_by_oid = resolve_demo_agent_code(hubspot_owner_id=f"demo_owner_{i:02d}")
            code_by_name = resolve_demo_agent_code(agent_name=f"Agente Demo {i:02d}")
            self.assertEqual(code_by_idx, expected)
            self.assertEqual(code_by_oid, expected)
            self.assertEqual(code_by_name, expected)

        # Backoffice Atención (11..30) -> AC-B01..AC-B20
        for i in range(11, 31):
            expected = f"AC-B{(i - 10):02d}"
            code_by_idx = calculate_demo_agent_code(i)
            code_by_oid = resolve_demo_agent_code(hubspot_owner_id=f"demo_owner_{i:02d}")
            code_by_name = resolve_demo_agent_code(agent_name=f"Agente Demo {i:02d}")
            self.assertEqual(code_by_idx, expected)
            self.assertEqual(code_by_oid, expected)
            self.assertEqual(code_by_name, expected)

        # Equipo Comercial (31..40) -> VT-C01..VT-C10
        for i in range(31, 41):
            expected = f"VT-C{(i - 30):02d}"
            code_by_idx = calculate_demo_agent_code(i)
            code_by_oid = resolve_demo_agent_code(hubspot_owner_id=f"demo_owner_{i:02d}")
            code_by_name = resolve_demo_agent_code(agent_name=f"Agente Demo {i:02d}")
            self.assertEqual(code_by_idx, expected)
            self.assertEqual(code_by_oid, expected)
            self.assertEqual(code_by_name, expected)

        # Equipo Retención (41..60) -> VT-R01..VT-R20
        for i in range(41, 61):
            expected = f"VT-R{(i - 40):02d}"
            code_by_idx = calculate_demo_agent_code(i)
            code_by_oid = resolve_demo_agent_code(hubspot_owner_id=f"demo_owner_{i:02d}")
            code_by_name = resolve_demo_agent_code(agent_name=f"Agente Demo {i:02d}")
            self.assertEqual(code_by_idx, expected)
            self.assertEqual(code_by_oid, expected)
            self.assertEqual(code_by_name, expected)

    def test_all_60_codes_are_unique(self):
        """Ensure no duplicate codes exist among the 60 demo agents."""
        codes = [calculate_demo_agent_code(i) for i in range(1, 61)]
        self.assertEqual(len(codes), 60)
        self.assertEqual(len(set(codes)), 60, f"Duplicates found in demo codes: {codes}")

    def test_no_collision_between_agents_10_and_11(self):
        """Agente 10 must be AC-F10 and Agente 11 must be AC-B01 (never both A1)."""
        code_10 = resolve_demo_agent_code(hubspot_owner_id="demo_owner_10", agent_name="Agente Demo 10")
        code_11 = resolve_demo_agent_code(hubspot_owner_id="demo_owner_11", agent_name="Agente Demo 11")
        self.assertEqual(code_10, "AC-F10")
        self.assertEqual(code_11, "AC-B01")
        self.assertNotEqual(code_10, code_11)

    def test_real_company_fallback_preserves_name(self):
        """Real companies (e.g. Boston Medical company_id=1) must fallback to name, not demo code."""
        real_code = resolve_agent_code(
            hubspot_owner_id="33013276",
            agent_name="Cristina Montenegro",
            company_id=1,
            is_demo=False,
        )
        self.assertEqual(real_code, "Cristina Montenegro")
        self.assertFalse(real_code.startswith("AC-") or real_code.startswith("VT-"))

    def test_demo_agent_initials_return_code(self):
        """For demo agents, resolve_agent_initials returns the structured code (e.g. AC-F01)."""
        init = resolve_agent_initials(
            hubspot_owner_id="demo_owner_01",
            agent_name="Agente Demo 01",
            company_id=6,
        )
        self.assertEqual(init, "AC-F01")


class TestResolveAgentIdentifiers(unittest.TestCase):
    """Test that resolve_agent_identifiers_to_owner_ids resolves user_ids and passes through owner_ids."""

    def _make_user(self, user_id, hubspot_owner_id, company_id=6):
        u = MagicMock()
        u.user_id = user_id
        u.hubspot_owner_id = hubspot_owner_id
        u.company_id = company_id
        return u

    def test_passes_through_non_numeric_owner_ids(self):
        async def _run_test():
            db = AsyncMock()
            result = await resolve_agent_identifiers_to_owner_ids(db, ["demo_owner_01", "demo_owner_02"])
            self.assertEqual(set(result), {"demo_owner_01", "demo_owner_02"})

        _run(_run_test())

    def test_resolves_integer_user_ids_to_owner_ids(self):
        async def _run_test():
            db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = [(42, "demo_owner_42")]
            db.execute = AsyncMock(return_value=mock_result)

            result = await resolve_agent_identifiers_to_owner_ids(db, ["42", "owner_literal"])
            self.assertIn("demo_owner_42", result)
            self.assertIn("owner_literal", result)

        _run(_run_test())

    def test_empty_input_returns_empty_list(self):
        async def _run_test():
            db = AsyncMock()
            result = await resolve_agent_identifiers_to_owner_ids(db, [])
            self.assertEqual(result, [])

        _run(_run_test())


class TestPersonalizedTrainingSchemas(unittest.TestCase):
    """Training schemas must expose agent_code, name, team_name."""

    def test_training_agent_setting_out_has_new_fields(self):
        from datetime import datetime, timezone
        from app.schemas.personalized_training import TrainingAgentSettingOut
        now = datetime.now(timezone.utc)
        setting = TrainingAgentSettingOut(
            setting_id=1,
            hubspot_owner_id="demo_owner_01",
            agent_name="Agente Demo 01",
            name="Agente Demo 01",
            agent_code="AC-F01",
            agent_initials="AC-F01",
            team_name="Front Atención",
            company_id=6,
            is_enabled=True,
            training_code_updated_at=now,
            created_at=now,
            updated_at=now,
        )
        self.assertEqual(setting.agent_code, "AC-F01")
        self.assertEqual(setting.name, "Agente Demo 01")
        self.assertEqual(setting.team_name, "Front Atención")

    def test_agent_overview_item_has_new_fields(self):
        from app.schemas.personalized_training import AgentOverviewItem
        item = AgentOverviewItem(
            hubspot_owner_id="demo_owner_11",
            agent_name="Agente Demo 11",
            name="Agente Demo 11",
            agent_code="AC-B01",
            agent_initials="AC-B01",
            team_name="Backoffice Atención",
            is_enabled=False,
        )
        self.assertEqual(item.agent_code, "AC-B01")
        self.assertEqual(item.name, "Agente Demo 11")
        self.assertEqual(item.team_name, "Backoffice Atención")


if __name__ == "__main__":
    unittest.main()
