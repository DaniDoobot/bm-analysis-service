"""
Unit test suite for prompt criteria drag & drop reordering endpoint.
"""
import os
import unittest
import httpx

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_reorder_db_test.db"

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.db import Base, get_engine
from app.models.users import User
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.utils.security import hash_password
from sqlalchemy.ext.asyncio import AsyncSession


class TestPromptCriteriaReorder(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine, expire_on_commit=False) as session:
            user = User(
                user_id=1,
                username="admin_user",
                email="admin@example.com",
                password_hash=hash_password("AdminPass123!"),
                role="super_admin",
                is_active=True,
                must_reset_password=False
            )
            session.add(user)

            prompt = Prompt(
                prompt_id=50,
                prompt_name="Test Structure",
                prompt_type="specific",
                is_active=True
            )
            session.add(prompt)

            pv = PromptVersion(
                id=100,
                prompt_id=50,
                prompt="<!-- BM_CRITERIA_BLOCK_START -->\n<!-- BM_CRITERIA_BLOCK_END -->",
                is_current=True
            )
            session.add(pv)

            c1 = PromptCriterion(
                criterion_id=101,
                prompt_id=50,
                criterion_key="crit_a",
                criterion_name="Criterion A",
                criterion_type="boolean",
                output_key="crit_a",
                order_index=10,
                is_active=True
            )
            c2 = PromptCriterion(
                criterion_id=102,
                prompt_id=50,
                criterion_key="crit_b",
                criterion_name="Criterion B",
                criterion_type="percentage",
                output_key="crit_b",
                order_index=20,
                is_active=True
            )
            c3 = PromptCriterion(
                criterion_id=103,
                prompt_id=50,
                criterion_key="crit_c",
                criterion_name="Criterion C",
                criterion_type="text",
                output_key="crit_c",
                order_index=30,
                is_active=True
            )
            session.add_all([c1, c2, c3])
            await session.commit()

        transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=transport, base_url="http://test")

        # Login to get token
        resp = await self.client.post("/bm/auth/login", json={
            "username": "admin_user",
            "password": "AdminPass123!"
        })
        self.token = resp.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    async def asyncTearDown(self):
        await self.client.aclose()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        if os.path.exists("./test_reorder_db_test.db"):
            try:
                os.remove("./test_reorder_db_test.db")
            except Exception:
                pass

    async def test_reorder_success(self):
        # Reorder to [103, 101, 102]
        resp = await self.client.post(
            "/bm/prompt-criteria/reorder",
            headers=self.headers,
            json={
                "prompt_id": 50,
                "ordered_criterion_ids": [103, 101, 102]
            }
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("criteria_count"), 3)
        reordered_ids = [c["criterion_id"] for c in data.get("criteria", [])]
        self.assertEqual(reordered_ids, [103, 101, 102])

    async def test_reorder_duplicates_fail(self):
        resp = await self.client.post(
            "/bm/prompt-criteria/reorder",
            headers=self.headers,
            json={
                "prompt_id": 50,
                "ordered_criterion_ids": [103, 101, 101]
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("duplicados", resp.json().get("detail", ""))

    async def test_reorder_incomplete_list_fail(self):
        resp = await self.client.post(
            "/bm/prompt-criteria/reorder",
            headers=self.headers,
            json={
                "prompt_id": 50,
                "ordered_criterion_ids": [103, 101]
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Faltan criterios activos", resp.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main()
