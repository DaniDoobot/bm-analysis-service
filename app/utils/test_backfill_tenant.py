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
            # Create a user and service without company_id
            u1 = User(user_id=10, username="user1", email="user1@test.com", role="agent", company_id=None, password_hash="hash")
            s1 = Service(service_id=10, service_key="s1", service_name="Service 1", company_id=None)
            db.add_all([u1, s1])
            await db.commit()

        # Run backfill in dry-run
        await run_backfill("--dry-run")

        async with AsyncSession(self.session_factory) as db:
            # Check user company_id remains None
            res_u = await db.execute(select(User).where(User.user_id == 10))
            user = res_u.scalar()
            self.assertIsNone(user.company_id)

            # Check no Boston Medical company was committed
            res_c = await db.execute(select(Company).where(Company.company_key == "boston-medical"))
            comp = res_c.scalar()
            self.assertIsNone(comp)

    async def test_apply_commits_and_preserves_other_companies(self):
        """2. Validate that --apply backfills NULL records and keeps other companies untouched."""
        async with AsyncSession(self.session_factory) as db:
            # Create another company
            other_comp = Company(company_id=2, company_name="Other Clinic", company_key="other-clinic", is_active=True)
            db.add(other_comp)

            # User 1: legacy (NULL company_id)
            u1 = User(user_id=10, username="user1", email="user1@test.com", role="agent", company_id=None, password_hash="hash")
            # User 2: belongs to other company
            u2 = User(user_id=20, username="user2", email="user2@test.com", role="agent", company_id=2, password_hash="hash")

            # Service 1: legacy (NULL company_id)
            s1 = Service(service_id=10, service_key="s1", service_name="Service 1", company_id=None)
            # Service 2: belongs to other company
            s2 = Service(service_id=20, service_key="s2", service_name="Service 2", company_id=2)

            db.add_all([u1, u2, s1, s2])
            await db.commit()

        # Run backfill apply
        await run_backfill("--apply")

        async with AsyncSession(self.session_factory) as db:
            # A. Boston Medical company should be created
            res_c = await db.execute(select(Company).where(Company.company_key == "boston-medical"))
            boston = res_c.scalar()
            self.assertIsNotNone(boston)
            boston_id = boston.company_id

            # B. User 1 should be updated to Boston Medical
            res_u1 = await db.execute(select(User).where(User.user_id == 10))
            user1 = res_u1.scalar()
            self.assertEqual(user1.company_id, boston_id)

            # C. User 2 should NOT be touched (remains other company)
            res_u2 = await db.execute(select(User).where(User.user_id == 20))
            user2 = res_u2.scalar()
            self.assertEqual(user2.company_id, 2)

            # D. Service 1 should be updated to Boston Medical
            res_s1 = await db.execute(select(Service).where(Service.service_id == 10))
            service1 = res_s1.scalar()
            self.assertEqual(service1.company_id, boston_id)

    async def test_verify_detects_inconsistencies(self):
        """3. Validate that verify checks report NULL values and mismatched company_ids."""
        async with AsyncSession(self.session_factory) as db:
            # Create Boston Medical
            boston = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            other = Company(company_id=2, company_name="Other Clinic", company_key="other-clinic", is_active=True)
            db.add_all([boston, other])
            await db.flush()

            # Service belongs to other clinic (ID 2)
            s = Service(service_id=1, service_key="s1", service_name="S1", company_id=2)
            # Team belongs to Boston Medical (ID 1) -> Inconsistent!
            t = Team(team_id=1, team_name="T1", company_id=1, service_id=1)
            # User has NULL company_id
            u = User(user_id=10, username="user1", email="user1@test.com", role="agent", company_id=None, password_hash="hash")

            db.add_all([s, t, u])
            await db.commit()

        # Capture output of verify check
        import sys
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            await verify_backfill()
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("VERIFICATION FAILED", output)
        self.assertIn("users have NULL company_id", output)
        self.assertIn("does not match Service 'S1' company_id (2)", output)


if __name__ == "__main__":
    unittest.main()
