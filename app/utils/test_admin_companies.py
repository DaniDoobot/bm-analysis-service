"""
Test Suite: Admin Companies CRUD Endpoints
Tests: GET/POST/PATCH /bm/companies with slug validation, uniqueness, permission scopes.
Uses SQLite in-memory with JSONB compatibility shim.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///admin_companies_test.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.users import User
from app.models.services import Service
from app.models.teams import Team
from app.dependencies import get_current_user
from app.main import app


class TestAdminCompanies(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Engine URL points to production!"

        if os.path.exists("admin_companies_test.db"):
            try:
                os.remove("admin_companies_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine) as db:
            # Companies
            self.c1 = Company(company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c2 = Company(company_name="Gesalux", company_key="gesalux", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()
            self.company1_id = self.c1.company_id
            self.company2_id = self.c2.company_id

            # Services for company1
            self.s1 = Service(service_name="Front", service_key="front", company_id=self.company1_id)
            self.s2 = Service(service_name="Admisiones", service_key="admisiones", company_id=self.company1_id)
            db.add_all([self.s1, self.s2])
            await db.flush()

            # Teams for company1
            self.t1 = Team(team_name="Equipo A", company_id=self.company1_id, service_id=self.s1.service_id, is_active=True)
            db.add(self.t1)
            await db.flush()

            # Users
            self.u_super = User(username="super", email="super@test.com", role="administrador", password_hash="x")
            self.u_comp = User(username="comp_admin", email="comp@test.com", role="company_admin", company_id=self.company1_id, password_hash="x", is_active=True)
            self.u_agent = User(username="agent1", email="agent1@test.com", role="agente", company_id=self.company1_id, password_hash="x", is_active=True)
            db.add_all([self.u_super, self.u_comp, self.u_agent])
            await db.flush()
            self.super_id = self.u_super.user_id
            self.comp_id = self.u_comp.user_id
            self.agent_id = self.u_agent.user_id
            await db.commit()

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # ── TEST 1: super_admin lists all companies ──────────────────────────────

    async def test_1_super_admin_lists_all_companies(self):
        """super_admin sees all companies with enriched counts."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/companies")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data), 2)
            # Verify enriched fields
            for comp in data:
                self.assertIn("company_id", comp)
                self.assertIn("company_name", comp)
                self.assertIn("company_key", comp)
                self.assertIn("is_active", comp)
                self.assertIn("services_count", comp)
                self.assertIn("users_count", comp)
                self.assertIn("teams_count", comp)
                self.assertIn("created_at", comp)
                self.assertIn("updated_at", comp)
                self.assertNotIn("password_hash", comp)
            # Boston Medical should have 2 services and 1 team
            bm = next(c for c in data if c["company_key"] == "boston-medical")
            self.assertEqual(bm["services_count"], 2)
            self.assertEqual(bm["teams_count"], 1)
            self.assertGreaterEqual(bm["users_count"], 1)

    # ── TEST 2: company_admin only sees their company ────────────────────────

    async def test_2_company_admin_sees_own_company(self):
        """company_admin sees only their own company."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/companies")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["company_id"], self.company1_id)

            # Can read detail of own company
            res_detail = await ac.get(f"/bm/companies/{self.company1_id}")
            self.assertEqual(res_detail.status_code, 200)
            self.assertEqual(res_detail.json()["company_id"], self.company1_id)

            # Cannot read detail of another company
            res_other = await ac.get(f"/bm/companies/{self.company2_id}")
            self.assertEqual(res_other.status_code, 403)

    # ── TEST 3: agent cannot manage companies ────────────────────────────────

    async def test_3_agent_cannot_manage_companies(self):
        """Agents cannot create or update companies."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.agent_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Cannot create
            res = await ac.post("/bm/companies", json={"company_name": "Hack", "company_key": "hack"})
            self.assertEqual(res.status_code, 403)

            # Cannot update
            res_patch = await ac.patch(f"/bm/companies/{self.company1_id}", json={"company_name": "Hack"})
            self.assertEqual(res_patch.status_code, 403)

    # ── TEST 4: super_admin creates valid company ────────────────────────────

    async def test_4_super_admin_creates_company(self):
        """super_admin can create a company with valid data."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            payload = {"company_name": "Clinica XYZ", "company_key": "clinica-xyz", "is_active": True}
            res = await ac.post("/bm/companies", json=payload)
            self.assertEqual(res.status_code, 201)
            data = res.json()
            self.assertEqual(data["company_name"], "Clinica XYZ")
            self.assertEqual(data["company_key"], "clinica-xyz")
            self.assertTrue(data["is_active"])
            self.assertEqual(data["services_count"], 0)
            self.assertEqual(data["users_count"], 0)
            self.assertEqual(data["teams_count"], 0)
            self.assertNotIn("password_hash", data)

    # ── TEST 5: company_admin cannot create company ──────────────────────────

    async def test_5_company_admin_cannot_create(self):
        """company_admin cannot create new companies."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/companies", json={"company_name": "Nueva Empresa", "company_key": "nueva-empresa"})
            self.assertEqual(res.status_code, 403)

    # ── TEST 6: duplicate company_key returns 400 ────────────────────────────

    async def test_6_duplicate_company_key_returns_400(self):
        """Duplicate company_key or company_name returns 400."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Duplicate key
            res_dup_key = await ac.post("/bm/companies", json={"company_name": "Diferente", "company_key": "boston-medical"})
            self.assertEqual(res_dup_key.status_code, 400)

            # Duplicate name
            res_dup_name = await ac.post("/bm/companies", json={"company_name": "Boston Medical", "company_key": "otro-key"})
            self.assertEqual(res_dup_name.status_code, 400)

    # ── TEST 7: invalid company_key slug returns 422 ─────────────────────────

    async def test_7_invalid_company_key_returns_422(self):
        """Invalid slug formats for company_key should return validation error (422)."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        # Note: UPPERCASE is NOT invalid — the validator auto-lowercases input
        # (e.g. "UPPERCASE" → "uppercase" → valid slug). This is intentional UX.
        invalid_keys = [
            "",               # empty
            "  ",             # whitespace only
            "Has Spaces",     # spaces (not allowed even after lowercase)
            "ÑoÑo",          # non-ascii special chars
            "-starts-hyphen", # starts with hyphen
            "has@symbol",     # special symbol
        ]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            for bad_key in invalid_keys:
                res = await ac.post(
                    "/bm/companies",
                    json={"company_name": f"Test {bad_key!r}", "company_key": bad_key}
                )
                self.assertIn(
                    res.status_code, [400, 422],
                    f"Expected 400/422 for key={bad_key!r}, got {res.status_code}"
                )

        # Valid slugs that SHOULD pass format validation
        valid_keys = ["valid-key", "another_key", "key123", "a1b2c3"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            for i, good_key in enumerate(valid_keys):
                res = await ac.post(
                    "/bm/companies",
                    json={"company_name": f"Empresa Valida {i}", "company_key": good_key}
                )
                # Should create (201) or fail on dup (400) but NOT 422
                self.assertNotEqual(res.status_code, 422, f"Key '{good_key}' should be valid but got 422")

    # ── TEST 8: super_admin edits company_name ───────────────────────────────

    async def test_8_super_admin_edits_company_name(self):
        """super_admin can rename a company."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.patch(f"/bm/companies/{self.company2_id}", json={"company_name": "Gesalux Dental"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["company_name"], "Gesalux Dental")
            # key should not have changed
            self.assertEqual(res.json()["company_key"], "gesalux")

    # ── TEST 9: super_admin edits company_key with uniqueness check ──────────

    async def test_9_super_admin_edits_company_key(self):
        """super_admin can change company_key; duplicate key blocked."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Valid new key
            res = await ac.patch(f"/bm/companies/{self.company2_id}", json={"company_key": "gesalux-2"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["company_key"], "gesalux-2")

            # Try to set to existing key of company1 → 400
            res_dup = await ac.patch(f"/bm/companies/{self.company2_id}", json={"company_key": "boston-medical"})
            self.assertEqual(res_dup.status_code, 400)
            self.assertIn("ya está en uso", res_dup.json()["detail"])

    # ── TEST 10: activate/deactivate company via is_active ───────────────────

    async def test_10_activate_deactivate_company(self):
        """super_admin can toggle is_active on a company."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Deactivate
            res_off = await ac.patch(f"/bm/companies/{self.company2_id}", json={"is_active": False})
            self.assertEqual(res_off.status_code, 200)
            self.assertFalse(res_off.json()["is_active"])

            # Reactivate
            res_on = await ac.patch(f"/bm/companies/{self.company2_id}", json={"is_active": True})
            self.assertEqual(res_on.status_code, 200)
            self.assertTrue(res_on.json()["is_active"])

            # Filter by is_active
            res_filter = await ac.get("/bm/companies?is_active=true")
            self.assertEqual(res_filter.status_code, 200)
            for c in res_filter.json():
                self.assertTrue(c["is_active"])

    # ── TEST 11: responses do not expose sensitive data ──────────────────────

    async def test_11_response_no_sensitive_fields(self):
        """Company responses never contain password_hash or other sensitive fields."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/companies")
            self.assertEqual(res.status_code, 200)
            for comp in res.json():
                self.assertNotIn("password_hash", comp)

            res_create = await ac.post(
                "/bm/companies",
                json={"company_name": "Clinic Safe", "company_key": "clinic-safe"}
            )
            self.assertEqual(res_create.status_code, 201)
            self.assertNotIn("password_hash", res_create.json())

            res_patch = await ac.patch(
                f"/bm/companies/{self.company1_id}",
                json={"company_name": "Boston Medical Updated"}
            )
            self.assertEqual(res_patch.status_code, 200)
            self.assertNotIn("password_hash", res_patch.json())


if __name__ == "__main__":
    unittest.main()
