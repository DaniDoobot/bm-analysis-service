"""
Unit and integration tests for HTTP query parameter parsing and precedence
of avg_score_min / score_min / eval_min and avg_score_max / score_max / eval_max.
"""
import os

# Satisfy app/db.py test isolation guard
if "DATABASE_URL" not in os.environ or "_test" not in os.environ.get("DATABASE_URL", ""):
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agent_evolution_test.db"

import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.core.tenant_context import TenantContext
from app.dependencies import get_db, get_current_user, get_tenant_context
from app.models.users import User
from app.core.roles import InternalRole

DUMMY_AGENT_EVOLUTION = {
    "agent": {"hubspot_owner_id": "123", "agent_name": "Test Agent"},
    "period": "30d",
    "source": "mass_evaluations",
    "generated_at": "2026-09-03T10:00:00Z",
    "summary": {"total_analyses": 0, "cita_rate": 0, "total_objeciones": 0},
    "trend": {
        "evaluacion_global_slope": 0,
        "evaluacion_global_direction": "stable",
        "evaluacion_global_delta_first_last": 0,
        "evaluacion_global_delta_pct": 0,
        "interpretation": "stable",
    },
    "timeline": [],
    "criteria_evolution": [],
    "strengths": [],
    "weaknesses": [],
    "latest_analyses": [],
}

DUMMY_AGENTS_LIST = []


class TestAgentScoreFilterRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock_user = User(
            user_id=1,
            email="test@bm.es",
            name="Test SuperAdmin",
            role="super_admin",
            company_id=1,
            hubspot_owner_id="123",
            is_active=True,
        )
        cls.mock_context = TenantContext(
            company_id=1,
            allowed_service_ids=None,
            allowed_agent_ids=None,
            is_super_admin=True,
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            user_id=1,
        )

        app.dependency_overrides[get_tenant_context] = lambda: cls.mock_context
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_current_user] = lambda: cls.mock_user
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_tenant_context, None)
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    # ──────────────────────────────────────────────────────────────────────────
    # Tests for GET /bm/agents/{hubspot_owner_id}/evolution
    # ──────────────────────────────────────────────────────────────────────────

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_avg_score_min(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?avg_score_min=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_score_min(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?score_min=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_eval_min(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?eval_min=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_avg_score_max(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?avg_score_max=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_max"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_score_max(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?score_max=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_max"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_eval_max(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?eval_max=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_max"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_avg_score_min_zero_preserved(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?avg_score_min=0")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 0.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_avg_score_max_zero_preserved(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?avg_score_max=0")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_max"], 0.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_precedence_min_avg_beats_score(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?avg_score_min=8&score_min=7&eval_min=6")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_precedence_min_score_beats_eval(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?score_min=7&eval_min=6")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 7.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_precedence_max_avg_beats_score(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?avg_score_max=8&score_max=9&eval_max=10")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_max"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_precedence_max_score_beats_eval(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution?score_max=9&eval_max=10")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_max"], 9.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_agent_evolution_no_params_none(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/agents/123/evolution")
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(mock_evo.call_args.kwargs["avg_score_min"])
        self.assertIsNone(mock_evo.call_args.kwargs["avg_score_max"])

    # ──────────────────────────────────────────────────────────────────────────
    # Tests for GET /bm/me/evolution
    # ──────────────────────────────────────────────────────────────────────────

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_my_evolution_avg_score_min(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/me/evolution?avg_score_min=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_my_evolution_score_min(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/me/evolution?score_min=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_my_evolution_eval_min(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/me/evolution?eval_min=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agent_evolution", new_callable=AsyncMock)
    def test_my_evolution_zero_preserved(self, mock_evo):
        mock_evo.return_value = DUMMY_AGENT_EVOLUTION
        res = self.client.get("/bm/me/evolution?avg_score_min=0&avg_score_max=0")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_min"], 0.0)
        self.assertEqual(mock_evo.call_args.kwargs["avg_score_max"], 0.0)

    # ──────────────────────────────────────────────────────────────────────────
    # Tests for GET /bm/agents (list_agents)
    # ──────────────────────────────────────────────────────────────────────────

    @patch("app.routers.dashboard.get_agents_list", new_callable=AsyncMock)
    def test_list_agents_avg_score_min(self, mock_list):
        mock_list.return_value = DUMMY_AGENTS_LIST
        res = self.client.get("/bm/agents?avg_score_min=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_list.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agents_list", new_callable=AsyncMock)
    def test_list_agents_score_min(self, mock_list):
        mock_list.return_value = DUMMY_AGENTS_LIST
        res = self.client.get("/bm/agents?score_min=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_list.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agents_list", new_callable=AsyncMock)
    def test_list_agents_eval_min(self, mock_list):
        mock_list.return_value = DUMMY_AGENTS_LIST
        res = self.client.get("/bm/agents?eval_min=8")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_list.call_args.kwargs["avg_score_min"], 8.0)

    @patch("app.routers.dashboard.get_agents_list", new_callable=AsyncMock)
    def test_list_agents_zero_preserved(self, mock_list):
        mock_list.return_value = DUMMY_AGENTS_LIST
        res = self.client.get("/bm/agents?avg_score_min=0&avg_score_max=0")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_list.call_args.kwargs["avg_score_min"], 0.0)
        self.assertEqual(mock_list.call_args.kwargs["avg_score_max"], 0.0)


if __name__ == "__main__":
    unittest.main()
