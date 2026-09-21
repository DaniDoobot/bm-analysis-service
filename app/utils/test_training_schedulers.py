"""
Unit tests for Granular Training Schedulers (TrainingScheduler and TrainingSchedulerAgent).
Verifies the 13 required capabilities:
1. Crear scheduler
2. Listar schedulers (con filtros)
3. Editar scheduler (y recomputar next_run_at)
4. Eliminar scheduler (en cascada con sus agentes)
5. Asociar varios agentes
6. Mismo agente en dos schedulers distintos
7. Aislamiento estricto por company_id
8. Validacion service/team (cascade y multitenancy)
9. Calculo exacto de next_run_at segun interval_days
10. Scheduler inactivo no ejecuta
11. Scheduler no vencido no ejecuta
12. Scheduler vencido ejecuta y actualiza last_run_at / next_run_at
13. Doble ejecucion no duplica ciclo
"""
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, BigInteger
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, 'sqlite')
def compile_jsonb_sqlite(type_, compiler, **kw):
    return 'JSON'

@compiles(BigInteger, 'sqlite')
def compile_bigint_sqlite(type_, compiler, **kw):
    return 'INTEGER'

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingRun,
    TrainingScheduler,
    TrainingSchedulerAgent,
)
from app.services.personalized_training_service import PersonalizedTrainingService


