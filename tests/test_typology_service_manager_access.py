import unittest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select, or_
from httpx import AsyncClient, ASGITransport

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.typologies import Typology
from app.utils.security import create_access_token, hash_password
from app.main import app
from app.dependencies import get_db


class TestTypologyServiceManagerAccess(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_maker() as db:
            pwd = hash_password("Secret123!")

            # 1. Company
            self.c_boston = Company(
                company_id=1,
                company_name="Boston Medical",
                company_key="boston-medical",
                is_active=True,
            )
            db.add(self.c_boston)
            await db.flush()

            # 2. Services: Front (1) and EXPAC (2)
            self.s_front = Service(
                service_id=1,
                service_name="Front",
                service_key="front",
                company_id=1,
                is_active=True,
            )
            self.s_expac = Service(
                service_id=2,
                service_name="Experiencia de Paciente",
                service_key="experiencia_paciente",
                company_id=1,
                is_active=True,
            )
            db.add_all([self.s_front, self.s_expac])
            await db.flush()

            # 3. Users: Superadmin and Juanjo (service_manager EXPAC)
            self.u_super = User(
                user_id=1,
                username="superadmin",
                email="super@speechbm.com",
                role="super_admin",
                password_hash=pwd,
                is_active=True,
            )
            self.u_juanjo = User(
                user_id=5,
                username="jrodriguez",
                email="jrodriguez@boston.es",
                role="responsable_servicio",
                company_id=1,
                primary_service_id=2,
                password_hash=pwd,
                is_active=True,
            )
            db.add_all([self.u_super, self.u_juanjo])
            await db.flush()

            # 4. Typologies:
            # Front (service_id=1): one with company_id=1, one with company_id=None (legacy)
            self.t_front_1 = Typology(
                typology_id=1,
                company_id=1,
                service_id=1,
                typology_key="cita",
                typology_name="Cita",
                sort_order=10,
                is_active=True,
            )
            self.t_front_2 = Typology(
                typology_id=2,
                company_id=None,
                service_id=1,
                typology_key="confirmacion",
                typology_name="Confirmación",
                sort_order=20,
                is_active=True,
            )

            # EXPAC (service_id=2): one with company_id=1, one with company_id=None (legacy)
            self.t_expac_1 = Typology(
                typology_id=9,
                company_id=1,
                service_id=2,
                typology_key="seguimiento_encuesta",
                typology_name="Seguimiento Encuesta",
                sort_order=20,
                is_active=True,
            )
            self.t_expac_2 = Typology(
                typology_id=26,
                company_id=None,
                service_id=2,
                typology_key="cita_trat",
                typology_name="Cita Trat",
                sort_order=60,
                is_active=True,
            )
            db.add_all([self.t_front_1, self.t_front_2, self.t_expac_1, self.t_expac_2])
            await db.commit()

        self.t_super = create_access_token({"user_id": 1, "email": "super@speechbm.com"})
        self.t_juanjo = create_access_token({"user_id": 5, "email": "jrodriguez@boston.es"})

        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        await self.client.aclose()
        await self.engine.dispose()

    # ── A) Superadmin can query EXPAC typologies ─────────────────────────────
    async def test_superadmin_can_query_expac_typologies(self):
        """A) superadmin GET /bm/typologies?service_id=2 -> 200 -> devuelve tipologías EXPAC."""
        res = await self.client.get(
            "/bm/typologies?service_id=2",
            headers={"Authorization": f"Bearer {self.t_super}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        keys = [t["typology_key"] for t in data]
        self.assertIn("seguimiento_encuesta", keys)
        self.assertIn("cita_trat", keys)
        self.assertNotIn("cita", keys)
        self.assertNotIn("confirmacion", keys)

    # ── B) Service manager can query allowed EXPAC typologies ─────────────────
    async def test_service_manager_can_query_allowed_service_typologies(self):
        """B) service_manager company_id=1 con allowed_service_ids=[2] GET /bm/typologies?service_id=2 -> 200."""
        res = await self.client.get(
            "/bm/typologies?service_id=2",
            headers={"Authorization": f"Bearer {self.t_juanjo}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(len(data), 2)
        keys = [t["typology_key"] for t in data]
        self.assertIn("seguimiento_encuesta", keys)
        self.assertIn("cita_trat", keys)
        self.assertNotIn("cita", keys)
        self.assertNotIn("confirmacion", keys)

    # ── C) Service manager cannot access Front typologies ────────────────────
    async def test_service_manager_cannot_query_unauthorized_front_service(self):
        """C) service_manager con acceso solo a service_id=2 GET /bm/typologies?service_id=1 -> no obtiene Front."""
        res = await self.client.get(
            "/bm/typologies?service_id=1",
            headers={"Authorization": f"Bearer {self.t_juanjo}"},
        )
        # Should be rejected with 400 (cascade validation mismatch) or 403
        self.assertIn(res.status_code, [400, 403], res.text)

    # ── D) Preserves typologies with company_id=1 and company_id=None ────────
    async def test_typologies_with_company_id_1_and_null_are_both_returned(self):
        """D) company_id=1 con tipologías company_id=1 y company_id=NULL -> ambas incluidas sin error."""
        res = await self.client.get(
            "/bm/typologies?service_id=2",
            headers={"Authorization": f"Bearer {self.t_juanjo}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        keys_and_companies = [(t["typology_key"], t["company_id"]) for t in data]
        # seguimiento_encuesta has company_id=1
        self.assertIn(("seguimiento_encuesta", 1), keys_and_companies)
        # cita_trat has company_id=None
        self.assertIn(("cita_trat", None), keys_and_companies)

    # ── E) No NameError when querying /bm/typologies without params ──────────
    async def test_no_name_error_when_service_manager_queries_typologies_list(self):
        """E) GET /bm/typologies sin service_id para service_manager -> no NameError, filtra por allowed_services."""
        res = await self.client.get(
            "/bm/typologies",
            headers={"Authorization": f"Bearer {self.t_juanjo}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        # Juanjo's primary service is 2, so only service 2 typologies should be returned
        keys = [t["typology_key"] for t in data]
        self.assertIn("seguimiento_encuesta", keys)
        self.assertIn("cita_trat", keys)
        self.assertNotIn("cita", keys)
        self.assertNotIn("confirmacion", keys)


if __name__ == "__main__":
    unittest.main()
