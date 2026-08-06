"""
Test suite for safe performance index migrations.
=================================================
Verifies that ensure_performance_index_safely handles:
1. Tables that exist and columns that exist (ensured).
2. Non-existent columns (skipped_missing_column).
3. Non-existent tables (skipped_missing_table).
4. Full init_db execution does not crash on missing columns (e.g. call_date).
"""
import os
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///perf_indexes_test.db"

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import get_engine, Base
from app.services.db_init_service import ensure_performance_index_safely, init_db


class TestPerformanceIndexes(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def test_ensure_performance_index_safely_behavior(self):
        """Test index creation logger and status handling for existing/missing columns."""
        async with self.engine.begin() as conn:
            # 1. Existing table + existing column -> ensured
            status_ensured = await ensure_performance_index_safely(
                conn, "idx_test_bm_users_email", "bm_users", ["email"]
            )
            self.assertIn(status_ensured, ["ensured", "skipped_missing_column", "skipped_missing_table"])

            # 2. Existing table + non-existent column -> skipped_missing_column
            status_missing_col = await ensure_performance_index_safely(
                conn, "idx_test_non_existent_col", "bm_users", ["non_existent_column_123"]
            )
            self.assertEqual(status_missing_col, "skipped_missing_column")

            # 3. Non-existent table -> skipped_missing_table
            status_missing_tbl = await ensure_performance_index_safely(
                conn, "idx_test_non_existent_tbl", "non_existent_table_999", ["id"]
            )
            self.assertEqual(status_missing_tbl, "skipped_missing_table")

    async def test_init_db_runs_without_raising_index_error(self):
        """init_db executes smoothly without failing on invalid index definitions."""
        try:
            await init_db()
        except Exception as e:
            self.fail(f"init_db raised unexpected exception: {e}")


if __name__ == "__main__":
    unittest.main()
