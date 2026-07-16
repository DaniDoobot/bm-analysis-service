import os
import sys
import unittest
import io
from unittest.mock import patch

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///backfill_test.db"

# Setup path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# SQLite Type Compilers for Compatibility
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import BigInteger

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.utils.backfill_boston_medical_tenant import run_backfill, verify_backfill

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class TestBackfillTenant(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        if os.path.exists("backfill_test.db"):
            try:
                os.remove("backfill_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Setup base database connection
        self.session_factory = get_engine()

    async def asyncTearDown(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        if os.path.exists("backfill_test.db"):
            try:
                os.remove("backfill_test.db")
            except Exception:
                pass

    async def test_dry_run_does_not_commit_changes(self):
        """1. Validate that --dry-run does not write changes to database."""
        async with AsyncSession(self.session_factory) as db:
            u1 = User(user_id=10, username="user1", email="user1@test.com", role="agent", company_id=None, password_hash="hash")
            s1 = Service(service_id=10, service_key="s1", service_name="Service 1", company_id=None)
            db.add_all([u1, s1])
            await db.commit()

        await run_backfill("--dry-run")

        async with AsyncSession(self.session_factory) as db:
            res_u = await db.execute(select(User).where(User.user_id == 10))
            user = res_u.scalar()
            self.assertIsNone(user.company_id)

            res_c = await db.execute(select(Company).where(Company.company_key == "boston-medical"))
            comp = res_c.scalar()
            self.assertIsNone(comp)

    async def test_apply_commits_and_preserves_other_companies(self):
        """2. Validate that --apply backfills NULL records and detaches super_admins."""
        async with AsyncSession(self.session_factory) as db:
            other_comp = Company(company_id=2, company_name="Other Clinic", company_key="other-clinic", is_active=True)
            db.add(other_comp)

            # User 1: agent legacy (NULL company_id)
            u1 = User(user_id=10, username="user1", email="user1@test.com", role="agente", company_id=None, password_hash="hash")
            # User 2: agent belongs to other company
            u2 = User(user_id=20, username="user2", email="user2@test.com", role="agente", company_id=2, password_hash="hash")
            # User 3: super_admin legacy has company_id 2 (should be detached)
            u3 = User(user_id=30, username="user3", email="user3@test.com", role="administrador", company_id=2, password_hash="hash")
            # User 4: super_admin legacy has NULL company_id (should remain NULL)
            u4 = User(user_id=40, username="user4", email="user4@test.com", role="admin", company_id=None, password_hash="hash")

            # Service 1: legacy (NULL company_id)
            s1 = Service(service_id=10, service_key="s1", service_name="Service 1", company_id=None)

            db.add_all([u1, u2, u3, u4, s1])
            await db.commit()

        # Run backfill apply
        await run_backfill("--apply")

        async with AsyncSession(self.session_factory) as db:
            res_c = await db.execute(select(Company).where(Company.company_key == "boston-medical"))
            boston = res_c.scalar()
            self.assertIsNotNone(boston)
            boston_id = boston.company_id

            # User 1 (agent) -> Boston Medical
            res_u1 = await db.execute(select(User).where(User.user_id == 10))
            self.assertEqual(res_u1.scalar().company_id, boston_id)

            # User 2 (agent other company) -> Remains 2
            res_u2 = await db.execute(select(User).where(User.user_id == 20))
            self.assertEqual(res_u2.scalar().company_id, 2)

            # User 3 (super_admin has company_id) -> Detached (None)
            res_u3 = await db.execute(select(User).where(User.user_id == 30))
            self.assertIsNone(res_u3.scalar().company_id)

            # User 4 (super_admin NULL company_id) -> Remains None
            res_u4 = await db.execute(select(User).where(User.user_id == 40))
            self.assertIsNone(res_u4.scalar().company_id)

            # Service 1 -> Boston Medical
            res_s1 = await db.execute(select(Service).where(Service.service_id == 10))
            self.assertEqual(res_s1.scalar().company_id, boston_id)

    async def test_verify_detects_inconsistencies(self):
        """3. Validate that verify checks report NULL values and mismatched company_ids."""
        async with AsyncSession(self.session_factory) as db:
            boston = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            db.add(boston)
            await db.flush()

            # Super admin has company_id -> Inconsistent!
            u1 = User(user_id=10, username="user1", email="user1@test.com", role="administrador", company_id=1, password_hash="hash")
            # Agent has NULL company_id -> Inconsistent!
            u2 = User(user_id=20, username="user2", email="user2@test.com", role="agente", company_id=None, password_hash="hash")

            db.add_all([u1, u2])
            await db.commit()

        # Capture output of verify check
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            await verify_backfill()
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("VERIFICATION FAILED", output)
        self.assertIn("Super admin user 'user1' (role: administrador) has company_id 1 (expected NULL)", output)
        self.assertIn("Non-superadmin user 'user2' (role: agente) has NULL company_id", output)

    async def test_idempotency_and_verify_success(self):
        """4. Test that verify succeeds after apply, and running apply twice changes nothing."""
        async with AsyncSession(self.session_factory) as db:
            u1 = User(user_id=10, username="user1", email="user1@test.com", role="agente", company_id=None, password_hash="hash")
            u2 = User(user_id=20, username="user2", email="user2@test.com", role="administrador", company_id=1, password_hash="hash")
            db.add_all([u1, u2])
            await db.commit()

        # Run 1st apply
        await run_backfill("--apply")

        # Capture output of verify after apply
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            await verify_backfill()
            output1 = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("VERIFICATION SUCCESS", output1)

        # Run 2nd apply (idempotency check)
        await run_backfill("--apply")

        # Capture output of verify again
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            await verify_backfill()
            output2 = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("VERIFICATION SUCCESS", output2)


if __name__ == "__main__":
    unittest.main()
