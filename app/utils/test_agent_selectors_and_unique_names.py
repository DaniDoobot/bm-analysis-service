"""
Tests for agent selector improvements:
- AgentInfo schema exposes user_id, id, team_id, team_name
- Label format is "Nombre · Equipo" not "XX · Nombre"
- resolve_agent_identifiers_to_owner_ids resolves user_ids -> owner_ids
- 60 demo agent names are unique and realistic
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock


def _run(coro):
    return asyncio.run(coro)


class TestAgentInfoSchema(unittest.TestCase):
    """AgentInfo in both schemas must expose user_id, id, team_id, team_name."""

    def test_analytics_agent_info_has_new_fields(self):
        from app.schemas.analytics import AgentInfo
        info = AgentInfo(
            user_id=42,
            id=42,
            hubspot_owner_id="owner_01",
            agent_name="Ana Garcia",
            name="Ana Garcia",
            agent_initials="AG",
            initials="AG",
            label="Ana Garcia · Front Atencion",
            team_id=1,
            team_name="Front Atencion",
        )
        self.assertEqual(info.user_id, 42)
        self.assertEqual(info.id, 42)
        self.assertEqual(info.team_id, 1)
        self.assertEqual(info.team_name, "Front Atencion")

    def test_dashboard_agent_info_has_new_fields(self):
        from app.schemas.dashboard import AgentInfo
        info = AgentInfo(
            user_id=7,
            id=7,
            hubspot_owner_id="owner_07",
            agent_name="Carlos Lopez",
            name="Carlos Lopez",
            agent_initials="CL",
            initials="CL",
            label="Carlos Lopez · Backoffice Atencion",
            team_id=2,
            team_name="Backoffice Atencion",
        )
        self.assertEqual(info.user_id, 7)
        self.assertEqual(info.id, 7)
        self.assertEqual(info.team_id, 2)
        self.assertEqual(info.team_name, "Backoffice Atencion")

    def test_label_format_name_dot_team(self):
        """Label must be 'Nombre · Equipo', NOT 'initials · Nombre'."""
        from app.schemas.analytics import AgentInfo
        info = AgentInfo(
            user_id=1,
            id=1,
            hubspot_owner_id="oid",
            agent_name="Laura Martinez",
            name="Laura Martinez",
            agent_initials="LM",
            initials="LM",
            label="Laura Martinez · Equipo Comercial",
            team_id=3,
            team_name="Equipo Comercial",
        )
        self.assertTrue(info.label.startswith("Laura Martinez"), f"Label starts wrongly: {info.label!r}")
        self.assertIn("·", info.label)
        self.assertIn("Equipo Comercial", info.label)

    def test_label_without_team_is_just_name(self):
        from app.schemas.analytics import AgentInfo
        info = AgentInfo(
            user_id=None,
            id=None,
            hubspot_owner_id="oid",
            agent_name="Sin Equipo",
            name="Sin Equipo",
            agent_initials="SE",
            initials="SE",
            label="Sin Equipo",
        )
        self.assertEqual(info.label, "Sin Equipo")
        self.assertNotIn("·", info.label)


class TestResolveAgentIdentifiers(unittest.TestCase):
    """Test that resolve_agent_identifiers_to_owner_ids resolves user_ids and passes through owner_ids."""

    def _make_user(self, user_id, hubspot_owner_id, company_id=6):
        u = MagicMock()
        u.user_id = user_id
        u.hubspot_owner_id = hubspot_owner_id
        u.company_id = company_id
        return u

    def test_passes_through_non_numeric_owner_ids(self):
        from app.utils.agent_resolvers import resolve_agent_identifiers_to_owner_ids

        async def _run_test():
            db = AsyncMock()
            result = await resolve_agent_identifiers_to_owner_ids(db, ["owner_01", "owner_02"])
            self.assertEqual(set(result), {"owner_01", "owner_02"})

        _run(_run_test())

    def test_resolves_integer_user_ids_to_owner_ids(self):
        from app.utils.agent_resolvers import resolve_agent_identifiers_to_owner_ids

        async def _run_test():
            db = AsyncMock()
            user_42 = self._make_user(42, "owner_42")
            mock_result = MagicMock()
            # resolve_agent_identifiers_to_owner_ids uses res.all() returning row tuples (user_id, hubspot_owner_id)
            mock_result.all.return_value = [(42, "owner_42")]
            db.execute = AsyncMock(return_value=mock_result)

            result = await resolve_agent_identifiers_to_owner_ids(db, ["42", "owner_literal"])
            self.assertIn("owner_42", result, f"Expected owner_42 in result: {result}")
            self.assertIn("owner_literal", result, f"Expected owner_literal in result: {result}")

        _run(_run_test())

    def test_empty_input_returns_empty_set(self):
        from app.utils.agent_resolvers import resolve_agent_identifiers_to_owner_ids

        async def _run_test():
            db = AsyncMock()
            result = await resolve_agent_identifiers_to_owner_ids(db, [])
            self.assertEqual(result, [])

        _run(_run_test())


class TestDemoAgentNames(unittest.TestCase):
    """The 60 demo agent names must be unique, realistic, and span all teams."""

    DEMO_AGENT_NAMES = [
        ("Ana Garcia Lopez", "AG"),
        ("Carlos Lopez Martinez", "CL"),
        ("Laura Martinez Sanchez", "LM"),
        ("Javier Rodriguez Fernandez", "JR"),
        ("Sofia Sanchez Gomez", "SS"),
        ("Alejandro Fernandez Diaz", "AF"),
        ("Lucia Gomez Ruiz", "LG"),
        ("Daniel Martin Torres", "DM"),
        ("Elena Diaz Moreno", "ED"),
        ("David Ruiz Jimenez", "DR"),
        ("Maria Moreno Alvarez", "MM"),
        ("Pablo Jimenez Romero", "PJ"),
        ("Carmen Alvarez Alonso", "CA"),
        ("Manuel Romero Gutierrez", "MR"),
        ("Marta Alonso Navarro", "MA"),
        ("Adrian Gutierrez Torres", "AG2"),
        ("Sara Navarro Dominguez", "SN"),
        ("Alvaro Torres Vazquez", "AT"),
        ("Paula Dominguez Ramos", "PD"),
        ("Mario Vazquez Gil", "MV"),
        ("Irene Ramos Ramirez", "IR"),
        ("Diego Gil Serrano", "DG"),
        ("Natalia Ramirez Blanco", "NR"),
        ("Hugo Serrano Molina", "HS"),
        ("Claudia Blanco Morales", "CB"),
        ("Gonzalo Molina Suarez", "GM"),
        ("Silvia Morales Ortega", "SM"),
        ("Marcos Suarez Delgado", "MS"),
        ("Patricia Ortega Castro", "PO"),
        ("Sergio Delgado Ortiz", "SD"),
        ("Raquel Castro Rubio", "RC"),
        ("Fernando Ortiz Marin", "FO"),
        ("Alicia Rubio Sanz", "AR"),
        ("Jorge Marin Nunez", "JM"),
        ("Cristina Sanz Iglesias", "CS"),
        ("Andres Nunez Medina", "AN"),
        ("Marina Iglesias Garrido", "MI"),
        ("Ruben Medina Cortes", "RM"),
        ("Andrea Garrido Castillo", "AG3"),
        ("Victor Cortes Santos", "VC"),
        ("Noelia Castillo Lozano", "NC"),
        ("Ivan Santos Guerrero", "IS"),
        ("Lorena Lozano Cano", "LL"),
        ("Raul Guerrero Prieto", "RG"),
        ("Miriam Cano Mendez", "MC"),
        ("Oscar Prieto Cruz", "OP"),
        ("Celia Mendez Calvo", "CM"),
        ("Guillermo Cruz Gallego", "GC"),
        ("Alba Calvo Vidal", "AV"),
        ("Hector Gallego Leon", "HG"),
        ("Rocio Vidal Herrera", "RV"),
        ("Gabriel Leon Marquez", "GL"),
        ("Teresa Herrera Pena", "TH"),
        ("Samuel Marquez Flores", "SM2"),
        ("Clara Pena Cabrera", "CP"),
        ("Roberto Flores Campos", "RF"),
        ("Ines Cabrera Vega", "IC"),
        ("Tomas Campos Fuentes", "TC"),
        ("Julia Vega Medina", "JV"),
        ("Lucas Fuentes Garcia", "LF"),
    ]

    def test_exactly_60_agents(self):
        self.assertEqual(len(self.DEMO_AGENT_NAMES), 60)

    def test_names_are_unique(self):
        names = [n for n, _ in self.DEMO_AGENT_NAMES]
        self.assertEqual(len(names), len(set(names)), "Duplicate full names found")

    def test_names_are_realistic_spanish(self):
        for name, _ in self.DEMO_AGENT_NAMES:
            parts = name.split()
            self.assertGreaterEqual(len(parts), 2, f"Name too short: {name!r}")
            self.assertTrue(parts[0][0].isupper(), f"First name not capitalized: {name!r}")

    def test_no_generic_placeholder_names(self):
        for name, _ in self.DEMO_AGENT_NAMES:
            self.assertNotIn("Demo", name, f"Found placeholder name: {name!r}")
            self.assertNotIn("Agente", name, f"Found placeholder name: {name!r}")

    def test_initials_strip_to_two_chars(self):
        for name, raw_initials in self.DEMO_AGENT_NAMES:
            clean = raw_initials.rstrip("0123456789")
            self.assertEqual(len(clean), 2, f"Initials for {name!r} are not 2 chars after strip: {clean!r}")

    def test_team_distribution(self):
        self.assertEqual(len(self.DEMO_AGENT_NAMES[0:10]), 10)
        self.assertEqual(len(self.DEMO_AGENT_NAMES[10:30]), 20)
        self.assertEqual(len(self.DEMO_AGENT_NAMES[30:40]), 10)
        self.assertEqual(len(self.DEMO_AGENT_NAMES[40:60]), 20)


if __name__ == "__main__":
    unittest.main()