class TestTrainingSchedulers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ['APP_ENV'] = 'test'
        from app.config import get_settings
        settings = get_settings()
        settings.enable_training_scheduler = True

        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:', echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async with self.async_session() as db:
            c1 = Company(company_id=1, company_name='Empresa Principal', company_key='main-co', is_active=True, is_demo=False)
            c2 = Company(company_id=2, company_name='Segunda Empresa', company_key='second-co', is_active=True, is_demo=False)
            db.add_all([c1, c2])
            await db.flush()

            s10 = Service(service_id=10, company_id=1, service_key='atencion', service_name='Atencion', is_active=True)
            db.add(s10)
            await db.flush()

            t100 = Team(team_id=100, company_id=1, service_id=10, team_name='Equipo Manana', is_active=True)
            db.add(t100)

            s20 = Service(service_id=20, company_id=2, service_key='soporte', service_name='Soporte', is_active=True)
            db.add(s20)
            await db.flush()

            t200 = Team(team_id=200, company_id=2, service_id=20, team_name='Equipo Tarde', is_active=True)
            db.add(t200)

            u1 = User(
                user_id=1, company_id=1, primary_service_id=10, primary_team_id=100,
                username='agente1', email='a1@example.com', name='Agente 1', role='agent',
                hubspot_owner_id='1001', password_hash='hash', is_active=True
            )
            u2 = User(
                user_id=2, company_id=1, primary_service_id=10, primary_team_id=100,
                username='agente2', email='a2@example.com', name='Agente 2', role='agent',
                hubspot_owner_id='1002', password_hash='hash', is_active=True
            )
            u3 = User(
                user_id=3, company_id=2, primary_service_id=20, primary_team_id=200,
                username='agente3', email='a3@example.com', name='Agente 3', role='agent',
                hubspot_owner_id='2001', password_hash='hash', is_active=True
            )
            db.add_all([u1, u2, u3])

            s_ag1 = TrainingAgentSetting(hubspot_owner_id='1001', company_id=1, agent_name='Agente 1', agent_initials='A1', is_enabled=True, include_in_scheduler=True)
            s_ag2 = TrainingAgentSetting(hubspot_owner_id='1002', company_id=1, agent_name='Agente 2', agent_initials='A2', is_enabled=True, include_in_scheduler=True)
            s_ag3 = TrainingAgentSetting(hubspot_owner_id='2001', company_id=2, agent_name='Agente 3', agent_initials='A3', is_enabled=True, include_in_scheduler=True)
            db.add_all([s_ag1, s_ag2, s_ag3])

            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_01_create_scheduler(self):
        async with self.async_session() as db:
            sch = await PersonalizedTrainingService.create_scheduler(
                db,
                name='Planificador Semanal Manana',
                company_id=1,
                service_id=10,
                team_id=100,
                interval_days=7,
                lookback_days=7,
                is_active=True,
                hubspot_owner_ids=['1001']
            )
            self.assertIsNotNone(sch['scheduler_id'])
            self.assertEqual(sch['name'], 'Planificador Semanal Manana')
            self.assertEqual(sch['company_id'], 1)
            self.assertEqual(sch['service_id'], 10)
            self.assertEqual(sch['team_id'], 100)
            self.assertEqual(sch['interval_days'], 7)
            self.assertEqual(sch['lookback_days'], 7)
            self.assertTrue(sch['is_active'])
            self.assertEqual(sch['agents_count'], 1)
            self.assertEqual(sch['hubspot_owner_ids'], ['1001'])
            self.assertIsNotNone(sch['next_run_at'])

    async def test_02_list_schedulers(self):
        async with self.async_session() as db:
            await PersonalizedTrainingService.create_scheduler(
                db, name='Planificador A', company_id=1, interval_days=14, lookback_days=14, is_active=True
            )
            await PersonalizedTrainingService.create_scheduler(
                db, name='Planificador B Inactivo', company_id=1, interval_days=7, lookback_days=7, is_active=False
            )
            await PersonalizedTrainingService.create_scheduler(
                db, name='Planificador C Empresa 2', company_id=2, interval_days=30, lookback_days=30, is_active=True
            )

            scheds_c1 = await PersonalizedTrainingService.list_schedulers(db, company_ids=[1])
            self.assertEqual(len(scheds_c1), 2)

            scheds_c1_active = await PersonalizedTrainingService.list_schedulers(db, company_ids=[1], is_active=True)
            self.assertEqual(len(scheds_c1_active), 1)
            self.assertEqual(scheds_c1_active[0]['name'], 'Planificador A')

            scheds_c2 = await PersonalizedTrainingService.list_schedulers(db, company_ids=[2])
            self.assertEqual(len(scheds_c2), 1)
            self.assertEqual(scheds_c2[0]['name'], 'Planificador C Empresa 2')

    async def test_03_update_scheduler(self):
        async with self.async_session() as db:
            sch = await PersonalizedTrainingService.create_scheduler(
                db, name='Original', company_id=1, interval_days=14, lookback_days=14
            )
            sch_id = sch['scheduler_id']
            orig_next_run = sch['next_run_at']

            updated = await PersonalizedTrainingService.update_scheduler(
                db,
                scheduler_id=sch_id,
                name='Nombre Editado',
                interval_days=5,
                lookback_days=10,
                is_active=False
            )
            self.assertEqual(updated['name'], 'Nombre Editado')
            self.assertEqual(updated['interval_days'], 5)
            self.assertEqual(updated['lookback_days'], 10)
            self.assertFalse(updated['is_active'])
            self.assertNotEqual(updated['next_run_at'], orig_next_run)

    async def test_04_delete_scheduler(self):
        async with self.async_session() as db:
            sch = await PersonalizedTrainingService.create_scheduler(
                db, name='Para Eliminar', company_id=1, hubspot_owner_ids=['1001', '1002']
            )
            sch_id = sch['scheduler_id']

            stmt = select(TrainingSchedulerAgent).where(TrainingSchedulerAgent.scheduler_id == sch_id)
            res = await db.execute(stmt)
            self.assertEqual(len(res.scalars().all()), 2)

            del_ok = await PersonalizedTrainingService.delete_scheduler(db, sch_id)
            self.assertTrue(del_ok)

            sch_check = await PersonalizedTrainingService.get_scheduler_by_id(db, sch_id)
            self.assertIsNone(sch_check)

            res_agents = await db.execute(stmt)
            self.assertEqual(len(res_agents.scalars().all()), 0)

    async def test_05_multiple_agents_association(self):
        async with self.async_session() as db:
            sch = await PersonalizedTrainingService.create_scheduler(
                db, name='Planificador Multiagente', company_id=1, hubspot_owner_ids=['1001', '1002']
            )
            self.assertEqual(sch['agents_count'], 2)
            self.assertIn('1001', sch['hubspot_owner_ids'])
            self.assertIn('1002', sch['hubspot_owner_ids'])

            updated = await PersonalizedTrainingService.update_scheduler(
                db, scheduler_id=sch['scheduler_id'], hubspot_owner_ids=['1001']
            )
            self.assertEqual(updated['agents_count'], 1)
            self.assertEqual(updated['hubspot_owner_ids'], ['1001'])

    async def test_06_same_agent_in_multiple_schedulers(self):
        async with self.async_session() as db:
            sch1 = await PersonalizedTrainingService.create_scheduler(
                db, name='Planificador 1', company_id=1, interval_days=7, hubspot_owner_ids=['1001']
            )
            sch2 = await PersonalizedTrainingService.create_scheduler(
                db, name='Planificador 2', company_id=1, interval_days=14, hubspot_owner_ids=['1001', '1002']
            )
            self.assertIn('1001', sch1['hubspot_owner_ids'])
            self.assertIn('1001', sch2['hubspot_owner_ids'])

            stmt = select(TrainingSchedulerAgent).where(TrainingSchedulerAgent.hubspot_owner_id == '1001')
            res = await db.execute(stmt)
            rows = res.scalars().all()
            self.assertEqual(len(rows), 2)
            scheduler_ids = {r.scheduler_id for r in rows}
            self.assertEqual(scheduler_ids, {sch1['scheduler_id'], sch2['scheduler_id']})

    async def test_07_multitenant_isolation(self):
        async with self.async_session() as db:
            sch1 = await PersonalizedTrainingService.create_scheduler(
                db, name='Scheduler Co 1', company_id=1, hubspot_owner_ids=['1001']
            )
            sch2 = await PersonalizedTrainingService.create_scheduler(
                db, name='Scheduler Co 2', company_id=2, hubspot_owner_ids=['2001']
            )

            list_c1 = await PersonalizedTrainingService.list_schedulers(db, company_ids=[1])
            self.assertEqual(len(list_c1), 1)
            self.assertEqual(list_c1[0]['scheduler_id'], sch1['scheduler_id'])

            list_c2 = await PersonalizedTrainingService.list_schedulers(db, company_ids=[2])
            self.assertEqual(len(list_c2), 1)
            self.assertEqual(list_c2[0]['scheduler_id'], sch2['scheduler_id'])

    async def test_08_service_team_cascade_validation(self):
        from app.utils.team_resolvers import validate_team_service_cascade
        from app.core.tenant_context import TenantContext
        from fastapi import HTTPException

        from app.core.roles import InternalRole
        super_admin_ctx = TenantContext(
            user_id=999, company_id=1, role='super_admin',
            raw_role='super_admin', normalized_role=InternalRole.SUPER_ADMIN,
            user_email='admin@example.com',
            is_super_admin=True, is_company_admin=False, is_service_manager=False, is_team_coordinator=False,
            allowed_company_ids=[1, 2], allowed_service_ids=None, allowed_team_ids=None, allowed_agent_ids=None
        )

        async with self.async_session() as db:
            await validate_team_service_cascade(db, service_id=10, team_id=100, context=super_admin_ctx, company_id=1)

            with self.assertRaises(HTTPException) as ctx_err:
                await validate_team_service_cascade(db, service_id=10, team_id=200, context=super_admin_ctx, company_id=1)
            self.assertIn(ctx_err.exception.status_code, [400, 404])

    async def test_09_next_run_at_calculation(self):
        async with self.async_session() as db:
            now = datetime.now(timezone.utc)
            sch = await PersonalizedTrainingService.create_scheduler(
                db, name='Interval Check', company_id=1, interval_days=10
            )
            expected_next = now + timedelta(days=10)
            diff_seconds = abs((sch['next_run_at'].replace(tzinfo=timezone.utc) - expected_next).total_seconds())
            self.assertLess(diff_seconds, 60)

    async def test_10_inactive_scheduler_does_not_execute(self):
        async with self.async_session() as db:
            sch = await PersonalizedTrainingService.create_scheduler(
                db, name='Inactivo', company_id=1, is_active=False, hubspot_owner_ids=['1001']
            )
            res = await PersonalizedTrainingService.execute_scheduler(db, scheduler=sch['scheduler_id'], force=False)
            self.assertIsNone(res)

    async def test_11_future_scheduler_does_not_execute(self):
        async with self.async_session() as db:
            sch = await PersonalizedTrainingService.create_scheduler(
                db, name='Futuro', company_id=1, is_active=True, interval_days=14, hubspot_owner_ids=['1001']
            )
            res = await PersonalizedTrainingService.execute_scheduler(db, scheduler=sch['scheduler_id'], force=False)
            self.assertIsNone(res)

    async def test_12_due_scheduler_executes_and_updates_run_dates(self):
        now = datetime.now(timezone.utc)
        async with self.async_session() as db:
            sch = await PersonalizedTrainingService.create_scheduler(
                db, name='Vencido', company_id=1, is_active=True, interval_days=7, lookback_days=7, hubspot_owner_ids=['1001']
            )
            sch_id = sch['scheduler_id']

            past_time = now - timedelta(hours=2)
            stmt = select(TrainingScheduler).where(TrainingScheduler.scheduler_id == sch_id)
            res_db = await db.execute(stmt)
            sch_model = res_db.scalars().first()
            sch_model.next_run_at = past_time
            await db.commit()

            mock_run = TrainingRun(
                training_run_id=501,
                period_start=now - timedelta(days=7),
                period_end=now,
                status='completed',
                triggered_by='scheduler',
                started_at=now,
                finished_at=now,
                agents_total=1,
                agents_completed=1,
                agents_failed=0,
            )

            with patch.object(PersonalizedTrainingService, 'run_personalized_training_pass', AsyncMock(return_value=mock_run)):
                exec_res = await PersonalizedTrainingService.execute_scheduler(db, scheduler=sch_id, force=False)
                self.assertIsNotNone(exec_res)
                self.assertTrue(exec_res['triggered'])
                self.assertEqual(exec_res['run_id'], 501)
                self.assertEqual(exec_res['status'], 'completed')

            refreshed = await PersonalizedTrainingService.get_scheduler_by_id(db, sch_id)
            self.assertIsNotNone(refreshed['last_run_at'])
            self.assertEqual(refreshed['last_status'], 'completed')
            self.assertGreater(refreshed['next_run_at'].replace(tzinfo=timezone.utc), now)

    async def test_13_duplicate_execution_prevention(self):
        now = datetime.now(timezone.utc)
        async with self.async_session() as db:
            sch = await PersonalizedTrainingService.create_scheduler(
                db, name='AntiDuplicados', company_id=1, is_active=True, interval_days=7, hubspot_owner_ids=['1001']
            )
            sch_id = sch['scheduler_id']

            stmt = select(TrainingScheduler).where(TrainingScheduler.scheduler_id == sch_id)
            res_db = await db.execute(stmt)
            sch_model = res_db.scalars().first()
            sch_model.next_run_at = now - timedelta(hours=1)
            await db.commit()

            mock_run = TrainingRun(
                training_run_id=601,
                status='completed',
                triggered_by='scheduler',
                agents_total=1,
                agents_completed=1,
                agents_failed=0,
            )

            with patch.object(PersonalizedTrainingService, 'run_personalized_training_pass', AsyncMock(return_value=mock_run)):
                first_res = await PersonalizedTrainingService.execute_scheduler(db, scheduler=sch_id, force=False)
                self.assertTrue(first_res['triggered'])

                second_res = await PersonalizedTrainingService.execute_scheduler(db, scheduler=sch_id, force=False)
                self.assertIsNone(second_res)

    async def test_14_multiple_schedulers_simultaneous_execution(self):
        """Verify two schedulers active simultaneously run their own agents, intervals, and lookbacks."""
        now = datetime.now(timezone.utc)
        async with self.async_session() as db:
            sch_a = await PersonalizedTrainingService.create_scheduler(
                db, name='Sched A (Company 1)', company_id=1, is_active=True,
                interval_days=7, lookback_days=14, hubspot_owner_ids=['1001']
            )
            sch_b = await PersonalizedTrainingService.create_scheduler(
                db, name='Sched B (Company 2)', company_id=2, is_active=True,
                interval_days=10, lookback_days=21, hubspot_owner_ids=['2001']
            )

            # Set both to be due in the past
            past_time = now - timedelta(hours=1)
            stmt = select(TrainingScheduler).where(TrainingScheduler.scheduler_id.in_([sch_a['scheduler_id'], sch_b['scheduler_id']]))
            res = await db.execute(stmt)
            for m in res.scalars().all():
                m.next_run_at = past_time
            await db.commit()

            runs_created = []
            async def fake_pass(**kwargs):
                r = TrainingRun(
                    training_run_id=700 + len(runs_created),
                    period_start=kwargs.get('period_start'),
                    period_end=kwargs.get('period_end'),
                    status='completed',
                    triggered_by='scheduler',
                    created_by_email=kwargs.get('created_by_email'),
                    company_id=kwargs.get('company_ids', [None])[0] if kwargs.get('company_ids') else None,
                    agents_total=len(kwargs.get('hubspot_owner_ids', [])),
                    agents_completed=len(kwargs.get('hubspot_owner_ids', [])),
                    agents_failed=0,
                )
                runs_created.append((kwargs, r))
                return r

            with patch.object(PersonalizedTrainingService, 'run_personalized_training_pass', side_effect=fake_pass):
                res_jobs = await PersonalizedTrainingService.run_due_training_jobs(db)
                self.assertTrue(res_jobs['triggered'])
                self.assertEqual(res_jobs['scheduler_mode'], 'granular')
                self.assertEqual(len(res_jobs['results']), 2)

            self.assertEqual(len(runs_created), 2)
            # Verify Scheduler A pass
            call_a = runs_created[0][0]
            self.assertEqual(call_a['hubspot_owner_ids'], ['1001'])
            self.assertEqual(call_a['created_by_email'], f"scheduler:{sch_a['scheduler_id']}")
            self.assertEqual(call_a['company_ids'], [1])
            expected_lookback_a = (call_a['period_end'] - call_a['period_start']).days + 1
            self.assertEqual(expected_lookback_a, 14)

            # Verify Scheduler B pass
            call_b = runs_created[1][0]
            self.assertEqual(call_b['hubspot_owner_ids'], ['2001'])
            self.assertEqual(call_b['created_by_email'], f"scheduler:{sch_b['scheduler_id']}")
            self.assertEqual(call_b['company_ids'], [2])
            expected_lookback_b = (call_b['period_end'] - call_b['period_start']).days + 1
            self.assertEqual(expected_lookback_b, 21)

            # Check next_run_at calculation independence
            ref_a = await PersonalizedTrainingService.get_scheduler_by_id(db, sch_a['scheduler_id'])
            ref_b = await PersonalizedTrainingService.get_scheduler_by_id(db, sch_b['scheduler_id'])
            self.assertEqual(ref_a['last_status'], 'completed')
            self.assertEqual(ref_b['last_status'], 'completed')
            self.assertGreater(ref_a['next_run_at'].replace(tzinfo=timezone.utc), now + timedelta(days=6))
            self.assertGreater(ref_b['next_run_at'].replace(tzinfo=timezone.utc), now + timedelta(days=9))

    async def test_15_scheduler_error_isolation(self):
        """Verify that an error executing Scheduler A does not block Scheduler B from executing."""
        now = datetime.now(timezone.utc)
        async with self.async_session() as db:
            sch_fail = await PersonalizedTrainingService.create_scheduler(
                db, name='Sched Fail', company_id=1, is_active=True, interval_days=7, hubspot_owner_ids=['1001']
            )
            sch_ok = await PersonalizedTrainingService.create_scheduler(
                db, name='Sched OK', company_id=1, is_active=True, interval_days=7, hubspot_owner_ids=['1002']
            )

            stmt = select(TrainingScheduler).where(TrainingScheduler.scheduler_id.in_([sch_fail['scheduler_id'], sch_ok['scheduler_id']]))
            res = await db.execute(stmt)
            for m in res.scalars().all():
                m.next_run_at = now - timedelta(hours=1)
            await db.commit()

            async def fake_pass(**kwargs):
                if kwargs.get('created_by_email') == f"scheduler:{sch_fail['scheduler_id']}":
                    raise RuntimeError("Gemini API connection error on scheduler A")
                return TrainingRun(
                    training_run_id=888,
                    status='completed',
                    triggered_by='scheduler',
                    created_by_email=kwargs.get('created_by_email'),
                    agents_total=1,
                    agents_completed=1,
                    agents_failed=0,
                )

            with patch.object(PersonalizedTrainingService, 'run_personalized_training_pass', side_effect=fake_pass):
                res_jobs = await PersonalizedTrainingService.run_due_training_jobs(db)
                self.assertEqual(res_jobs['scheduler_mode'], 'granular')
                # Scheduler B succeeded, overall triggered should still be True
                self.assertTrue(res_jobs['triggered'])
                self.assertEqual(len(res_jobs['results']), 2)

            ref_fail = await PersonalizedTrainingService.get_scheduler_by_id(db, sch_fail['scheduler_id'])
            ref_ok = await PersonalizedTrainingService.get_scheduler_by_id(db, sch_ok['scheduler_id'])
            self.assertEqual(ref_fail['last_status'], 'failed')
            self.assertEqual(ref_ok['last_status'], 'completed')

    async def test_16_concurrent_running_lock(self):
        """Verify that a scheduler in 'running' status (< 15 min) skips duplicate execution even when forced."""
        now = datetime.now(timezone.utc)
        async with self.async_session() as db:
            sch = await PersonalizedTrainingService.create_scheduler(
                db, name='Running Test', company_id=1, is_active=True, interval_days=7, hubspot_owner_ids=['1001']
            )
            sch_id = sch['scheduler_id']

            stmt = select(TrainingScheduler).where(TrainingScheduler.scheduler_id == sch_id)
            res = await db.execute(stmt)
            m = res.scalars().first()
            m.last_status = 'running'
            m.last_run_at = now - timedelta(minutes=2)
            await db.commit()

            # Attempt execution while running
            res_running = await PersonalizedTrainingService.execute_scheduler(db, scheduler=sch_id, force=True)
            self.assertIsNotNone(res_running)
            self.assertFalse(res_running['triggered'])
            self.assertEqual(res_running['reason'], 'Scheduler already running')

    async def test_17_legacy_fallback_when_zero_granular(self):
        """Verify fallback to legacy scheduler settings when 0 granular schedulers exist."""
        now = datetime.now(timezone.utc)
        async with self.async_session() as db:
            # Delete all granular schedulers in this session
            stmt = select(TrainingScheduler)
            res = await db.execute(stmt)
            for s in res.scalars().all():
                await db.delete(s)
            await db.commit()

            # Configure legacy persistent settings
            db_settings = await PersonalizedTrainingService.get_or_create_scheduler_settings(db)
            db_settings.is_enabled = True
            db_settings.next_run_at = now - timedelta(hours=1)
            await db.commit()

            mock_run = TrainingRun(
                training_run_id=999,
                status='completed',
                triggered_by='scheduler',
                agents_total=2,
                agents_completed=2,
                agents_failed=0,
            )

            with patch.object(PersonalizedTrainingService, 'run_personalized_training_pass', AsyncMock(return_value=mock_run)):
                res_legacy = await PersonalizedTrainingService.run_due_training_jobs(db)
                self.assertTrue(res_legacy['triggered'])
                self.assertEqual(res_legacy['run_id'], 999)


if __name__ == '__main__':
    unittest.main()
