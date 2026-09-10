"""
Test suite for service and team agent selection in Speech BM mass evaluations and automations.
Validates:
- Dynamic resolution of hubspot_owner_ids by service (Front vs EXPAC).
- Strict disjointness between Front and EXPAC owners.
- Explicit agent_owner_ids override vs dynamic resolution.
- Fail-closed behavior when a service has no valid agents.
- Preservation of legacy manual fallbacks.
"""
import asyncio
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.mass_evaluations import (
    MassAnalysisAutomation,
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from app.models.prompts import Prompt, PromptVersion
from app.models.services import Service
from app.models.teams import Team, AgentTeamAssociation, UserTeamAssociation, UserServiceAssociation
from app.models.users import User
from app.services.hubspot_service import HubSpotService
from app.services.mass_evaluation_service import MassEvaluationService


class TestServiceTeamSelection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        async with self.async_session() as session:
            # Create Services: 1=Front, 2=EXPAC, 3=EmptyService
            s1 = Service(service_id=1, company_id=1, service_key="front", service_name="Front", is_active=True)
            s2 = Service(service_id=2, company_id=1, service_key="expac", service_name="EXPAC", is_active=True)
            s3 = Service(service_id=3, company_id=1, service_key="empty", service_name="Empty Service", is_active=True)
            session.add_all([s1, s2, s3])

            # Create Teams
            t1 = Team(team_id=1, company_id=1, service_id=1, team_name="Equipo Front", is_active=True)
            t2 = Team(team_id=2, company_id=1, service_id=2, team_name="Equipo EXPAC", is_active=True)
            session.add_all([t1, t2])

            # Front Agents (Team 1, Service 1)
            u1 = User(user_id=101, company_id=1, username="front1", email="front1@test.com", password_hash="x", name="Front Agent 1", hubspot_owner_id="33013277", is_active=True, primary_team_id=1, primary_service_id=1)
            u2 = User(user_id=102, company_id=1, username="front2", email="front2@test.com", password_hash="x", name="Front Agent 2", hubspot_owner_id="33013276", is_active=True, primary_service_id=1)
            u3 = User(user_id=103, company_id=1, username="front3", email="front3@test.com", password_hash="x", name="Front Agent 3", hubspot_owner_id="1375831790", is_active=True)
            # Association via AgentTeamAssociation
            assoc_u3 = AgentTeamAssociation(user_id=103, team_id=1)

            # EXPAC Agents (Team 2, Service 2)
            u4 = User(user_id=201, company_id=1, username="expac1", email="expac1@test.com", password_hash="x", name="EXPAC Agent 1", hubspot_owner_id="76997586", is_active=True, primary_team_id=2, primary_service_id=2)
            u5 = User(user_id=202, company_id=1, username="expac2", email="expac2@test.com", password_hash="x", name="EXPAC Agent 2", hubspot_owner_id="1626092736", is_active=True, primary_service_id=2)
            u6 = User(user_id=203, company_id=1, username="expac3", email="expac3@test.com", password_hash="x", name="EXPAC Agent 3", hubspot_owner_id="1841837788", is_active=True)
            assoc_u6 = UserTeamAssociation(user_id=203, team_id=2)

            # Inactive agent - should NOT be included
            u_inactive = User(user_id=301, company_id=1, username="inactive", email="inactive@test.com", password_hash="x", name="Inactive Agent", hubspot_owner_id="99999999", is_active=False, primary_service_id=1)

            # Agent with empty hubspot_owner_id - should NOT be included
            u_no_hs = User(user_id=302, company_id=1, username="nohs", email="nohs@test.com", password_hash="x", name="No HS Agent", hubspot_owner_id="", is_active=True, primary_service_id=1)

            session.add_all([u1, u2, u3, assoc_u3, u4, u5, u6, assoc_u6, u_inactive, u_no_hs])

            # Prompts
            p1 = Prompt(prompt_id=58, company_id=1, service_id=1, prompt_name="Front Prompt", prompt_type="evaluation", is_active=True)
            pv1 = PromptVersion(id=580, prompt_id=58, version_label="v1", prompt="Test front prompt", is_current=True)
            p2 = Prompt(prompt_id=59, company_id=1, service_id=2, prompt_name="EXPAC Prompt", prompt_type="evaluation", is_active=True)
            pv2 = PromptVersion(id=590, prompt_id=59, version_label="v1", prompt="Test expac prompt", is_current=True)
            session.add_all([p1, pv1, p2, pv2])

            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    # ── Test A, B, C: Dynamic Resolution and Disjointness ────────────────────────

    async def test_case_a_front_owners_resolution(self):
        """Service 1 (Front) resolves only active Front agents with hubspot_owner_id."""
        async with self.async_session() as session:
            owners = await MassEvaluationService.get_active_owner_ids_for_service(session, service_id=1)
            self.assertEqual(sorted(owners), ["1375831790", "33013276", "33013277"])
            self.assertNotIn("99999999", owners)
            self.assertNotIn("", owners)

    async def test_case_b_expac_owners_resolution(self):
        """Service 2 (EXPAC) resolves only active EXPAC agents with hubspot_owner_id."""
        async with self.async_session() as session:
            owners = await MassEvaluationService.get_active_owner_ids_for_service(session, service_id=2)
            self.assertEqual(sorted(owners), ["1626092736", "1841837788", "76997586"])

    async def test_case_c_front_and_expac_disjoint(self):
        """Front and EXPAC owner sets must be strictly disjoint."""
        async with self.async_session() as session:
            front_owners = set(await MassEvaluationService.get_active_owner_ids_for_service(session, service_id=1))
            expac_owners = set(await MassEvaluationService.get_active_owner_ids_for_service(session, service_id=2))
            intersection = front_owners.intersection(expac_owners)
            self.assertEqual(len(intersection), 0, f"Expected empty intersection, got: {intersection}")

    # ── Test D: Explicit agent_owner_ids Override ───────────────────────────────

    async def test_case_d_explicit_agent_owner_ids_priority(self):
        """Explicit non-empty agent_owner_ids takes absolute priority over service resolution."""
        async with self.async_session() as session:
            explicit_ids = ["999001", "999002"]
            job = MassEvaluationJob(
                job_id=101,
                company_id=1,
                service_id=1,
                prompt_id=58,
                job_name="Job with explicit owners",
                agent_owner_ids=explicit_ids,
                execution_source="manual",
                is_active=True,
                created_at=datetime.now(timezone.utc)
            )
            session.add(job)
            await session.commit()

            with patch("app.services.hubspot_service.HubSpotService.search_calls_for_mass_evaluation", new_callable=AsyncMock) as mock_search:
                mock_search.return_value = []
                preview = await MassEvaluationService.search_calls_for_job_preview(job_id=101, db=session)
                self.assertEqual(mock_search.call_count, 1)
                called_filters = mock_search.call_args[0][0]
                self.assertEqual(called_filters.get("agent_owner_ids"), explicit_ids)

    # ── Test E: Fail-Closed When Service Has No Valid Agents ────────────────────

    async def test_case_e_fail_closed_empty_service(self):
        """When a service has no valid agents, run fails closed (0 calls, no HubSpot call, status completed)."""
        async with self.async_session() as session:
            job = MassEvaluationJob(
                job_id=102,
                company_id=1,
                service_id=3,
                prompt_id=58,
                job_name="Job on empty service",
                agent_owner_ids=[],
                execution_source="automation",
                is_active=True,
                created_at=datetime.now(timezone.utc)
            )
            session.add(job)
            await session.commit()

            run = MassEvaluationRun(
                run_id=2001,
                job_id=102,
                company_id=1,
                service_id=3,
                trigger_type="automation",
                execution_source="automation",
                status="running",
                started_at=datetime.now(timezone.utc),
                effective_filters={"service_id": 3, "agent_owner_ids": []},
                created_at=datetime.now(timezone.utc)
            )
            session.add(run)
            await session.commit()

        with patch("app.db.get_engine", return_value=self.engine),              patch("app.services.hubspot_service.HubSpotService.search_calls_for_mass_evaluation", new_callable=AsyncMock) as mock_search:

            await MassEvaluationService._execute_background_run(
                job_id=102,
                run_id=2001,
                filters_payload={"service_id": 3, "agent_owner_ids": []}
            )

            mock_search.assert_not_called()

        async with self.async_session() as verify_session:
            fresh_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == 2001)
            fresh_res = await verify_session.execute(fresh_stmt)
            fresh_run = fresh_res.scalar_one()
            self.assertEqual(fresh_run.status, "completed")
            self.assertEqual(fresh_run.calls_found, 0)
            self.assertEqual(fresh_run.calls_selected, 0)
            self.assertEqual(fresh_run.calls_analyzed, 0)

    # ── Test F: EXPAC Automation Never Searches Front Agents ───────────────────

    async def test_case_f_expac_never_searches_front_agents(self):
        """EXPAC automation (service_id=2) passes only EXPAC owners to HubSpot."""
        async with self.async_session() as session:
            job = MassEvaluationJob(
                job_id=103,
                company_id=1,
                service_id=2,
                prompt_id=59,
                job_name="EXPAC Automation Job",
                agent_owner_ids=[],
                execution_source="automation",
                is_active=True,
                created_at=datetime.now(timezone.utc)
            )
            auto = MassAnalysisAutomation(
                automation_id=99,
                service_id=2,
                job_id=103,
                name="EXPAC Automation",
                prompt_id=59,
                is_active=True
            )
            run = MassEvaluationRun(
                run_id=2002,
                job_id=103,
                company_id=1,
                service_id=2,
                trigger_type="automation",
                execution_source="automation",
                status="running",
                started_at=datetime.now(timezone.utc),
                effective_filters={"service_id": 2, "agent_owner_ids": []},
                created_at=datetime.now(timezone.utc)
            )
            session.add_all([job, auto, run])
            await session.commit()

            with patch("app.db.get_engine", return_value=self.engine),                  patch("app.services.hubspot_service.HubSpotService.search_calls_for_mass_evaluation", new_callable=AsyncMock) as mock_search:

                mock_search.return_value = []
                await MassEvaluationService._execute_background_run(
                    job_id=103,
                    run_id=2002,
                    filters_payload={"service_id": 2, "agent_owner_ids": []}
                )

                self.assertEqual(mock_search.call_count, 1)
                search_args = mock_search.call_args[0][0]
                passed_owners = search_args.get("agent_owner_ids")

                self.assertEqual(sorted(passed_owners), ["1626092736", "1841837788", "76997586"])
                self.assertNotIn("33013277", passed_owners)
                self.assertNotIn("33013276", passed_owners)
                self.assertNotIn("1375831790", passed_owners)

    # ── Test G: Direct HubSpotService Fail-Closed & Legacy Fallback ────────────

    async def test_case_g_hubspot_service_direct_filtering(self):
        """Direct tests of HubSpotService.search_calls_for_mass_evaluation filter rules."""
        hs = HubSpotService()

        # 1. agent_owner_ids = [] -> returns [] without HTTP request
        res_empty = await hs.search_calls_for_mass_evaluation({"agent_owner_ids": []})
        self.assertEqual(res_empty, [])

        # 2. agent_owner_ids = None & execution_source='automation' -> fail-closed (returns [])
        res_auto = await hs.search_calls_for_mass_evaluation({"agent_owner_ids": None, "execution_source": "automation"})
        self.assertEqual(res_auto, [])

        # 3. agent_owner_ids = None & service_id=2 -> fail-closed (returns [])
        res_svc = await hs.search_calls_for_mass_evaluation({"agent_owner_ids": None, "service_id": 2})
        self.assertEqual(res_svc, [])

        # 4. agent_owner_ids = None & no service & execution_source='on_demand' & allow_legacy_fallback=True
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"results": []}
            mock_resp.raise_for_status.return_value = None
            mock_post.return_value = mock_resp

            res_legacy = await hs.search_calls_for_mass_evaluation({
                "agent_owner_ids": None,
                "allow_legacy_fallback": True,
                "execution_source": "on_demand"
            })
            self.assertEqual(res_legacy, [])
            self.assertEqual(mock_post.call_count, 1)
            payload = mock_post.call_args[1]["json"]
            filter_groups = payload["filterGroups"][0]["filters"]
            owner_filter = next(f for f in filter_groups if f["propertyName"] == "hubspot_owner_id")
            self.assertEqual(owner_filter["operator"], "IN")
            from app.utils.hubspot_owners import OWNER_TO_NAME
            self.assertEqual(owner_filter["values"], [str(x) for x in OWNER_TO_NAME.keys()])

    # ── Test H: Role Filtering and Non-Agent Exclusion ────────────────────────

    async def test_case_h_role_filtering_and_exclusion(self):
        """
        Validates:
        1. role='agente' + valid owner + correct service -> INCLUDED
        2. role='agent' + valid owner + correct service -> INCLUDED
        3. role='admin' + valid owner + correct service -> EXCLUDED
        4. role='manager' + valid owner + correct service -> EXCLUDED
        5. role='supervisor' + valid owner + correct service -> EXCLUDED
        6. inactive agent -> EXCLUDED
        7. agent without hubspot_owner_id -> EXCLUDED
        """
        async with self.async_session() as session:
            # Create a dedicated Service 4
            s4 = Service(service_id=4, company_id=1, service_key="role_test", service_name="Role Test Service", is_active=True)
            session.add(s4)
            await session.flush()

            # 1. role="agente" + valid owner + correct service -> INCLUDED
            u_agente = User(
                user_id=401, company_id=1, username="role_agente", email="role_agente@test.com",
                password_hash="x", name="Agente Role", role="agente", hubspot_owner_id="8001",
                is_active=True, primary_service_id=4
            )
            # 2. role="agent" + valid owner + correct service -> INCLUDED
            u_agent = User(
                user_id=402, company_id=1, username="role_agent", email="role_agent@test.com",
                password_hash="x", name="Agent Role", role="agent", hubspot_owner_id="8002",
                is_active=True, primary_service_id=4
            )
            # Case insensitive / trim check: role=" AGENTE " -> INCLUDED
            u_agent_trimmed = User(
                user_id=403, company_id=1, username="role_agent_trimmed", email="role_trimmed@test.com",
                password_hash="x", name="Agent Trimmed", role=" AGENTE ", hubspot_owner_id="8003",
                is_active=True, primary_service_id=4
            )
            # 3. role="admin" + valid owner + correct service -> EXCLUDED
            u_admin = User(
                user_id=404, company_id=1, username="role_admin", email="role_admin@test.com",
                password_hash="x", name="Admin User", role="admin", hubspot_owner_id="8004",
                is_active=True, primary_service_id=4
            )
            # 4. role="manager" + valid owner + correct service -> EXCLUDED
            u_manager = User(
                user_id=405, company_id=1, username="role_manager", email="role_manager@test.com",
                password_hash="x", name="Manager User", role="manager", hubspot_owner_id="8005",
                is_active=True, primary_service_id=4
            )
            # 5. role="supervisor" + valid owner + correct service -> EXCLUDED
            u_supervisor = User(
                user_id=406, company_id=1, username="role_supervisor", email="role_supervisor@test.com",
                password_hash="x", name="Supervisor User", role="supervisor", hubspot_owner_id="8006",
                is_active=True, primary_service_id=4
            )
            # 6. inactive agent -> EXCLUDED
            u_inactive = User(
                user_id=407, company_id=1, username="role_inactive", email="role_inactive@test.com",
                password_hash="x", name="Inactive Agente", role="agente", hubspot_owner_id="8007",
                is_active=False, primary_service_id=4
            )
            # 7. agent without hubspot_owner_id (None) -> EXCLUDED
            u_none_owner = User(
                user_id=408, company_id=1, username="role_none_owner", email="role_none@test.com",
                password_hash="x", name="No Owner Agente", role="agente", hubspot_owner_id=None,
                is_active=True, primary_service_id=4
            )

            session.add_all([
                u_agente, u_agent, u_agent_trimmed,
                u_admin, u_manager, u_supervisor,
                u_inactive, u_none_owner
            ])
            await session.commit()

            owners = await MassEvaluationService.get_active_owner_ids_for_service(session, service_id=4)

            # Assert included
            self.assertIn("8001", owners, "role='agente' must be included")
            self.assertIn("8002", owners, "role='agent' must be included")
            self.assertIn("8003", owners, "role=' AGENTE ' (case-insensitive/trimmed) must be included")

            # Assert excluded
            self.assertNotIn("8004", owners, "role='admin' must be excluded")
            self.assertNotIn("8005", owners, "role='manager' must be excluded")
            self.assertNotIn("8006", owners, "role='supervisor' must be excluded")
            self.assertNotIn("8007", owners, "inactive agent must be excluded")
            self.assertNotIn(None, owners, "None owner must be excluded")

            self.assertEqual(sorted(owners), ["8001", "8002", "8003"])


if __name__ == "__main__":
    unittest.main()

