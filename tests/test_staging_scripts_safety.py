"""
Unit tests for Staging Database Safety Scripts.
Validates static safety constraints, regex guards, isolation rules, and lack of hardcoded secrets
across neutralize_staging_db.sql, validate_staging_db.sql, and refresh_staging_db.sh.
"""
import os
import re
import unittest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))


class TestStagingScriptsSafety(unittest.TestCase):

    def setUp(self):
        self.neutralize_sql_path = os.path.join(SCRIPTS_DIR, "neutralize_staging_db.sql")
        self.validate_sql_path = os.path.join(SCRIPTS_DIR, "validate_staging_db.sql")
        self.refresh_sh_path = os.path.join(SCRIPTS_DIR, "refresh_staging_db.sh")
        self.compare_sql_path = os.path.join(SCRIPTS_DIR, "compare_prod_staging.sql")

        self.assertTrue(os.path.isfile(self.neutralize_sql_path), "neutralize_staging_db.sql must exist")
        self.assertTrue(os.path.isfile(self.validate_sql_path), "validate_staging_db.sql must exist")
        self.assertTrue(os.path.isfile(self.refresh_sh_path), "refresh_staging_db.sh must exist")
        self.assertTrue(os.path.isfile(self.compare_sql_path), "compare_prod_staging.sql must exist")

        with open(self.neutralize_sql_path, "r", encoding="utf-8") as f:
            self.neutralize_sql = f.read()

        with open(self.validate_sql_path, "r", encoding="utf-8") as f:
            self.validate_sql = f.read()

        with open(self.refresh_sh_path, "r", encoding="utf-8") as f:
            self.refresh_sh = f.read()

        with open(self.compare_sql_path, "r", encoding="utf-8") as f:
            self.compare_sql = f.read()

    def test_01_neutralize_sql_transaction_and_database_guard(self):
        """neutralize_staging_db.sql must run inside a transaction and guard current_database()."""
        self.assertIn("BEGIN;", self.neutralize_sql)
        self.assertIn("COMMIT;", self.neutralize_sql)
        self.assertIn("current_database()", self.neutralize_sql)
        self.assertIn("RAISE EXCEPTION", self.neutralize_sql)

        # Must verify allowed markers
        self.assertTrue(
            bool(re.search(r"staging.*pruebas.*test.*dev", self.neutralize_sql, re.IGNORECASE)),
            "Script must check for staging/pruebas/test/dev markers in current_database()"
        )

        # Must explicitly blacklist production names
        for prod_name in ["n8n", "speechbm", "production", "prod", "bm_analysis"]:
            self.assertIn(prod_name, self.neutralize_sql.lower())

    def test_02_neutralize_sql_required_updates(self):
        """neutralize_staging_db.sql must deactivate schedulers and runs without deleting history."""
        # 1. Mass evaluation jobs schedule disarmed
        self.assertIn("bm_mass_evaluation_jobs", self.neutralize_sql)
        self.assertIn("schedule_enabled = false", self.neutralize_sql)

        # 2. Automations disarmed
        self.assertIn("bm_mass_analysis_automations", self.neutralize_sql)
        self.assertIn("is_active = false", self.neutralize_sql)

        # 3. Active runs cancelled/failed
        self.assertIn("bm_mass_evaluation_runs", self.neutralize_sql)
        self.assertIn("status = 'cancelled'", self.neutralize_sql)

        self.assertIn("bm_mass_analysis_automation_runs", self.neutralize_sql)
        self.assertIn("status = 'failed'", self.neutralize_sql)

        # 4. Training schedulers and runs
        self.assertIn("bm_training_schedulers", self.neutralize_sql)
        self.assertIn("bm_training_scheduler_settings", self.neutralize_sql)
        self.assertIn("bm_training_runs", self.neutralize_sql)

        # 5. CloudTalk jobs
        self.assertIn("bm_cloudtalk_evaluation_jobs", self.neutralize_sql)
        self.assertIn("status = 'skipped'", self.neutralize_sql)

        # 6. Reset tokens purged
        self.assertIn("DELETE FROM bm_password_reset_tokens;", self.neutralize_sql)
        self.assertIn("reset_token = NULL", self.neutralize_sql)

        # Core historical tables must NOT be dropped or truncated
        for safe_table in ["bm_analyses", "bm_analysis_criterion_results", "bm_mass_evaluation_results", "bm_companies"]:
            self.assertNotIn(f"DROP TABLE {safe_table}", self.neutralize_sql.upper())
            self.assertNotIn(f"TRUNCATE {safe_table}", self.neutralize_sql.upper())
            self.assertNotIn(f"DELETE FROM {safe_table}", self.neutralize_sql.upper())

    def test_03_validate_sql_read_only_and_checks(self):
        """validate_staging_db.sql must be strictly read-only and verify views and risk metrics."""
        # Verify read-only: no DDL or DML mutations
        normalized_sql = re.sub(r"--[^\n]*", "", self.validate_sql).upper()
        self.assertNotIn("INSERT INTO", normalized_sql)
        self.assertNotIn("UPDATE ", normalized_sql)
        self.assertNotIn("DELETE FROM", normalized_sql)
        self.assertNotIn("DROP TABLE", normalized_sql)
        self.assertNotIn("ALTER TABLE", normalized_sql)

        # Verify views check
        self.assertIn("vw_bm_%", self.validate_sql)
        self.assertIn("information_schema.views", self.validate_sql)
        self.assertIn("total_analytical_views", self.validate_sql)

        # Verify all 14 expected production views are explicitly audited
        expected_14_views = [
            "vw_bm_all_analysis_criteria_flat",
            "vw_bm_analysis_criteria_flat",
            "vw_bm_analysis_full_current",
            "vw_bm_analysis_full_history",
            "vw_bm_analysis_results_current",
            "vw_bm_analysis_results_history",
            "vw_bm_analysis_results_pivot",
            "vw_bm_individual_analysis_criteria_flat",
            "vw_bm_individual_analysis_report_wide",
            "vw_bm_individual_analysis_summary",
            "vw_bm_mass_evaluation_calls_summary",
            "vw_bm_mass_evaluation_criteria_flat",
            "vw_bm_mass_evaluation_daily_summary",
            "vw_bm_mass_evaluation_report_wide",
        ]
        for v in expected_14_views:
            self.assertIn(v, self.validate_sql)

        # Verify key tables checked
        for tbl in ["bm_companies", "bm_users", "bm_services", "bm_analyses", "bm_mass_evaluation_results"]:
            self.assertIn(tbl, self.validate_sql)

    def test_04_refresh_sh_mandatory_confirm_and_safety_checks(self):
        """refresh_staging_db.sh must require confirmation flag, selective scope, and prevent production overwrites."""
        # Confirmation parameter required
        self.assertIn("--confirm-refresh-staging", self.refresh_sh)

        # URL validation
        self.assertIn("PROD_DATABASE_URL", self.refresh_sh)
        self.assertIn("STAGING_DATABASE_URL", self.refresh_sh)
        self.assertIn("PROD_DATABASE_URL\" = \"$STAGING_DATABASE_URL", self.refresh_sh)

        # Safety comments
        self.assertIn("# NEVER DROP PRODUCTION", self.refresh_sh)
        self.assertIn("# TARGET MUST BE STAGING", self.refresh_sh)

        # Selective dump patterns (-t 'public.bm_*' -t 'public.vw_bm_*')
        self.assertIn("-t 'public.bm_*'", self.refresh_sh)
        self.assertIn("-t 'public.vw_bm_*'", self.refresh_sh)

        # Must NOT dump n8n or agent execution tables
        self.assertNotIn("agent_checkpoints", self.refresh_sh)
        self.assertNotIn("agent_execution", self.refresh_sh)

        # Format custom pg_dump
        self.assertIn("pg_dump", self.refresh_sh)
        self.assertIn("--format=custom", self.refresh_sh)

        # pg_restore options
        self.assertIn("pg_restore", self.refresh_sh)
        self.assertIn("--no-owner", self.refresh_sh)
        self.assertIn("--no-privileges", self.refresh_sh)
        self.assertIn("--clean", self.refresh_sh)
        self.assertIn("--if-exists", self.refresh_sh)

        # No Dokploy or Docker commands
        self.assertNotIn("dokploy", self.refresh_sh.lower())
        self.assertNotIn("docker restart", self.refresh_sh.lower())
        self.assertNotIn("docker stop", self.refresh_sh.lower())

    def test_05_compare_prod_staging_sql_read_only(self):
        """compare_prod_staging.sql must be strictly read-only and compare Speech BM metrics."""
        normalized_sql = re.sub(r"--[^\n]*", "", self.compare_sql).upper()
        self.assertNotIn("INSERT INTO", normalized_sql)
        self.assertNotIn("UPDATE ", normalized_sql)
        self.assertNotIn("DELETE FROM", normalized_sql)
        self.assertNotIn("DROP TABLE", normalized_sql)
        self.assertNotIn("ALTER TABLE", normalized_sql)

        self.assertIn("current_database()", self.compare_sql)
        self.assertIn("bm_%", self.compare_sql)
        self.assertIn("vw_bm_%", self.compare_sql)
        self.assertIn("bm_companies", self.compare_sql)
        self.assertIn("bm_analyses", self.compare_sql)

    def test_06_no_hardcoded_secrets_in_any_file(self):
        """Ensure no real passwords, API keys, or live production URLs are present in scripts."""
        for path, content in [
            (self.neutralize_sql_path, self.neutralize_sql),
            (self.validate_sql_path, self.validate_sql),
            (self.refresh_sh_path, self.refresh_sh),
            (self.compare_sql_path, self.compare_sql),
        ]:
            # No hardcoded password strings
            self.assertNotIn("SpeechPruebas2026!", content)
            self.assertNotIn("91.98.230.119", content)
            self.assertNotIn("AIzaSy", content)
            self.assertNotIn("pat-eu1-", content)
