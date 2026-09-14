"""
Test suite for Migration v017: Agents Comparison Performance Indexes.
====================================================================
Verifies:
1. Migration file exists at migrations/v017_agents_comparison_indexes.sql.
2. Contains idempotent CREATE INDEX IF NOT EXISTS statements.
3. Defines targeted results indexes on bm_mass_evaluation_results.
4. Accurate locking comments and no illegal CONCURRENTLY inside transaction-oriented SQL.
"""
import os
import unittest


class TestAgentsComparisonIndexesMigration(unittest.TestCase):

    def setUp(self):
        self.migration_path = os.path.join("migrations", "v017_agents_comparison_indexes.sql")

    def test_01_migration_file_exists(self):
        """Verifies migration file exists at the expected path."""
        self.assertTrue(os.path.exists(self.migration_path), f"File not found: {self.migration_path}")

    def test_02_idempotent_syntax(self):
        """Verifies that all CREATE INDEX statements use IF NOT EXISTS."""
        with open(self.migration_path, "r", encoding="utf-8") as f:
            sql_content = f.read()

        raw_blocks = sql_content.split(";")
        statements = []
        for blk in raw_blocks:
            lines = [l.strip() for l in blk.splitlines() if l.strip() and not l.strip().startswith("--")]
            if lines:
                statements.append(" ".join(lines))
        self.assertEqual(len(statements), 2, f"Expected exactly 2 index statements, found {len(statements)}")

        for stmt in statements:
            self.assertTrue(
                "CREATE INDEX IF NOT EXISTS" in stmt,
                f"Statement must use CREATE INDEX IF NOT EXISTS: {stmt}"
            )

    def test_03_targeted_indexes_coverage(self):
        """Verifies the exact target indexes and columns are present."""
        with open(self.migration_path, "r", encoding="utf-8") as f:
            sql_content = f.read()

        # 1. Index on service_id + status + COALESCE(call_timestamp, analysis_timestamp)
        self.assertIn("idx_mass_eval_results_svc_status_coalesce_date", sql_content)
        self.assertIn("bm_mass_evaluation_results", sql_content)
        self.assertIn("service_id", sql_content)
        self.assertIn("status", sql_content)
        self.assertIn("COALESCE(call_timestamp, analysis_timestamp)", sql_content)

        # 2. Index on company_id + COALESCE(call_timestamp, analysis_timestamp)
        self.assertIn("idx_mass_eval_results_company_coalesce_date", sql_content)
        self.assertIn("company_id", sql_content)

    def test_04_no_concurrently_inside_migration(self):
        """Verifies that CONCURRENTLY is not used inside migration script directly."""
        with open(self.migration_path, "r", encoding="utf-8") as f:
            sql_content = f.read()
        # Ensure CONCURRENTLY is only mentioned in comments, not in statements
        statements = [stmt.strip() for stmt in sql_content.split(";") if stmt.strip() and not stmt.strip().startswith("--")]
        for stmt in statements:
            self.assertNotIn("CONCURRENTLY", stmt.upper())

    def test_05_locking_documentation(self):
        """Verifies that migration does not claim to be non-blocking and accurately describes SHARE lock."""
        with open(self.migration_path, "r", encoding="utf-8") as f:
            sql_content = f.read()
        self.assertNotIn("non-blocking", sql_content.lower())
        self.assertIn("SHARE lock", sql_content)
        self.assertIn("BLOCKED", sql_content)


if __name__ == "__main__":
    unittest.main()
