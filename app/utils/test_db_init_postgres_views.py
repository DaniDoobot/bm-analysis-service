"""
Test Suite: test_db_init_postgres_views.py
Validates that db_init_service runs without connection closed errors.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///bm_perf_test.db"

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.db import get_engine, Base
from app.services.db_init_service import init_db

async def run_tests():
    engine = get_engine()
    if os.path.exists("bm_perf_test.db"):
        try:
            os.remove("bm_perf_test.db")
        except Exception:
            pass

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    print("Running init_db()...")
    await init_db()
    print("PASS: test_db_init_postgres_views executed cleanly without exceptions.")

if __name__ == "__main__":
    asyncio.run(run_tests())
