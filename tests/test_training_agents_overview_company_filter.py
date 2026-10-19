import os
import sys
import unittest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///training_overview_company_filter_test.db"

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import Base
from app.dependencies import get_db
from app.main import app
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting
from app.utils.security import create_access_token


class TestTrainingAgentsOverviewCompanyFilter(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine, class_=AsyncSession, expire_on_commit=False
        )

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async def override_get_db():
            async with self.session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

        async with self.session_factory() as db:
            # Companies
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            c2 = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_active=True)
            db.add_all([c1, c2])
            await db.flush()

            # Services
            s1 = Service(service_id=1, service_name="Front", service_key="front", company_id=1)
            s2 = Service(service_id=2, service_name="EXPAC", service_key="expac", company_id=1)
            s7 = Service(service_id=10, service_name="Atención al Cliente", service_key="atc-demo", company_id=7)
            db.add_all([s1, s2, s7])
            await db.flush()

            # Teams
            t1 = Team(team_id=1, team_name="Equipo Front", company_id=1, service_id=1)
            t2 = Team(team_id=2, team_name="Equipo EXPAC", company_id=1, service_id=2)
            t7 = Team(team_id=10, team_name="Equipo Demo", company_id=7, service_id=10)
            db.add_all([t1, t2, t7])
            await db.flush()

            # Admin users
            u_super = User(
                user_id=1,
                username="superadmin",
                email="super@doobot.ai",
                role="super_admin",
                company_id=1,
                is_active=True,
                password_hash="x",
            )
            u_c1_admin = User(
                user_id=2,
                username="admin_c1",
                email="admin@boston.es",
                role="company_admin",
                company_id=1,
                is_active=True,
                password_hash="x",
            )
            u_c2_admin = User(
                user_id=3,
                username="admin_c7",
                email="admin@demo.es",
                role="company_admin",
                company_id=7,
                is_active=True,
                password_hash="x",
            )

            # Agents
            ag_c1_s1 = User(
                user_id=10,
                username="cmontenegro",
                name="Cristina Montenegro",
                email="cmontenegro@boston.es",
                role="agente",
                company_id=1,
                primary_service_id=1,
                primary_team_id=1,
                hubspot_owner_id="33013276",
                is_active=True,
                password_hash="x",
            )
            ag_c1_s2 = User(
                user_id=11,
                username="ag_expac",
                name="Agente Expac",
                email="expac@boston.es",
                role="agente",
                company_id=1,
                primary_service_id=2,
                primary_team_id=2,
                hubspot_owner_id="99001122",
                is_active=True,
                password_hash="x",
            )
            ag_c7_s10 = User(
                user_id=20,
                username="demo_ag1",
                name="Agente Demo 01",
                email="demo1@demo.es",
                role="agente",
                company_id=7,
                primary_service_id=10,
                primary_team_id=10,
                hubspot_owner_id="demo_owner_01",
                is_active=True,
                password_hash="x",
            )
            db.add_all([u_super, u_c1_admin, u_c2_admin, ag_c1_s1, ag_c1_s2, ag_c7_s10])
            await db.flush()

            db.add_all([
                UserServiceAssociation(user_id=10, service_id=1),
                UserTeamAssociation(user_id=10, team_id=1),
                UserServiceAssociation(user_id=11, service_id=2),
                UserTeamAssociation(user_id=11, team_id=2),
                UserServiceAssociation(user_id=20, service_id=10),
                UserTeamAssociation(user_id=20, team_id=10),
            ])

            db.add_all([
                TrainingAgentSetting(
                    hubspot_owner_id="33013276",
                    agent_name="Cristina Montenegro",
                    agent_initials="CM",
                    is_enabled=True,
                    company_id=1,
                ),
                TrainingAgentSetting(
                    hubspot_owner_id="99001122",
                    agent_name="Agente Expac",
                    agent_initials="AE",
                    is_enabled=True,
                    company_id=1,
                ),
                TrainingAgentSetting(
                    hubspot_owner_id="demo_owner_01",
                    agent_name="Agente Demo 01",
                    agent_initials="AD",
                    is_enabled=True,
                    company_id=7,
                ),
            ])
            await db.commit()

        self.token_super = create_access_token({"sub": "superadmin", "user_id": 1, "company_id": 1, "role": "super_admin"})
        self.token_c1_admin = create_access_token({"sub": "admin_c1", "user_id": 2, "company_id": 1, "role": "company_admin"})
        self.token_c7_admin = create_access_token({"sub": "admin_c7", "user_id": 3, "company_id": 7, "role": "company_admin"})

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.pop(get_db, None)
        await self.engine.dispose()
        if os.path.exists("training_overview_company_filter_test.db"):
            try:
                os.remove("training_overview_company_filter_test.db")
            except Exception:
                pass

    async def test_1_and_2_and_5_without_company_id_returns_all_in_scope_with_company_id_and_name(self):
        """1, 2, 5: Without company_id, returns all agents in scope and every item includes company_id and company_name."""
        res = await self.client.get(
            "/bm/training/admin/agents-overview",
            headers={"Authorization": f"Bearer {self.token_super}"},
        )
        self.assertEqual(res.status_code, 200)
        items = res.json()
        self.assertEqual(len(items), 3)

        by_owner = {it["hubspot_owner_id"]: it for it in items}
        self.assertEqual(by_owner["33013276"]["company_id"], 1)
        self.assertEqual(by_owner["33013276"]["company_name"], "Boston Medical")
        self.assertEqual(by_owner["99001122"]["company_id"], 1)
        self.assertEqual(by_owner["99001122"]["company_name"], "Boston Medical")
        self.assertEqual(by_owner["demo_owner_01"]["company_id"], 7)
        self.assertEqual(by_owner["demo_owner_01"]["company_name"], "Empresa Demo")

    async def test_3_filter_by_company_id(self):
        """3: Filtering by company_id returns only agents belonging to that company."""
        res_c1 = await self.client.get(
            "/bm/training/admin/agents-overview?company_id=1",
            headers={"Authorization": f"Bearer {self.token_super}"},
        )
        self.assertEqual(res_c1.status_code, 200)
        items_c1 = res_c1.json()
        self.assertEqual({it["hubspot_owner_id"] for it in items_c1}, {"33013276", "99001122"})
        for it in items_c1:
            self.assertEqual(it["company_id"], 1)
            self.assertEqual(it["company_name"], "Boston Medical")

        res_c7 = await self.client.get(
            "/bm/training/admin/agents-overview?company_id=7",
            headers={"Authorization": f"Bearer {self.token_super}"},
        )
        self.assertEqual(res_c7.status_code, 200)
        items_c7 = res_c7.json()
        self.assertEqual([it["hubspot_owner_id"] for it in items_c7], ["demo_owner_01"])
        self.assertEqual(items_c7[0]["company_id"], 7)
        self.assertEqual(items_c7[0]["company_name"], "Empresa Demo")

    async def test_4_combined_company_id_and_service_id(self):
        """4: Combining company_id and service_id applies AND logic."""
        res = await self.client.get(
            "/bm/training/admin/agents-overview?company_id=1&service_id=1",
            headers={"Authorization": f"Bearer {self.token_super}"},
        )
        self.assertEqual(res.status_code, 200)
        items = res.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["hubspot_owner_id"], "33013276")
        self.assertEqual(items[0]["company_id"], 1)
        self.assertEqual(items[0]["company_name"], "Boston Medical")
        self.assertEqual(items[0]["service_id"], 1)
        self.assertEqual(items[0]["service_name"], "Front")

    async def test_6_multitenant_isolation_and_nonexistent_company(self):
        """6: Company Admin cannot access another company's agents via company_id (403); non-existent company returns []."""
        # Company 1 admin without company_id -> only sees company 1 agents
        res_own = await self.client.get(
            "/bm/training/admin/agents-overview",
            headers={"Authorization": f"Bearer {self.token_c1_admin}"},
        )
        self.assertEqual(res_own.status_code, 200)
        self.assertEqual({it["hubspot_owner_id"] for it in res_own.json()}, {"33013276", "99001122"})

        # Company 1 admin requesting company_id=7 -> 403 Forbidden
        res_cross = await self.client.get(
            "/bm/training/admin/agents-overview?company_id=7",
            headers={"Authorization": f"Bearer {self.token_c1_admin}"},
        )
        self.assertEqual(res_cross.status_code, 403)

        # Company 7 admin requesting company_id=1 -> 403 Forbidden
        res_cross_2 = await self.client.get(
            "/bm/training/admin/agents-overview?company_id=1",
            headers={"Authorization": f"Bearer {self.token_c7_admin}"},
        )
        self.assertEqual(res_cross_2.status_code, 403)

        # Super admin requesting non-existent company_id=999 -> 200 []
        res_none = await self.client.get(
            "/bm/training/admin/agents-overview?company_id=999",
            headers={"Authorization": f"Bearer {self.token_super}"},
        )
        self.assertEqual(res_none.status_code, 200)
        self.assertEqual(res_none.json(), [])


if __name__ == "__main__":
    unittest.main()
