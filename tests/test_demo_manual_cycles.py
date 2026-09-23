
# -*- coding: utf-8 -*-
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ['DATABASE_URL'] = 'sqlite+aiosqlite:///demo_manual_test.db'
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, 'sqlite')
def compile_jsonb_sqlite(type_, compiler, **kw):
    return 'JSON'

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from app.db import Base, get_engine
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
)
from app.services.personalized_training_service import PersonalizedTrainingService
from app.utils.security import create_access_token
from app.main import app
from httpx import AsyncClient, ASGITransport


class TestDemoManualCycles(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        
        if os.path.exists('demo_manual_test.db'):
            try:
                os.remove('demo_manual_test.db')
            except Exception:
                pass

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async with self.async_session() as db:
            self.c_boston = Company(
                company_id=1, company_name='Boston Medical Group', company_key='boston-medical', is_active=True, is_demo=False
            )
            self.c_demo = Company(
                company_id=7, company_name='Empresa Demo', company_key='empresa-demo', is_active=True, is_demo=True
            )
            db.add_all([self.c_boston, self.c_demo])
            await db.flush()

            self.s_boston = Service(
                service_id=1, service_name='Front Desk Boston', service_key='front-boston', company_id=1
            )
            self.s_demo = Service(
                service_id=7, service_name='Servicio Demo Comercial', service_key='demo-comercial', company_id=7
            )
            db.add_all([self.s_boston, self.s_demo])
            await db.flush()

            self.t_boston = Team(
                team_id=1, team_name='Equipo Boston', company_id=1, service_id=1
            )
            self.t_demo = Team(
                team_id=7, team_name='Equipo Demo', company_id=7, service_id=7
            )
            db.add_all([self.t_boston, self.t_demo])
            await db.flush()

            self.u_super = User(
                user_id=1, username='superadmin', email='super@example.com', role='admin', password_hash='dummy'
            )
            self.u_demo_admin = User(
                user_id=2, username='demo_admin', email='admin@demo.com', role='company_admin', company_id=7, password_hash='dummy'
            )
            self.u_boston_admin = User(
                user_id=3, username='boston_admin', email='admin@boston.com', role='company_admin', company_id=1, password_hash='dummy'
            )
            self.u_demo_agent = User(
                user_id=10, username='demo_agent_1', email='agent1@demo.com', role='agent', company_id=7, hubspot_owner_id='demo_owner_01', primary_service_id=7, password_hash='dummy'
            )
            self.u_boston_agent = User(
                user_id=11, username='boston_agent_1', email='agent1@boston.com', role='agente', company_id=1, hubspot_owner_id='boston_owner_01', primary_service_id=1, password_hash='dummy'
            )
            db.add_all([self.u_super, self.u_demo_admin, self.u_boston_admin, self.u_demo_agent, self.u_boston_agent])
            await db.flush()

            self.setting_demo = TrainingAgentSetting(
                setting_id=1,
                company_id=7,
                hubspot_owner_id='demo_owner_01',
                agent_name='Demo Agente Uno',
                agent_initials='DA',
                is_enabled=False,
                include_in_scheduler=True,
            )
            self.setting_boston = TrainingAgentSetting(
                setting_id=2,
                company_id=1,
                hubspot_owner_id='boston_owner_01',
                agent_name='Boston Agente Uno',
                agent_initials='BA',
                is_enabled=False,
                include_in_scheduler=True,
            )
            db.add_all([self.setting_demo, self.setting_boston])
            await db.commit()

        self.tok_super = create_access_token({'user_id': 1, 'email': 'super@example.com'})
        self.tok_demo_admin = create_access_token({'user_id': 2, 'email': 'admin@demo.com'})
        self.tok_boston_admin = create_access_token({'user_id': 3, 'email': 'admin@boston.com'})
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver')

    async def asyncTearDown(self):
        await self.engine.dispose()
        if os.path.exists('demo_manual_test.db'):
            try:
                os.remove('demo_manual_test.db')
            except Exception:
                pass

    async def test_01_demo_agent_disabled_receives_manual_cycle(self):
        async with self.async_session() as db:
            reports = await PersonalizedTrainingService.create_manual_cycles(
                db=db,
                hubspot_owner_ids=['demo_owner_01'],
                title='Ciclo Demo Manual de Calidad',
                general_objectives=['Mejorar la deteccion de necesidades.'],
                specific_objectives=['Aplicar escucha activa sin interrumpir al cliente.']
            )
            self.assertEqual(len(reports), 1)
            rep = reports[0]
            self.assertEqual(rep.hubspot_owner_id, 'demo_owner_01')
            self.assertEqual(rep.status, 'in_progress')
            self.assertEqual(rep.company_id, 7)
            self.assertEqual(rep.cycle_mode, 'manual')

    async def test_02_demo_agent_disabled_excluded_from_automatic_scheduler(self):
        async with self.async_session() as db:
            with patch.object(
                PersonalizedTrainingService,
                'generate_report_for_agent',
                new=AsyncMock(return_value=None)
            ) as mock_gen:
                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    triggered_by='scheduler'
                )
                called_owners = [c.kwargs.get('hubspot_owner_id') for c in mock_gen.call_args_list]
                self.assertNotIn('demo_owner_01', called_owners)
                self.assertEqual(run.agents_total, 0)

    async def test_03_normal_agent_disabled_excluded_from_automatic_scheduler(self):
        async with self.async_session() as db:
            with patch.object(
                PersonalizedTrainingService,
                'generate_report_for_agent',
                new=AsyncMock(return_value=None)
            ) as mock_gen:
                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    triggered_by='scheduler'
                )
                called_owners = [c.kwargs.get('hubspot_owner_id') for c in mock_gen.call_args_list]
                self.assertNotIn('boston_owner_01', called_owners)

    async def test_04_demo_admin_cannot_create_cycle_for_other_company(self):
        res = await self.client.post(
            '/bm/training/admin/manual-cycle',
            json={
                'hubspot_owner_ids': ['boston_owner_01'],
                'title': 'Intento no autorizado',
                'general_objectives': ['Objetivo'],
                'specific_objectives': ['Objetivo especifico']
            },
            headers={'Authorization': f'Bearer {self.tok_demo_admin}'}
        )
        self.assertEqual(res.status_code, 403)

    async def test_05_demo_manual_cycle_prompts_have_no_boston_or_patient_terms(self):
        async with self.async_session() as db:
            reports = await PersonalizedTrainingService.create_manual_cycles(
                db=db,
                hubspot_owner_ids=['demo_owner_01'],
                title='Ciclo Demo B2B',
                general_objectives=['Refuerzo comercial B2B.'],
                specific_objectives=['Validacion de necesidades del cliente.']
            )
            self.assertEqual(len(reports), 1)
            rep = reports[0]

            stmt = select(TrainingSimulationPrompt).where(TrainingSimulationPrompt.training_report_id == rep.training_report_id)
            res = await db.execute(stmt)
            prompts = res.scalars().all()
            self.assertEqual(len(prompts), 4)

            for p in prompts:
                txt = p.prompt_text.lower()
                self.assertNotIn('boston medical', txt)
                self.assertNotIn('salud sexual', txt)
                self.assertNotIn('paciente', txt)
                self.assertTrue('cliente simulado' in txt or 'cliente' in txt)

    async def test_06_boston_manual_cycle_prompts_retain_clinical_context(self):
        async with self.async_session() as db:
            reports = await PersonalizedTrainingService.create_manual_cycles(
                db=db,
                hubspot_owner_ids=['boston_owner_01'],
                title='Ciclo Clinico Boston',
                general_objectives=['Protocolo clinico de atencion.'],
                specific_objectives=['Gestion de dudas del paciente.']
            )
            self.assertEqual(len(reports), 1)
            rep = reports[0]

            stmt = select(TrainingSimulationPrompt).where(TrainingSimulationPrompt.training_report_id == rep.training_report_id)
            res = await db.execute(stmt)
            prompts = res.scalars().all()
            self.assertEqual(len(prompts), 4)

            self.assertIn('boston medical group', prompts[0].prompt_text.lower())
            for p in prompts:
                p_lower = p.prompt_text.lower()
                self.assertIn('paciente', p_lower)

    async def test_07_list_agent_settings_for_manual_returns_disabled_agents(self):
        res = await self.client.get(
            '/bm/training/admin/settings?company_id=7&for_manual=true',
            headers={'Authorization': f'Bearer {self.tok_super}'}
        )
        self.assertEqual(res.status_code, 200)
        items = res.json()
        demo_owners = [i['hubspot_owner_id'] for i in items]
        self.assertIn('demo_owner_01', demo_owners)


    async def test_08_post_admin_generate_manual_explicit_demo_agent(self):
        from unittest.mock import MagicMock
        mock_rep = MagicMock()
        mock_rep.status = 'completed'
        mock_rep.error_message = None

        with patch.object(
            PersonalizedTrainingService,
            'generate_report_for_agent',
            new=AsyncMock(return_value=mock_rep)
        ) as mock_gen:
            res = await self.client.post(
                '/bm/training/admin/generate',
                json={
                    'hubspot_owner_ids': ['demo_owner_01'],
                    'force_regenerate': True
                },
                headers={'Authorization': f'Bearer {self.tok_demo_admin}'}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data['agents_total'], 1)
            self.assertEqual(data['triggered_by'], 'manual')
            mock_gen.assert_called_once()
            called_owner = mock_gen.call_args.kwargs.get('hubspot_owner_id')
            self.assertEqual(called_owner, 'demo_owner_01')

    async def test_09_post_admin_generate_other_company_forbidden_for_demo(self):
        res = await self.client.post(
            '/bm/training/admin/generate',
            json={
                'hubspot_owner_ids': ['demo_owner_01'],
                'force_regenerate': True
            },
            headers={'Authorization': f'Bearer {self.tok_boston_admin}'}
        )
        self.assertEqual(res.status_code, 403)

    async def test_10_settings_default_preserves_previous_behavior_without_for_manual(self):
        res = await self.client.get(
            '/bm/training/admin/settings?company_id=7&is_enabled=true',
            headers={'Authorization': f'Bearer {self.tok_super}'}
        )
        self.assertEqual(res.status_code, 200)
        items = res.json()
        demo_owners = [i['hubspot_owner_id'] for i in items]
        self.assertNotIn('demo_owner_01', demo_owners)


if __name__ == '__main__':
    unittest.main()
