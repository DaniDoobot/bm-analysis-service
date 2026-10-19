# -*- coding: utf-8 -*-
"""
Tests for Training Cycle service context resolution, Agent cycle viewing,
and error message sanitization.

Covers:
1. Agent with TrainingAgentSetting can consult their cycle without AttributeError
2. Canonical resolution of service_id:
   - from TrainingRun.service_id
   - from User.primary_service
   - from User.primary_team.service
   - from single N:M assigned service
3. Completed cycle retains completed status, prompts and simulation counts
4. Existing report is visible and accessible
5. Simulations reflect real execution status
6. Scoping: Agent cannot consult other agents' cycles
7. Super admin continues working and viewing history
8. Error message sanitization masks internal Python exceptions for agents
9. Static assertion: no invalid TrainingAgentSetting.service_id lookups
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

DB_FILE = 'test_cycle_service_context.db'
os.environ['DATABASE_URL'] = f'sqlite+aiosqlite:///{DB_FILE}'
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, 'sqlite')
def compile_jsonb_sqlite(type_, compiler, **kw):
    return 'JSON'

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from app.db import Base, get_engine
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingRun,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
)
from app.services.personalized_training_service import PersonalizedTrainingService
from app.routers.personalized_training import (
    sanitize_report_for_agent,
    get_my_current_training,
    get_agent_current_training,
)
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from fastapi import HTTPException


class TestTrainerCycleServiceContext(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        if os.path.exists(DB_FILE):
            try:
                os.remove(DB_FILE)
            except Exception:
                pass

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async with self.async_session() as db:
            # 1. Company
            self.company = Company(company_id=1, company_name='Boston Medical Group', company_key='boston', is_active=True)
            db.add(self.company)

            # 2. Services
            self.svc_front = Service(service_id=1, company_id=1, service_name='Front', service_key='front', is_active=True)
            self.svc_expac = Service(service_id=2, company_id=1, service_name='Experiencia de Paciente', service_key='expac', is_active=True)
            db.add_all([self.svc_front, self.svc_expac])

            # 3. Team with service_id = 1
            self.team_front = Team(team_id=1, company_id=1, service_id=1, team_name='Equipo Front Principal')
            db.add(self.team_front)

            # 4. Users
            # Agent 1 (Luci): has primary_team_id=1, primary_service_id=None
            self.u_luci = User(
                user_id=76,
                company_id=1,
                username='ldossantos',
                email='luci@boston.com',
                name='Luci Dos Santos',
                role='agente',
                hubspot_owner_id='1375831790',
                is_active=True,
                primary_team_id=1,
                primary_service_id=None,
                password_hash='x'
            )
            # Agent 2 (Bryan): has primary_service_id=1 directly
            self.u_bryan = User(
                user_id=196,
                company_id=1,
                username='bherrera',
                email='bryan@boston.com',
                name='Bryan Herrera',
                role='agente',
                hubspot_owner_id='33013277',
                is_active=True,
                primary_service_id=1,
                password_hash='x'
            )
            # Super Admin
            self.u_admin = User(
                user_id=1,
                company_id=1,
                username='admin',
                email='admin@boston.com',
                name='Super Admin',
                role='super_admin',
                is_active=True,
                password_hash='x'
            )
            db.add_all([self.u_luci, self.u_bryan, self.u_admin])
            await db.flush()

            # 5. TrainingAgentSetting (Notice: NO service_id column exists!)
            self.setting_luci = TrainingAgentSetting(
                setting_id=2,
                company_id=1,
                hubspot_owner_id='1375831790',
                agent_name='Luci Dos Santos',
                agent_initials='LD',
                is_enabled=True,
                include_in_scheduler=True,
            )
            self.setting_bryan = TrainingAgentSetting(
                setting_id=6,
                company_id=1,
                hubspot_owner_id='33013277',
                agent_name='Bryan Herrera',
                agent_initials='BH',
                is_enabled=False,
                include_in_scheduler=True,
            )
            db.add_all([self.setting_luci, self.setting_bryan])

            # 6. TrainingRun
            self.run_107 = TrainingRun(
                training_run_id=107,
                company_id=1,
                service_id=1,
                period_start=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 10, 14, 23, 59, 59, tzinfo=timezone.utc),
                status='running',
                triggered_by='scheduler'
            )
            db.add(self.run_107)

            # 7. Completed historical report for Luci (Report 274)
            self.report_274 = TrainingAgentReport(
                training_report_id=274,
                training_run_id=103,
                company_id=1,
                service_id=1,
                hubspot_owner_id='1375831790',
                agent_name='Luci Dos Santos',
                agent_initials='LD',
                period_start=datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 9, 27, 23, 59, 59, tzinfo=timezone.utc),
                status='completed',
                is_current=True,
                evaluations_count=4,
                calls_count=4,
            )
            db.add(self.report_274)
            await db.flush()

            # Add prompts and simulations to report 274
            for i in range(1, 5):
                p = TrainingSimulationPrompt(
                    simulation_prompt_id=i,
                    training_report_id=274,
                    hubspot_owner_id='1375831790',
                    prompt_number=i,
                    title=f'Simulacion {i}',
                    scenario_type='roleplay',
                    prompt_text='Instrucciones secretas del prompt'
                )
                db.add(p)
                await db.flush()
                c = TrainingCompletionStatus(
                    completion_id=i,
                    training_report_id=274,
                    simulation_prompt_id=i,
                    hubspot_owner_id='1375831790',
                    status='completed'
                )
                db.add(c)

            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        if os.path.exists(DB_FILE):
            try:
                os.remove(DB_FILE)
            except Exception:
                pass

    # 1. Canonical Service Resolution Tests
    async def test_resolve_agent_service_id_from_run(self):
        """Should resolve service_id from TrainingRun if run has service_id."""
        async with self.async_session() as db:
            sid = await PersonalizedTrainingService.resolve_agent_service_id(
                db, hubspot_owner_id='1375831790', run_id=107
            )
            self.assertEqual(sid, 1)

    async def test_resolve_agent_service_id_from_primary_team(self):
        """Should resolve service_id from User.primary_team.service when primary_service is None."""
        async with self.async_session() as db:
            sid = await PersonalizedTrainingService.resolve_agent_service_id(
                db, hubspot_owner_id='1375831790', run_id=None
            )
            self.assertEqual(sid, 1)

    async def test_resolve_agent_service_id_from_primary_service(self):
        """Should resolve service_id from User.primary_service directly."""
        async with self.async_session() as db:
            sid = await PersonalizedTrainingService.resolve_agent_service_id(
                db, hubspot_owner_id='33013277', run_id=None
            )
            self.assertEqual(sid, 1)

    # 2. Report Generation without AttributeError
    @patch('app.services.personalized_training_service.complete_text')
    @patch('app.services.personalized_training_service.PersonalizedTrainingService.aggregate_agent_evaluations')
    async def test_generate_report_for_agent_success(self, mock_agg, mock_complete):
        """Report generation must not raise AttributeError and must pass service_id to complete_text."""
        mock_agg.return_value = {
            "evaluations_count": 5,
            "calls_count": 5,
            "avg_evaluacion_global": Decimal("7.5"),
            "criteria_averages": {},
            "tipologia_distribution": {},
            "critical_feedbacks": [],
            "cierre_cita_rate": Decimal("80.0"),
        }
        mock_complete.return_value = '{"strengths": ["Escucha"], "weaknesses": ["Cierre"], "summary": "Buen desempeno", "objectives": []}'

        async with self.async_session() as db:
            period_start = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
            period_end = datetime(2026, 10, 14, 23, 59, 59, tzinfo=timezone.utc)

            rep = await PersonalizedTrainingService.generate_report_for_agent(
                db,
                hubspot_owner_id='1375831790',
                period_start=period_start,
                period_end=period_end,
                run_id=107
            )

            self.assertIsNotNone(rep)
            self.assertEqual(rep.status, 'pending_approval')
            self.assertEqual(rep.service_id, 1)
            self.assertEqual(rep.company_id, 1)

            # Verify complete_text was called with service_id=1 and company_id=1
            self.assertTrue(mock_complete.called)
            kwargs = mock_complete.call_args.kwargs
            self.assertEqual(kwargs.get('service_id'), 1)
            self.assertEqual(kwargs.get('company_id'), 1)

    # 3. Error handling: Failed report persists service_id and handles retries
    @patch('app.services.personalized_training_service.complete_text')
    @patch('app.services.personalized_training_service.PersonalizedTrainingService.aggregate_agent_evaluations')
    async def test_generate_report_handles_failure_and_retries(self, mock_agg, mock_complete):
        """When complete_text raises, failed report record stores service_id and allows retry."""
        mock_agg.return_value = {
            "evaluations_count": 5,
            "calls_count": 5,
            "avg_evaluacion_global": Decimal("7.5"),
            "criteria_averages": {},
            "tipologia_distribution": {},
            "critical_feedbacks": [],
            "cierre_cita_rate": Decimal("80.0"),
        }
        mock_complete.side_effect = RuntimeError("OpenAI rate limit")

        async with self.async_session() as db:
            period_start = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
            period_end = datetime(2026, 10, 14, 23, 59, 59, tzinfo=timezone.utc)

            # 1. Trigger failure (complete_text raises, generate_report_for_agent catches and saves failed_report)
            failed_rep = await PersonalizedTrainingService.generate_report_for_agent(
                db,
                hubspot_owner_id='1375831790',
                period_start=period_start,
                period_end=period_end,
                run_id=107
            )

            self.assertIsNotNone(failed_rep)
            self.assertEqual(failed_rep.status, 'failed')
            self.assertEqual(failed_rep.service_id, 1)

            # 2. Retry generation now succeeds
            mock_complete.side_effect = None
            mock_complete.return_value = '{"strengths": ["Escucha"], "weaknesses": ["Cierre"], "summary": "OK", "objectives": []}'

            rep_retry = await PersonalizedTrainingService.generate_report_for_agent(
                db,
                hubspot_owner_id='1375831790',
                period_start=period_start,
                period_end=period_end,
                run_id=107
            )
            self.assertIsNotNone(rep_retry)
            self.assertEqual(rep_retry.status, 'pending_approval')

    # 4. Error Message Sanitization Test
    def test_sanitize_report_for_agent_masks_exceptions(self):
        """Internal Python exceptions must be masked for agents."""
        raw_report = {
            "training_report_id": 286,
            "status": "failed",
            "error_message": "'TrainingAgentSetting' object has no attribute 'service_id'",
            "prompts": [{"prompt_text": "Sensitive system instructions"}],
            "simulations": [{"prompt_text": "Secret prompt", "improvement_objectives": ["obj1"]}],
        }

        sanitized = sanitize_report_for_agent(raw_report)
        self.assertNotIn("TrainingAgentSetting", sanitized["error_message"])
        self.assertIn("no pudo completarse", sanitized["error_message"])
        self.assertEqual(sanitized["prompts"][0]["prompt_text"], "")
        self.assertEqual(sanitized["simulations"][0]["prompt_text"], "")
        self.assertIsNone(sanitized["simulations"][0]["improvement_objectives"])

    # 5. Completed Report Data Integrity & Visibility
    async def test_completed_report_retains_data_and_is_visible(self):
        """Historical completed reports must retain completed status, simulations, and prompts."""
        async with self.async_session() as db:
            detail = await PersonalizedTrainingService.get_agent_detail(
                db, hubspot_owner_id='1375831790'
            )
            self.assertIsNotNone(detail)
            cur = detail.get("current_report")
            self.assertIsNotNone(cur)
            self.assertEqual(cur["status"], "completed")
            self.assertEqual(len(cur.get("simulations", [])), 4)
            self.assertEqual(cur.get("progress_completed"), 4)

    # 6. Scoping: Agent Cannot Consult Other Agents' Cycles
    async def test_agent_cannot_access_other_agent_cycles(self):
        """An agent querying another agent's endpoint must receive 403 Forbidden."""
        agent_context = TenantContext(
            user_id=76,
            user_email='luci@boston.com',
            user_role='agente',
            raw_role='agente',
            normalized_role=InternalRole.AGENT,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
            allowed_agent_ids=['1375831790'],
            is_super_admin=False
        )

        async with self.async_session() as db:
            # Trying to access Bryan's training report (33013277)
            with self.assertRaises(HTTPException) as ctx:
                await get_agent_current_training(
                    hubspot_owner_id='33013277',
                    context=agent_context,
                    db=db
                )
            self.assertEqual(ctx.exception.status_code, 403)

    # 7. Super Admin Can Access Agent Cycles
    async def test_super_admin_can_access_agent_cycles(self):
        """Super admin context can access agent cycle without restrictions."""
        admin_context = TenantContext(
            user_id=1,
            user_email='admin@boston.com',
            user_role='super_admin',
            raw_role='super_admin',
            normalized_role=InternalRole.SUPER_ADMIN,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=None,
            allowed_agent_ids=None,
            is_super_admin=True
        )

        async with self.async_session() as db:
            res = await get_agent_current_training(
                hubspot_owner_id='1375831790',
                context=admin_context,
                db=db
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["status"], "completed")

    # 8. Static Audit: No invalid TrainingAgentSetting.service_id lookups
    def test_static_audit_no_invalid_setting_service_id_lookups(self):
        """Ensure no code in app/ accesses .service_id on TrainingAgentSetting model directly."""
        import glob
        app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))
        offending_lines = []
        for f in glob.glob(f'{app_dir}/**/*.py', recursive=True):
            with open(f, 'r', encoding='utf-8') as fp:
                for idx, line in enumerate(fp, 1):
                    # Check for direct attribute access on agent_setting or training_agent_setting
                    if 'agent_setting.service_id' in line or 'training_agent_setting.service_id' in line or 'TrainingAgentSetting.service_id' in line:
                        offending_lines.append(f'{f}:{idx}: {line.strip()}')
        self.assertEqual(offending_lines, [], f"Found invalid references: {offending_lines}")


if __name__ == '__main__':
    unittest.main()
