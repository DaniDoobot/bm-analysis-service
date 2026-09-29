# -*- coding: utf-8 -*-
"""
Tests for Trainer Agent Settings organizational context resolution and company filtering.
Covers the 16 requirements specified in the audit and fix task:
1. primary_service present -> respected
2. primary_service NULL + single N:M service -> uses N:M
3. primary_service NULL + single team with service -> correct fallback
4. multiple services without primary -> None (no arbitrary pick)
5. primary_team present -> respected
6. primary_team NULL + single N:M team -> uses N:M
7. multiple teams without primary -> None (no arbitrary pick)
8. Bryan -> Front + Equipo Front Principal
9. Cristina -> Front + Equipo Front Principal
10. Eugenia -> Front + Equipo Front Principal
11. Ludmila -> Experiencia de Paciente + Equipo ExpPa Principal
12. company_id=1 returns exclusively Boston Medical
13. company_id=7 returns exclusively Empresa Demo
14. superadmin + company_id=1 respects filter
15. no cross-company leaks
16. no N+1 query explosion (batch resolution)
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

DB_FILE = 'test_agent_settings_resolution.db'
os.environ['DATABASE_URL'] = f'sqlite+aiosqlite:///{DB_FILE}'
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, 'sqlite')
def compile_jsonb_sqlite(type_, compiler, **kw):
    return 'JSON'

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, event

from app.db import Base, get_engine
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting
from app.services.personalized_training_service import PersonalizedTrainingService
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.routers.personalized_training import list_agent_settings


class TestTrainerAgentSettingsResolution(unittest.IsolatedAsyncioTestCase):

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
            # 1. Companies
            self.c_boston = Company(company_id=1, company_name='Boston Medical Group', company_key='boston', is_active=True)
            self.c_demo = Company(company_id=7, company_name='Empresa Demo', company_key='demo', is_active=True, is_demo=True)
            db.add_all([self.c_boston, self.c_demo])

            # 2. Services for Boston Medical
            self.s_front = Service(service_id=1, company_id=1, service_name='Front', service_key='front', is_active=True)
            self.s_exppa = Service(service_id=2, company_id=1, service_name='Experiencia de Paciente', service_key='experiencia_paciente', is_active=True)
            self.s_ascom = Service(service_id=3, company_id=1, service_name='Asesores Comerciales', service_key='asesorias', is_active=True)
            # Service for Demo
            self.s_demo = Service(service_id=10, company_id=7, service_name='Demo Front', service_key='demo_front', is_active=True)
            db.add_all([self.s_front, self.s_exppa, self.s_ascom, self.s_demo])

            # 3. Teams for Boston Medical
            self.t_front = Team(team_id=1, company_id=1, service_id=1, team_name='Equipo Front Principal')
            self.t_exppa = Team(team_id=2, company_id=1, service_id=2, team_name='Equipo ExpPa Principal')
            self.t_ascom = Team(team_id=3, company_id=1, service_id=3, team_name='Equipo Comerciales Principal')
            # Team for Demo
            self.t_demo = Team(team_id=16, company_id=7, service_id=10, team_name='Equipo Demo Front')
            db.add_all([self.t_front, self.t_exppa, self.t_ascom, self.t_demo])

            # 4. Users
            # Bryan Herrera: primary_service=None, primary_team=None, NM service=1, NM team=1
            self.u_bryan = User(user_id=196, company_id=1, username='bherrera', email='bherrera@boston.com', name='Bryan Herrera', role='agent', hubspot_owner_id='33013277', is_active=True, password_hash='x')
            # Cristina Montenegro: primary_service=None, primary_team=None, NM service=1, NM team=1
            self.u_cristina = User(user_id=197, company_id=1, username='cmontenegro', email='cmontenegro@boston.com', name='Cristina Montenegro', role='agent', hubspot_owner_id='33013276', is_active=True, password_hash='x')
            # Eugenia Carreño: primary_service=None, primary_team=None, NM service=1, NM team=1
            self.u_eugenia = User(user_id=157, company_id=1, username='ecarreno', email='ecarreno@boston.com', name='Eugenia Carreño', role='agent', hubspot_owner_id='1375831791', is_active=True, password_hash='x')
            # Ludmila Nascarella: primary_service=2, primary_team=None, NM team=2
            self.u_ludmila = User(user_id=212, company_id=1, username='lnascarella', email='lnascarella@boston.com', name='Ludmila Nascarella', role='agent', hubspot_owner_id='36333511', is_active=True, primary_service_id=2, password_hash='x')

            # Special case agents:
            # Agent with primary_team present
            self.u_has_prim_team = User(user_id=301, company_id=1, username='agent_prim_team', email='prim_team@boston.com', name='Agent Prim Team', role='agent', hubspot_owner_id='hs_prim_team', is_active=True, primary_team_id=1, password_hash='x')
            # Agent with no service association, but has single team association pointing to service 3
            self.u_fallback_team_svc = User(user_id=302, company_id=1, username='agent_fb_team', email='fb_team@boston.com', name='Agent Fallback Team', role='agent', hubspot_owner_id='hs_fb_team', is_active=True, password_hash='x')
            # Agent with multiple services and no primary
            self.u_multi_services = User(user_id=303, company_id=1, username='agent_multi_svc', email='multi_svc@boston.com', name='Agent Multi Svc', role='agent', hubspot_owner_id='hs_multi_svc', is_active=True, password_hash='x')
            # Agent with multiple teams and no primary
            self.u_multi_teams = User(user_id=304, company_id=1, username='agent_multi_team', email='multi_team@boston.com', name='Agent Multi Team', role='agent', hubspot_owner_id='hs_multi_team', is_active=True, password_hash='x')

            # Demo agent
            self.u_demo_agent = User(user_id=401, company_id=7, username='demo_agent', email='demo_agent@demo.com', name='Demo Agent 1', role='agent', hubspot_owner_id='hs_demo_01', is_active=True, primary_service_id=10, primary_team_id=16, password_hash='x')

            db.add_all([
                self.u_bryan, self.u_cristina, self.u_eugenia, self.u_ludmila,
                self.u_has_prim_team, self.u_fallback_team_svc, self.u_multi_services, self.u_multi_teams,
                self.u_demo_agent
            ])
            await db.flush()

            # Associations
            # Bryan: UserService 1, AgentTeam 1
            db.add(UserServiceAssociation(user_id=196, service_id=1))
            db.add(AgentTeamAssociation(user_id=196, team_id=1))

            # Cristina: UserService 1, AgentTeam 1
            db.add(UserServiceAssociation(user_id=197, service_id=1))
            db.add(AgentTeamAssociation(user_id=197, team_id=1))

            # Eugenia: UserService 1, AgentTeam 1
            db.add(UserServiceAssociation(user_id=157, service_id=1))
            db.add(AgentTeamAssociation(user_id=157, team_id=1))

            # Ludmila: UserService 2, AgentTeam 2
            db.add(UserServiceAssociation(user_id=212, service_id=2))
            db.add(AgentTeamAssociation(user_id=212, team_id=2))

            # u_fallback_team_svc: only AgentTeam 3 (no UserService)
            db.add(AgentTeamAssociation(user_id=302, team_id=3))

            # u_multi_services: UserService 1 and UserService 2
            db.add(UserServiceAssociation(user_id=303, service_id=1))
            db.add(UserServiceAssociation(user_id=303, service_id=2))

            # u_multi_teams: AgentTeam 1 and AgentTeam 2
            db.add(AgentTeamAssociation(user_id=304, team_id=1))
            db.add(AgentTeamAssociation(user_id=304, team_id=2))

            # Demo agent: UserService 10, AgentTeam 16
            db.add(UserServiceAssociation(user_id=401, service_id=10))
            db.add(AgentTeamAssociation(user_id=401, team_id=16))

            # TrainingAgentSettings
            self.set_bryan = TrainingAgentSetting(setting_id=1, company_id=1, hubspot_owner_id='33013277', agent_name='Bryan Herrera', agent_initials='BH', is_enabled=True)
            self.set_cristina = TrainingAgentSetting(setting_id=2, company_id=1, hubspot_owner_id='33013276', agent_name='Cristina Montenegro', agent_initials='CM', is_enabled=True)
            self.set_eugenia = TrainingAgentSetting(setting_id=3, company_id=1, hubspot_owner_id='1375831791', agent_name='Eugenia Carreño', agent_initials='EC', is_enabled=True)
            self.set_ludmila = TrainingAgentSetting(setting_id=4, company_id=1, hubspot_owner_id='36333511', agent_name='Ludmila Nascarella', agent_initials='LN', is_enabled=True)
            self.set_prim_team = TrainingAgentSetting(setting_id=5, company_id=1, hubspot_owner_id='hs_prim_team', agent_name='Agent Prim Team', agent_initials='PT', is_enabled=True)
            self.set_fb_team = TrainingAgentSetting(setting_id=6, company_id=1, hubspot_owner_id='hs_fb_team', agent_name='Agent Fallback Team', agent_initials='FT', is_enabled=True)
            self.set_multi_svc = TrainingAgentSetting(setting_id=7, company_id=1, hubspot_owner_id='hs_multi_svc', agent_name='Agent Multi Svc', agent_initials='MS', is_enabled=True)
            self.set_multi_team = TrainingAgentSetting(setting_id=8, company_id=1, hubspot_owner_id='hs_multi_team', agent_name='Agent Multi Team', agent_initials='MT', is_enabled=True)
            self.set_demo = TrainingAgentSetting(setting_id=9, company_id=7, hubspot_owner_id='hs_demo_01', agent_name='Demo Agent 1', agent_initials='DA', is_enabled=True)

            db.add_all([
                self.set_bryan, self.set_cristina, self.set_eugenia, self.set_ludmila,
                self.set_prim_team, self.set_fb_team, self.set_multi_svc, self.set_multi_team,
                self.set_demo
            ])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        if os.path.exists(DB_FILE):
            try:
                os.remove(DB_FILE)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Tests for Service Resolution
    # ─────────────────────────────────────────────────────────────────────────

    async def test_01_primary_service_present_is_respected(self):
        """1. primary_service presente -> se respeta."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['36333511'])
            self.assertEqual(len(res), 1)
            ludmila = res[0]
            self.assertEqual(ludmila.service_id, 2)
            self.assertEqual(ludmila.service_name, 'Experiencia de Paciente')

    async def test_02_primary_service_null_uses_nm_service(self):
        """2. primary_service NULL + un servicio N:M -> usa N:M."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['33013277'])
            self.assertEqual(len(res), 1)
            bryan = res[0]
            self.assertEqual(bryan.service_id, 1)
            self.assertEqual(bryan.service_name, 'Front')

    async def test_03_primary_service_null_single_team_service_fallback(self):
        """3. primary_service NULL + equipo único con service -> fallback correcto."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['hs_fb_team'])
            self.assertEqual(len(res), 1)
            agent = res[0]
            # Associated only to team 3 (Asesores Comerciales)
            self.assertEqual(agent.service_id, 3)
            self.assertEqual(agent.service_name, 'Asesores Comerciales')
            self.assertEqual(agent.team_id, 3)
            self.assertEqual(agent.team_name, 'Equipo Comerciales Principal')

    async def test_04_multiple_services_without_primary_returns_none(self):
        """4. múltiples servicios sin primary -> no elegir arbitrariamente (None)."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['hs_multi_svc'])
            self.assertEqual(len(res), 1)
            agent = res[0]
            self.assertIsNone(agent.service_id)
            self.assertIsNone(agent.service_name)

    # ─────────────────────────────────────────────────────────────────────────
    # Tests for Team Resolution
    # ─────────────────────────────────────────────────────────────────────────

    async def test_05_primary_team_present_is_respected(self):
        """5. primary_team presente -> se respeta."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['hs_prim_team'])
            self.assertEqual(len(res), 1)
            agent = res[0]
            self.assertEqual(agent.team_id, 1)
            self.assertEqual(agent.team_name, 'Equipo Front Principal')

    async def test_06_primary_team_null_uses_nm_team(self):
        """6. primary_team NULL + un equipo N:M -> usa N:M."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['33013277'])
            self.assertEqual(len(res), 1)
            bryan = res[0]
            self.assertEqual(bryan.team_id, 1)
            self.assertEqual(bryan.team_name, 'Equipo Front Principal')

    async def test_07_multiple_teams_without_primary_returns_none(self):
        """7. múltiples equipos sin primary -> no elegir arbitrariamente (None)."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['hs_multi_team'])
            self.assertEqual(len(res), 1)
            agent = res[0]
            self.assertIsNone(agent.team_id)
            self.assertIsNone(agent.team_name)

    # ─────────────────────────────────────────────────────────────────────────
    # Tests for Specific Boston Medical Agents
    # ─────────────────────────────────────────────────────────────────────────

    async def test_08_bryan_returns_front_and_equipo_front(self):
        """8. Bryan devuelve Front + Equipo Front Principal."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['33013277'])
            self.assertEqual(len(res), 1)
            a = res[0]
            self.assertEqual(a.service_id, 1)
            self.assertEqual(a.service_name, 'Front')
            self.assertEqual(a.team_id, 1)
            self.assertEqual(a.team_name, 'Equipo Front Principal')

    async def test_09_cristina_returns_front_and_equipo_front(self):
        """9. Cristina devuelve Front + Equipo Front Principal."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['33013276'])
            self.assertEqual(len(res), 1)
            a = res[0]
            self.assertEqual(a.service_id, 1)
            self.assertEqual(a.service_name, 'Front')
            self.assertEqual(a.team_id, 1)
            self.assertEqual(a.team_name, 'Equipo Front Principal')

    async def test_10_eugenia_returns_front_and_equipo_front(self):
        """10. Eugenia devuelve Front + Equipo Front Principal."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['1375831791'])
            self.assertEqual(len(res), 1)
            a = res[0]
            self.assertEqual(a.service_id, 1)
            self.assertEqual(a.service_name, 'Front')
            self.assertEqual(a.team_id, 1)
            self.assertEqual(a.team_name, 'Equipo Front Principal')

    async def test_11_ludmila_returns_experiencia_paciente_and_equipo_exppa(self):
        """11. Ludmila sigue devolviendo Experiencia de Paciente y Equipo ExpPa Principal."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1], allowed_agent_ids=['36333511'])
            self.assertEqual(len(res), 1)
            a = res[0]
            self.assertEqual(a.service_id, 2)
            self.assertEqual(a.service_name, 'Experiencia de Paciente')
            self.assertEqual(a.team_id, 2)
            self.assertEqual(a.team_name, 'Equipo ExpPa Principal')

    # ─────────────────────────────────────────────────────────────────────────
    # Tests for Company Filtering and Scoping
    # ─────────────────────────────────────────────────────────────────────────

    async def test_12_company_id_1_returns_only_boston(self):
        """12. company_id=1 devuelve solo Boston."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1])
            self.assertTrue(len(res) > 0)
            for item in res:
                self.assertEqual(item.company_id, 1)
            owner_ids = [item.hubspot_owner_id for item in res]
            self.assertIn('33013277', owner_ids)
            self.assertNotIn('hs_demo_01', owner_ids)

    async def test_13_company_id_7_returns_only_demo(self):
        """13. company_id=7 devuelve solo Demo."""
        async with self.async_session() as db:
            res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[7])
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].hubspot_owner_id, 'hs_demo_01')
            self.assertEqual(res[0].company_id, 7)
            self.assertEqual(res[0].service_name, 'Demo Front')
            self.assertEqual(res[0].team_name, 'Equipo Demo Front')

    async def test_14_superadmin_with_company_id_1_respects_filter(self):
        """14. superadmin + company_id=1 respeta filtro en endpoint list_agent_settings."""
        super_ctx = TenantContext(
            user_id=1, username='superadmin', user_email='super@example.com',
            raw_role='admin', normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True, company_id=None,
            allowed_company_ids=[1, 7]
        )
        async with self.async_session() as db:
            res = await list_agent_settings(
                context=super_ctx,
                company_id=1,
                db=db
            )
            self.assertTrue(len(res) > 0)
            for item in res:
                self.assertEqual(item.company_id, 1)
            owner_ids = [item.hubspot_owner_id for item in res]
            self.assertNotIn('hs_demo_01', owner_ids)

    async def test_15_no_cross_company_leaks(self):
        """15. no cross-company: verify Boston company_admin cannot see Demo and company_ids=1 has zero demo agents."""
        boston_admin_ctx = TenantContext(
            user_id=3, username='boston_admin', user_email='admin@boston.com',
            raw_role='company_admin', normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False, company_id=1,
            allowed_company_ids=[1]
        )
        async with self.async_session() as db:
            res = await list_agent_settings(
                context=boston_admin_ctx,
                db=db
            )
            for item in res:
                self.assertEqual(item.company_id, 1)
            owner_ids = [item.hubspot_owner_id for item in res]
            self.assertNotIn('hs_demo_01', owner_ids)

    async def test_16_no_n_plus_one_batch_resolution(self):
        """16. no N+1 significativo: resolution executes batch queries, not per-agent queries."""
        query_count = 0

        # We attach a listener to count SQL statements executed during enrichment
        def count_queries(conn, cursor, statement, parameters, context, executemany):
            nonlocal query_count
            query_count += 1

        async with self.async_session() as db:
            sync_engine = self.engine.sync_engine
            event.listen(sync_engine, "before_cursor_execute", count_queries)
            try:
                # Query all 8 Boston agents
                res = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1])
                self.assertEqual(len(res), 8)
                # Ensure queries executed is small and bounded (<= 12 total queries for setup + 8 agents)
                self.assertLessEqual(query_count, 12, f"Too many queries ({query_count}), possible N+1 query issue")
            finally:
                event.remove(sync_engine, "before_cursor_execute", count_queries)


if __name__ == '__main__':
    unittest.main()
