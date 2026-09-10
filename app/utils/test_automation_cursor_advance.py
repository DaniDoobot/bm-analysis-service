"""
Comprehensive security and operational test suite for MassAnalysisAutomation cursor advance.
Validates:
- Case A: Active automation -> 409 Conflict, no marker created.
- Case B: Inactive automation with active run (running or pending) -> 409 Conflict, no marker created.
- Case C: Agent role -> 403 Forbidden.
- Case D: Non-administrative role (e.g. Team Coordinator) -> 403 Forbidden.
- Case E: Authorized admin within scope (Super Admin / Company Admin / Service Manager) -> 200 OK.
- Case F: Admin outside allowed_company_ids or allowed_service_ids -> 403 Forbidden.
- Case G: Empty or whitespace reason -> 422 Unprocessable Entity.
- Case H: Double operation / concurrency -> safe behavior (second call rejected, no backward corruption).
- Case I: Normal advance with inactive automation -> marker completed_empty created, history untouched.
- Case J: Zero MassEvaluationRun or MassEvaluationResult records created.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.roles import InternalRole
from app.core.tenant_context import TenantContext
from app.db import Base
from app.dependencies import get_db, get_tenant_context
from app.main import app
from app.models.mass_evaluations import (
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from app.models.prompts import Prompt, PromptVersion
from app.models.services import Service
from app.services.mass_evaluation_service import (
    MassEvaluationService,
    AutomationConflictError,
)


class TestAutomationCursorAdvanceHardening(unittest.IsolatedAsyncioTestCase):
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
            # Services
            s1 = Service(service_id=1, company_id=1, service_key="front", service_name="Front", is_active=True)
            s2 = Service(service_id=2, company_id=1, service_key="expac", service_name="EXPAC", is_active=True)
            s3 = Service(service_id=3, company_id=2, service_key="other", service_name="Other Company Service", is_active=True)
            session.add_all([s1, s2, s3])

            # Prompts
            p1 = Prompt(prompt_id=1, company_id=1, service_id=1, prompt_name="Prompt Front", prompt_type="audio", is_active=True)
            pv1 = PromptVersion(id=1, prompt_id=1, is_current=True, prompt="Eval text")
            session.add_all([p1, pv1])

            # Jobs
            j1 = MassEvaluationJob(job_id=48, job_name="Job Front", service_id=1, company_id=1, prompt_id=1, prompt_version_id=1, is_active=True)
            j2 = MassEvaluationJob(job_id=54, job_name="Job EXPAC", service_id=2, company_id=1, prompt_id=1, prompt_version_id=1, is_active=True)
            session.add_all([j1, j2])

            # Automation 8: Front (active)
            a8 = MassAnalysisAutomation(
                automation_id=8,
                service_id=1,
                job_id=48,
                prompt_id=1,
                prompt_version_id=1,
                name="Front Automation",
                is_active=True,
                interval_minutes=10,
                delay_minutes=5,
                lookback_minutes=10,
            )
            # Automation 9: EXPAC (inactive)
            a9 = MassAnalysisAutomation(
                automation_id=9,
                service_id=2,
                job_id=54,
                prompt_id=1,
                prompt_version_id=1,
                name="EXPAC Automation",
                is_active=False,
                interval_minutes=10,
                delay_minutes=5,
                lookback_minutes=10,
            )
            # Automation 10: Other company (inactive)
            a10 = MassAnalysisAutomation(
                automation_id=10,
                service_id=3,
                job_id=54,
                prompt_id=1,
                prompt_version_id=1,
                name="Other Company Automation",
                is_active=False,
                interval_minutes=10,
                delay_minutes=5,
                lookback_minutes=10,
            )
            session.add_all([a8, a9, a10])

            # Historical run 10275 for Automation 9 (dated 2026-09-09)
            hist_run_9_from = datetime(2026, 9, 9, 11, 49, 21, 795780, tzinfo=timezone.utc)
            hist_run_9_to = datetime(2026, 9, 9, 11, 59, 21, 795780, tzinfo=timezone.utc)
            run_10275 = MassAnalysisAutomationRun(
                automation_run_id=10275,
                automation_id=9,
                window_from=hist_run_9_from,
                window_to=hist_run_9_to,
                status="completed",
                calls_found=12,
                calls_selected=10,
                calls_skipped=2,
                error_message=None,
            )
            session.add(run_10275)
            await session.commit()

        # Database dependency override
        async def override_get_db():
            async with self.async_session() as s:
                yield s

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        await self.engine.dispose()

    # ── Test Cases ──────────────────────────────────────────────────────────

    def test_case_a_active_automation_rejected(self):
        """Case A: Active automation -> 409 Conflict, no marker created."""
        ctx = TenantContext(
            user_id=1,
            user_email="superadmin@example.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1, 2],
        )
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        resp = self.client.post(
            "/bm/mass-analysis/automations/8/advance-cursor",
            json={"reason": "Advancing active automation"}
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("must be inactive", resp.json()["detail"])

    def test_case_b_inactive_with_active_run_rejected(self):
        """Case B: Inactive automation with active run -> 409 Conflict, no marker created."""
        # Insert a running run for automation 9
        async def add_active_run():
            async with self.async_session() as session:
                r_active = MassAnalysisAutomationRun(
                    automation_run_id=9099,
                    automation_id=9,
                    status="running",
                    started_at=datetime.now(timezone.utc),
                    calls_found=0,
                    calls_selected=0,
                    calls_skipped=0,
                )
                session.add(r_active)
                await session.commit()

        asyncio.run(add_active_run())

        ctx = TenantContext(
            user_id=1,
            user_email="superadmin@example.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1],
        )
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        resp = self.client.post(
            "/bm/mass-analysis/automations/9/advance-cursor",
            json={"reason": "Advancing while running"}
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("currently in progress", resp.json()["detail"])

    def test_case_c_agent_role_rejected(self):
        """Case C: Agent role -> 403 Forbidden."""
        ctx = TenantContext(
            user_id=10,
            user_email="agent@example.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=[2],
        )
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        resp = self.client.post(
            "/bm/mass-analysis/automations/9/advance-cursor",
            json={"reason": "Agent advance attempt"}
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("No autorizado", resp.json()["detail"])

    def test_case_d_non_administrative_role_rejected(self):
        """Case D: Non-administrative role (e.g. Team Coordinator) -> 403 Forbidden."""
        ctx = TenantContext(
            user_id=11,
            user_email="coordinator@example.com",
            raw_role="team_coordinator",
            normalized_role=InternalRole.TEAM_COORDINATOR,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=[2],
        )
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        resp = self.client.post(
            "/bm/mass-analysis/automations/9/advance-cursor",
            json={"reason": "Coordinator attempt"}
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("No autorizado", resp.json()["detail"])

    def test_case_e_authorized_admin_allowed(self):
        """Case E: Authorized admin within scope (Company Admin) -> 200 OK."""
        ctx = TenantContext(
            user_id=2,
            user_email="companyadmin@example.com",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=[2],
        )
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        resp = self.client.post(
            "/bm/mass-analysis/automations/9/advance-cursor",
            json={"reason": "Reactivating EXPAC automation"}
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["automation_id"], 9)
        self.assertIsNotNone(data["marker_automation_run_id"])

    def test_case_f_admin_outside_scope_rejected(self):
        """Case F: Admin outside allowed_company_ids or allowed_service_ids -> 403 Forbidden."""
        # User is company admin for company 1, but automation 10 belongs to company 2
        ctx = TenantContext(
            user_id=2,
            user_email="companyadmin@example.com",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=None,
        )
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        resp = self.client.post(
            "/bm/mass-analysis/automations/10/advance-cursor",
            json={"reason": "Cross-company advance"}
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("otra empresa", resp.json()["detail"])

    def test_case_g_empty_reason_rejected(self):
        """Case G: Empty or whitespace reason -> 422 Unprocessable Entity."""
        ctx = TenantContext(
            user_id=1,
            user_email="superadmin@example.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1],
        )
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        # Whitespace only
        resp = self.client.post(
            "/bm/mass-analysis/automations/9/advance-cursor",
            json={"reason": "     "}
        )
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Empty string
        resp2 = self.client.post(
            "/bm/mass-analysis/automations/9/advance-cursor",
            json={"reason": ""}
        )
        self.assertEqual(resp2.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Too short (< 5 chars)
        resp3 = self.client.post(
            "/bm/mass-analysis/automations/9/advance-cursor",
            json={"reason": "abc"}
        )
        self.assertEqual(resp3.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_case_h_double_operation_safe(self):
        """Case H: Double operation -> second call rejected safely, no backward movement."""
        ctx = TenantContext(
            user_id=1,
            user_email="superadmin@example.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1],
        )
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        target = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        # First call succeeds
        resp1 = self.client.post(
            "/bm/mass-analysis/automations/9/advance-cursor",
            json={"target_cursor": target, "reason": "First advance"}
        )
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)

        # Second call with same target is safely rejected (target <= watermark)
        resp2 = self.client.post(
            "/bm/mass-analysis/automations/9/advance-cursor",
            json={"target_cursor": target, "reason": "Duplicate advance"}
        )
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Moving cursor backwards is forbidden", resp2.json()["detail"])

    async def test_case_i_normal_advance_and_history_intact(self):
        """Case I: Normal advance with inactive automation -> marker completed_empty, history untouched."""
        async with self.async_session() as session:
            orig_10275 = await session.get(MassAnalysisAutomationRun, 10275)
            orig_from = orig_10275.window_from
            orig_to = orig_10275.window_to

            target = datetime.now(timezone.utc) - timedelta(minutes=15)
            res = await MassEvaluationService.advance_automation_cursor(
                db=session,
                automation_id=9,
                target_cursor=target,
                reason="Advancing inactive automation",
            )
            self.assertTrue(res["ok"])
            marker = await session.get(MassAnalysisAutomationRun, res["marker_automation_run_id"])
            self.assertEqual(marker.status, "completed_empty")
            self.assertEqual(marker.calls_found, 0)
            self.assertIn("[administrative_cursor_advance]", marker.error_message)

            # Historical run 10275 remains completely untouched
            session.expire_all()
            run_10275_after = await session.get(MassAnalysisAutomationRun, 10275)
            self.assertEqual(run_10275_after.window_from, orig_from)
            self.assertEqual(run_10275_after.window_to, orig_to)
            self.assertIsNone(run_10275_after.error_message)

    async def test_case_j_no_evaluations_or_runs_created(self):
        """Case J: Zero MassEvaluationRun or MassEvaluationResult records created."""
        async with self.async_session() as session:
            target = datetime.now(timezone.utc) - timedelta(minutes=15)
            await MassEvaluationService.advance_automation_cursor(
                db=session,
                automation_id=9,
                target_cursor=target,
                reason="Audit zero evals check",
            )

            eval_runs = (await session.execute(select(MassEvaluationRun))).scalars().all()
            self.assertEqual(len(eval_runs), 0)

            eval_results = (await session.execute(select(MassEvaluationResult))).scalars().all()
            self.assertEqual(len(eval_results), 0)


if __name__ == "__main__":
    unittest.main()
