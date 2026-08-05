"""
Test suite for realistic agent initials resolution.
====================================================
Verifies resolve_agent_initials logic for:
1. Match by hubspot_owner_id returning User.agent_initials ("LD")
2. Match by normalized name when hubspot_owner_id is missing ("LD")
3. Result with name that would fallback to "LF", but matching User.agent_initials gives "LD"
4. Standard fallback calculation when no user/match exists
"""
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agent_initials_realistic_test.db"

# SQLite compilation patches for test environment
from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult
from app.utils.agent_resolvers import (
    resolve_agent_initials,
    build_user_initials_maps,
    resolve_agent_initials_async,
    get_fallback_initials,
    normalize_name_key,
)


class TestAgentInitialsResolutionRealistic(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            # Seed test company & service
            db.add(Company(company_id=900, company_key="bm_init_test", company_name="Initials Test Co"))
            await db.flush()
            db.add(Service(service_id=901, company_id=900, service_key="front_init", service_name="Front Init"))
            await db.flush()

            # Seed User Luci Dos Santos with hubspot_owner_id="1375831790" and agent_initials="LD"
            db.add(User(
                user_id=9001,
                username="luci_dos_santos",
                email="luci@bostonmedical.es",
                name="Luci Dos Santos",
                role="agent",
                company_id=900,
                is_active=True,
                hubspot_owner_id="1375831790",
                agent_initials="LD",
                password_hash="dummy_hash",
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(User).where(User.user_id == 9001))
            await db.execute(delete(Service).where(Service.service_id == 901))
            await db.execute(delete(Company).where(Company.company_id == 900))
            await db.commit()

    async def test_case_1_match_by_hubspot_owner_id(self):
        """Case 1: Result has hubspot_owner_id='1375831790', matches User with agent_initials='LD'."""
        async with AsyncSession(self.engine) as db:
            by_owner, by_name, users_list = await build_user_initials_maps(db, company_id=900)
            initials = resolve_agent_initials(
                hubspot_owner_id="1375831790",
                agent_name="Luci Dos Santos Furtado",
                by_owner=by_owner,
                by_name=by_name,
                users_list=users_list,
            )
            self.assertEqual(initials, "LD")

    async def test_case_2_match_by_normalized_name_without_owner_id(self):
        """Case 2: Result has no hubspot_owner_id, but agent_name matches User.name -> returns 'LD'."""
        async with AsyncSession(self.engine) as db:
            by_owner, by_name, users_list = await build_user_initials_maps(db, company_id=900)
            initials = resolve_agent_initials(
                hubspot_owner_id=None,
                agent_name="Luci Dos Santos Furtado",
                by_owner=by_owner,
                by_name=by_name,
                users_list=users_list,
            )
            self.assertEqual(initials, "LD")

    async def test_case_3_name_would_fallback_to_LF_but_user_gives_LD(self):
        """Case 3: Fallback on name 'Luci Furtado' would produce 'LF', but User match gives 'LD'."""
        # Standard fallback would be 'LF'
        self.assertEqual(get_fallback_initials("Luci Furtado"), "LF")

        async with AsyncSession(self.engine) as db:
            by_owner, by_name, users_list = await build_user_initials_maps(db, company_id=900)
            initials = resolve_agent_initials(
                hubspot_owner_id=None,
                agent_name="Luci Furtado",
                by_owner=by_owner,
                by_name=by_name,
                users_list=users_list,
            )
            self.assertEqual(initials, "LD")

    async def test_case_4_unknown_agent_uses_fallback(self):
        """Case 4: No user match exists -> fallback calculated initials used."""
        async with AsyncSession(self.engine) as db:
            by_owner, by_name, users_list = await build_user_initials_maps(db, company_id=900)
            initials = resolve_agent_initials(
                hubspot_owner_id=None,
                agent_name="Pedro Gomez",
                by_owner=by_owner,
                by_name=by_name,
                users_list=users_list,
            )
            self.assertEqual(initials, "PG")

    async def test_async_convenience_wrapper(self):
        """Convenience wrapper resolve_agent_initials_async returns correct initials."""
        async with AsyncSession(self.engine) as db:
            res = await resolve_agent_initials_async(
                db=db,
                hubspot_owner_id="1375831790",
                agent_name="Luci Dos Santos",
                company_id=900,
            )
            self.assertEqual(res, "LD")


if __name__ == "__main__":
    unittest.main()
