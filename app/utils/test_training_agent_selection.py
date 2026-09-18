"""
Unit tests for Training Hub agent selection and scheduler decoupling.
Verifies:
1. Model defaults for include_in_scheduler = True.
2. get_agent_settings organizational enrichment (service, team, company).
3. Filtering by is_enabled and include_in_scheduler in get_agent_settings.
4. Single agent update of include_in_scheduler.
5. Bulk update of include_in_scheduler (bulk_update_scheduler_settings).
6. Scheduler pass (triggered_by='scheduler') strictly respects include_in_scheduler == True.
7. Manual pass (triggered_by='manual') respects explicitly selected agents regardless of include_in_scheduler.
8. get_agent_overview exposes organizational and scheduler fields.
"""
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# SQLite compatibility shims for PostgreSQL JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingRun,
    TrainingAgentReport,
)
from app.services.personalized_training_service import PersonalizedTrainingService


class TestTrainingAgentSelection(unittest.IsolatedAsyncioTestCase):
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

        async with self.async_session() as db:
            c1 = Company(company_id=1, company_name="Empresa Principal", company_key="main-co", is_active=True, is_demo=False)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=10, company_id=1, service_key="atencion-cliente", service_name="Atención al Cliente", is_active=True)
            db.add(s1)
            await db.flush()

            t1 = Team(team_id=100, company_id=1, service_id=10, team_name="Equipo Mañana", is_active=True)
            db.add(t1)
            await db.flush()

            # Create test agents in bm_users
            u1 = User(
                user_id=1,
                company_id=1,
                primary_service_id=10,
                primary_team_id=100,
                username="agente1",
                email="agente1@example.com",
                name="Agente Uno",
                role="agent",
                hubspot_owner_id="1001",
                agent_initials="AU",
                password_hash="hash",
                is_active=True
            )
            u2 = User(
                user_id=2,
                company_id=1,
                primary_service_id=10,
                primary_team_id=100,
                username="agente2",
                email="agente2@example.com",
                name="Agente Dos",
                role="agent",
                hubspot_owner_id="1002",
                agent_initials="AD",
                password_hash="hash",
                is_active=True
            )
            u3 = User(
                user_id=3,
                company_id=1,
                primary_service_id=10,
                primary_team_id=100,
                username="agente3",
                email="agente3@example.com",
                name="Agente Tres",
                role="agent",
                hubspot_owner_id="1003",
                agent_initials="AT",
                password_hash="hash",
                is_active=True
            )
            db.add_all([u1, u2, u3])

            # Settings:
            # u1: is_enabled=True, include_in_scheduler=True
            # u2: is_enabled=True, include_in_scheduler=False (Active for cycles, but excluded from auto scheduler)
            # u3: is_enabled=False, include_in_scheduler=True (Inactive agent)
            s_u1 = TrainingAgentSetting(
                hubspot_owner_id="1001",
                agent_name="Agente Uno",
                agent_initials="AU",
                is_enabled=True,
                include_in_scheduler=True,
                company_id=1
            )
            s_u2 = TrainingAgentSetting(
                hubspot_owner_id="1002",
                agent_name="Agente Dos",
                agent_initials="AD",
                is_enabled=True,
                include_in_scheduler=False,
                company_id=1
            )
            s_u3 = TrainingAgentSetting(
                hubspot_owner_id="1003",
                agent_name="Agente Tres",
                agent_initials="AT",
                is_enabled=False,
                include_in_scheduler=True,
                company_id=1
            )
            db.add_all([s_u1, s_u2, s_u3])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_model_default_include_in_scheduler(self):
        """Verify that a new TrainingAgentSetting defaults include_in_scheduler to True."""
        async with self.async_session() as db:
            new_s = TrainingAgentSetting(
                hubspot_owner_id="9999",
                agent_name="Nuevo Agente",
                agent_initials="NA",
                is_enabled=True,
                company_id=1
            )
            db.add(new_s)
            await db.commit()
            await db.refresh(new_s)
            self.assertTrue(new_s.include_in_scheduler)

    async def test_get_agent_settings_organizational_enrichment(self):
        """Verify that get_agent_settings enriches settings with service, team, and company data."""
        async with self.async_session() as db:
            settings = await PersonalizedTrainingService.get_agent_settings(db, company_ids=[1])
            self.assertEqual(len(settings), 3)
            s1 = next(s for s in settings if s.hubspot_owner_id == "1001")
            self.assertEqual(s1.service_id, 10)
            self.assertEqual(s1.service_name, "Atención al Cliente")
            self.assertEqual(s1.team_id, 100)
            self.assertEqual(s1.team_name, "Equipo Mañana")
            self.assertEqual(s1.company_name, "Empresa Principal")
            self.assertTrue(s1.is_enabled)
            self.assertTrue(s1.include_in_scheduler)

    async def test_get_agent_settings_filtering(self):
        """Verify filtering by is_enabled and include_in_scheduler."""
        async with self.async_session() as db:
            # Filter is_enabled=True: should return u1 and u2
            active = await PersonalizedTrainingService.get_agent_settings(db, is_enabled=True)
            active_ids = [s.hubspot_owner_id for s in active]
            self.assertIn("1001", active_ids)
            self.assertIn("1002", active_ids)
            self.assertNotIn("1003", active_ids)

            # Filter include_in_scheduler=True: should return u1 and u3
            scheduled = await PersonalizedTrainingService.get_agent_settings(db, include_in_scheduler=True)
            scheduled_ids = [s.hubspot_owner_id for s in scheduled]
            self.assertIn("1001", scheduled_ids)
            self.assertNotIn("1002", scheduled_ids)
            self.assertIn("1003", scheduled_ids)

            # Filter is_enabled=True AND include_in_scheduler=True: should return only u1
            active_scheduled = await PersonalizedTrainingService.get_agent_settings(
                db, is_enabled=True, include_in_scheduler=True
            )
            self.assertEqual(len(active_scheduled), 1)
            self.assertEqual(active_scheduled[0].hubspot_owner_id, "1001")

    async def test_update_agent_setting_individual(self):
        """Verify updating include_in_scheduler on a single agent setting."""
        async with self.async_session() as db:
            # Change u2 include_in_scheduler to True
            updated = await PersonalizedTrainingService.update_agent_setting(
                db, hubspot_owner_id="1002", include_in_scheduler=True
            )
            self.assertIsNotNone(updated)
            self.assertTrue(updated.include_in_scheduler)
            self.assertEqual(updated.service_name, "Atención al Cliente")

            # Verify in DB
            stmt = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == "1002")
            res = await db.execute(stmt)
            persisted = res.scalars().first()
            self.assertTrue(persisted.include_in_scheduler)

    async def test_bulk_update_scheduler_settings(self):
        """Verify bulk update of include_in_scheduler for multiple agents."""
        async with self.async_session() as db:
            count = await PersonalizedTrainingService.bulk_update_scheduler_settings(
                db,
                hubspot_owner_ids=["1001", "1003"],
                include_in_scheduler=False
            )
            self.assertEqual(count, 2)

            # Verify both are now False
            stmt = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id.in_(["1001", "1003"]))
            res = await db.execute(stmt)
            for s in res.scalars().all():
                self.assertFalse(s.include_in_scheduler)

    async def test_scheduler_pass_respects_include_in_scheduler(self):
        """
        Verify that automatic scheduler pass (triggered_by='scheduler') strictly excludes
        agents where include_in_scheduler == False, even if is_enabled == True.
        """
        async with self.async_session() as db:
            with patch.object(
                PersonalizedTrainingService, "generate_report_for_agent", new_callable=AsyncMock
            ) as mock_gen:
                mock_rep = TrainingAgentReport(
                    training_report_id=999,
                    hubspot_owner_id="1001",
                    agent_name="Agente Uno",
                    status="completed"
                )
                mock_gen.return_value = mock_rep

                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    triggered_by="scheduler",
                    company_ids=[1]
                )

                # Total agents included should be 1 (only u1: is_enabled=True and include_in_scheduler=True)
                # u2 is excluded because include_in_scheduler=False
                # u3 is excluded because is_enabled=False
                self.assertEqual(run.agents_total, 1)
                self.assertEqual(mock_gen.call_count, 1)
                call_args = mock_gen.call_args[1]
                self.assertEqual(call_args["hubspot_owner_id"], "1001")

    async def test_manual_pass_respects_selected_agents(self):
        """
        Verify that manual pass (triggered_by='manual') allows running for agents
        explicitly selected in hubspot_owner_ids, even if include_in_scheduler is False.
        """
        async with self.async_session() as db:
            with patch.object(
                PersonalizedTrainingService, "generate_report_for_agent", new_callable=AsyncMock
            ) as mock_gen:
                mock_rep = TrainingAgentReport(
                    training_report_id=888,
                    hubspot_owner_id="1002",
                    agent_name="Agente Dos",
                    status="completed"
                )
                mock_gen.return_value = mock_rep

                # u2 has include_in_scheduler=False, but is_enabled=True
                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    hubspot_owner_ids=["1002"],
                    triggered_by="manual"
                )

                self.assertEqual(run.agents_total, 1)
                self.assertEqual(mock_gen.call_count, 1)
                self.assertEqual(mock_gen.call_args[1]["hubspot_owner_id"], "1002")

    async def test_agent_overview_includes_organizational_and_scheduler_fields(self):
        """Verify that get_agent_overview includes organizational and scheduler attributes."""
        async with self.async_session() as db:
            overview = await PersonalizedTrainingService.get_agent_overview(db, company_ids=[1])
            self.assertEqual(len(overview), 3)
            item1 = next(it for it in overview if it["hubspot_owner_id"] == "1001")
            self.assertEqual(item1["service_id"], 10)
            self.assertEqual(item1["service_name"], "Atención al Cliente")
            self.assertEqual(item1["team_id"], 100)
            self.assertEqual(item1["team_name"], "Equipo Mañana")
            self.assertEqual(item1["company_name"], "Empresa Principal")
            self.assertTrue(item1["is_enabled"])
            self.assertTrue(item1["include_in_scheduler"])

            item2 = next(it for it in overview if it["hubspot_owner_id"] == "1002")
            self.assertTrue(item2["is_enabled"])
            self.assertFalse(item2["include_in_scheduler"])

    async def test_router_list_agent_settings(self):
        """Verify list_agent_settings router endpoint with new query filters."""
        from app.routers.personalized_training import list_agent_settings
        from app.core.tenant_context import TenantContext
        from app.core.roles import InternalRole

        ctx = TenantContext(
            user_id=99,
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1]
        )

        async with self.async_session() as db:
            # Test filter by is_enabled=True and include_in_scheduler=True
            res = await list_agent_settings(
                context=ctx,
                company_id=1,
                service_id=10,
                team_id=100,
                is_enabled=True,
                include_in_scheduler=True,
                db=db
            )
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].hubspot_owner_id, "1001")
            self.assertEqual(res[0].service_name, "Atención al Cliente")
            self.assertEqual(res[0].team_name, "Equipo Mañana")
            self.assertTrue(res[0].is_enabled)
            self.assertTrue(res[0].include_in_scheduler)

    async def test_router_bulk_scheduler_settings(self):
        """Verify bulk_update_scheduler_settings router endpoint."""
        from app.routers.personalized_training import bulk_update_scheduler_settings
        from app.schemas.personalized_training import BulkSchedulerAgentUpdate
        from app.core.tenant_context import TenantContext
        from app.core.roles import InternalRole

        ctx = TenantContext(
            user_id=99,
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1]
        )

        async with self.async_session() as db:
            payload = BulkSchedulerAgentUpdate(
                hubspot_owner_ids=["1002"],
                include_in_scheduler=True
            )
            res = await bulk_update_scheduler_settings(payload=payload, context=ctx, db=db)
            self.assertEqual(res["updated_count"], 1)
            self.assertTrue(res["include_in_scheduler"])
            self.assertEqual(res["hubspot_owner_ids"], ["1002"])


if __name__ == "__main__":
    unittest.main()
