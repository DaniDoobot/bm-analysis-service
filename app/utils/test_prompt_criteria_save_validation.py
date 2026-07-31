"""
Unit test suite for criteria save validation, prompt length limits and category allowed_values normalization.
"""
import os
import unittest
import httpx

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_save_validation_db_test.db"

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


class TestPromptCriteriaSaveValidation(unittest.IsolatedAsyncioTestCase):
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
                prompt_id=60,
                prompt_name="Test Validation Prompt",
                prompt_type="specific",
                is_active=True
            )
            session.add(prompt)

            pv = PromptVersion(
                id=200,
                prompt_id=60,
                prompt="<!-- BM_CRITERIA_BLOCK_START -->\n<!-- BM_CRITERIA_BLOCK_END -->",
                is_current=True
            )
            session.add(pv)

            crit_cat = PromptCriterion(
                criterion_id=201,
                prompt_id=60,
                criterion_key="tipo_llamada",
                criterion_name="Tipo de Llamada",
                criterion_type="category",
                output_key="tipo_llamada",
                allowed_values=["cita", "confirmacion", "cita", "otros", "otros", "  "],
                order_index=10,
                is_active=True
            )
            session.add(crit_cat)
            await session.commit()

        transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=transport, base_url="http://test")

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
        if os.path.exists("./test_save_validation_db_test.db"):
            try:
                os.remove("./test_save_validation_db_test.db")
            except Exception:
                pass

    async def test_category_normalization_endpoint(self):
        # Trigger category normalization
        resp = await self.client.post(
            "/bm/prompt-criteria/normalize-categories?prompt_id=60",
            headers=self.headers
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))

        # Verify allowed_values has no duplicates and no empty strings
        engine = get_engine()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            c = await session.get(PromptCriterion, 201)
            self.assertIn("cita", c.allowed_values)
            self.assertIn("otros", c.allowed_values)
            self.assertEqual(c.allowed_values.count("otros"), 1)
            self.assertEqual(c.allowed_values.count("cita"), 1)
            self.assertNotIn("  ", c.allowed_values)

    async def test_prompt_length_150k_allowed(self):
        # A prompt of ~150,000 characters should NOT fail (limit raised to 500,000)
        large_description = "B" * 150000
        resp = await self.client.post(
            "/bm/prompt-criteria/save",
            headers=self.headers,
            json={
                "prompt_id": 60,
                "criterion_key": "large_150k_criterion",
                "criterion_name": "Large 150k Criterion",
                "criterion_description": large_description,
                "criterion_type": "text",
                "output_key": "large_150k_criterion"
            }
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))

    async def test_prompt_length_exceeded_error_structure(self):
        # Try to save a criterion with description causing prompt > 500,000 chars
        huge_description = "A" * 505000
        resp = await self.client.post(
            "/bm/prompt-criteria/save",
            headers=self.headers,
            json={
                "prompt_id": 60,
                "criterion_key": "huge_criterion",
                "criterion_name": "Huge Criterion",
                "criterion_description": huge_description,
                "criterion_type": "text",
                "output_key": "huge_criterion"
            }
        )
        self.assertEqual(resp.status_code, 422)
        detail = resp.json().get("detail", {})
        self.assertEqual(detail.get("code"), "prompt_too_long")
        self.assertGreater(detail.get("prompt_length", 0), 500000)
        self.assertEqual(detail.get("max_prompt_length"), 500000)
        self.assertTrue(len(detail.get("largest_criteria", [])) > 0)
        self.assertIn("500,000", detail.get("suggestion", ""))


if __name__ == "__main__":
    unittest.main()
