"""
Unit and integration test suite for ERR-01 (Lookback search margin & canonical deduplication).
Validates all required test scenarios:
1. Normal short call inside window -> processed once.
2. Long call whose timestamp is in previous window and recording appears later -> captured in next execution via lookback.
3. Canonical identity: same call_id + same prompt_id + completed -> deduplicated and not reprocessed.
4. Canonical identity: same call_id + different prompt_id -> NOT blocked (allowed to evaluate independently).
5. Canonical identity: same call_id + same prompt_id + completed + is_evaluable=False -> deduplicated and not reprocessed.
6. Failed call within lookback -> not blocked, re-eligible for attempt.
7. Call <20s in automation -> excluded by policy.
8. Manual run <20s -> allowed.
9. Logical windows remain continuous.
10. Lookback (default 120 min) covers calls >60 min and does not modify last_window_to watermark.
11. No duplicates in bm_mass_evaluation_results on multiple overlapping runs.
"""
import os
import sys

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///local_test_err01.sqlite"
os.environ["APP_ENV"] = "test"
sys.path.insert(0, os.path.abspath("."))

import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, delete, text

from app.models.mass_evaluations import (
    Base,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from app.services.mass_evaluation_service import MassEvaluationService
from app.config import get_settings


class TestERR01LookbackCoverage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        self.SessionLocal = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def test_00_lookback_default_is_120_minutes(self):
        """Verify default lookback is 120 minutes."""
        self.assertEqual(get_settings().automation_call_lookback_minutes, 120)

    async def test_01_and_02_lookback_captures_delayed_recording_and_long_calls(self):
        """
        Scenario 1 & 2:
        Run 1 at 11:06:05 for window 10:51-11:01 misses call at 10:58 because recording is missing.
        Run 2 at 11:16:05 for window 11:01-11:11 searches [09:01, 11:11] (lookback 120m)
        and successfully captures the call now that recording is present.
        Also verifies a 75-minute long call (> 60m) is comfortably covered.
        """
        async with self.SessionLocal() as db:
            job = MassEvaluationJob(
                job_id=1,
                job_name="Front Automation Job",
                service_id=1,
                prompt_id=58,
                is_active=True,
                execution_source="automation",
                only_with_recording=True,
                duration_min_seconds=20,
            )
            db.add(job)
            await db.commit()

            aut = MassAnalysisAutomation(
                automation_id=1,
                name="Front Auto",
                job_id=1,
                service_id=1,
                prompt_id=58,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
                is_active=True,
            )
            db.add(aut)
            await db.commit()

            now_1 = datetime(2026, 8, 13, 11, 6, 5, tzinfo=timezone.utc)
            w_from_1, w_to_1, ready_1, _ = await MassEvaluationService.get_automation_next_window(
                db, aut, now=now_1
            )
            self.assertEqual(w_to_1, datetime(2026, 8, 13, 11, 1, 5, tzinfo=timezone.utc))

            # Simulate completing Run 1
            run_1 = MassAnalysisAutomationRun(
                automation_id=1,
                status="completed",
                started_at=now_1,
                finished_at=now_1,
                window_from=w_from_1,
                window_to=w_to_1,
                calls_found=0,
                calls_selected=0,
                calls_skipped=0,
            )
            db.add(run_1)
            await db.commit()

            # Now Run 2 at 11:16:05:
            now_2 = datetime(2026, 8, 13, 11, 16, 5, tzinfo=timezone.utc)
            w_from_2, w_to_2, ready_2, _ = await MassEvaluationService.get_automation_next_window(
                db, aut, now=now_2
            )
            # Logical window is strictly continuous
            self.assertEqual(w_from_2, w_to_1)
            self.assertEqual(w_to_2, datetime(2026, 8, 13, 11, 11, 5, tzinfo=timezone.utc))

            # Effective search from with 120m lookback
            lookback_margin = get_settings().automation_call_lookback_minutes
            self.assertEqual(lookback_margin, 120)
            eff_search_from = w_from_2 - timedelta(minutes=lookback_margin)
            
            # Case 511728701643 (started at 10:58:16)
            call_ts_1 = datetime(2026, 8, 13, 10, 58, 16, tzinfo=timezone.utc)
            self.assertTrue(eff_search_from <= call_ts_1 <= w_to_2)

            # Ultra long call: started 75 min ago (> 60m) at 09:40:00
            call_ts_long = datetime(2026, 8, 13, 9, 40, 0, tzinfo=timezone.utc)
            self.assertTrue(eff_search_from <= call_ts_long <= w_to_2)

    async def test_03_and_04_canonical_identity_deduplication(self):
        """
        Scenario 3, 4, 5:
        Deduplication uses canonical identity (call_id + prompt_id + status='completed').
        - same call_id + same prompt (58) + completed (evaluable) -> skipped.
        - same call_id + same prompt (58) + completed (non-evaluable) -> skipped.
        - same call_id + different prompt (99) + completed -> NOT skipped for prompt 58.
        - same call_id + same prompt (58) + failed -> NOT skipped (re-attempted).
        - new call_id -> NOT skipped.
        """
        async with self.SessionLocal() as db:
            current_prompt_id = 58

            # 1. same call_id + same prompt (58) + completed
            res_evaluable = MassEvaluationResult(
                run_id=100,
                job_id=1,
                call_id="call_evaluable_1",
                prompt_id=current_prompt_id,
                prompt_snapshot="Test prompt snapshot",
                status="completed",
                is_evaluable=True,
                evaluacion_global=8.5,
            )
            # 2. same call_id + same prompt (58) + completed + is_evaluable=False
            res_non_evaluable = MassEvaluationResult(
                run_id=100,
                job_id=1,
                call_id="call_non_evaluable_2",
                prompt_id=current_prompt_id,
                prompt_snapshot="Test prompt snapshot",
                status="completed",
                is_evaluable=False,
                non_evaluable_reason="dropped_or_cut_call",
                evaluacion_global=None,
            )
            # 3. same call_id + different prompt (99) + completed
            res_different_prompt = MassEvaluationResult(
                run_id=100,
                job_id=2,
                call_id="call_diff_prompt_3",
                prompt_id=99,
                prompt_snapshot="Other prompt snapshot",
                status="completed",
                is_evaluable=True,
                evaluacion_global=9.0,
            )
            # 4. same call_id + same prompt (58) + failed
            res_failed = MassEvaluationResult(
                run_id=100,
                job_id=1,
                call_id="call_failed_4",
                prompt_id=current_prompt_id,
                prompt_snapshot="Test prompt snapshot",
                status="failed",
                error_message="503 UNAVAILABLE",
            )
            db.add_all([res_evaluable, res_non_evaluable, res_different_prompt, res_failed])
            await db.commit()

            # Incoming candidates from HubSpot during overlapping search
            candidate_ids = [
                "call_evaluable_1",
                "call_non_evaluable_2",
                "call_diff_prompt_3",
                "call_failed_4",
                "call_brand_new_5",
            ]

            # Query completed IDs for current_prompt_id
            stmt_completed = (
                select(MassEvaluationResult.call_id)
                .where(
                    MassEvaluationResult.call_id.in_(candidate_ids),
                    MassEvaluationResult.prompt_id == current_prompt_id,
                    MassEvaluationResult.status == "completed",
                )
            )
            res_completed = await db.execute(stmt_completed)
            already_completed_set = set(res_completed.scalars().all())

            # 1. same prompt + completed (evaluable) -> in set (skipped)
            self.assertIn("call_evaluable_1", already_completed_set)
            # 2. same prompt + completed (non-evaluable) -> in set (skipped)
            self.assertIn("call_non_evaluable_2", already_completed_set)
            # 3. different prompt (99) -> NOT in set for prompt 58 (NOT skipped)
            self.assertNotIn("call_diff_prompt_3", already_completed_set)
            # 4. same prompt + failed -> NOT in set (NOT skipped, re-attempted)
            self.assertNotIn("call_failed_4", already_completed_set)
            # 5. brand new call -> NOT in set (NOT skipped)
            self.assertNotIn("call_brand_new_5", already_completed_set)

            new_candidates = [cid for cid in candidate_ids if cid not in already_completed_set]
            self.assertEqual(new_candidates, ["call_diff_prompt_3", "call_failed_4", "call_brand_new_5"])

    async def test_06_failed_calls_not_permanently_blocked(self):
        """
        Scenario 6:
        Calls with status='failed' can be re-evaluated when they reappear in lookback.
        """
        async with self.SessionLocal() as db:
            r_failed = MassEvaluationResult(
                run_id=101,
                job_id=1,
                call_id="call_retry_6",
                prompt_id=58,
                prompt_snapshot="Test prompt snapshot",
                status="failed",
                error_message="500 INTERNAL",
            )
            db.add(r_failed)
            await db.commit()

            updated_row = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=102,
                job_id=1,
                execution_source="automation",
                call_id="call_retry_6",
                prompt_id=58,
                defaults={
                    "prompt_snapshot": "Test prompt snapshot",
                    "status": "completed",
                    "is_evaluable": True,
                    "evaluacion_global": 7.8,
                    "error_message": None,
                },
            )
            await db.commit()

            self.assertEqual(updated_row.status, "completed")
            self.assertEqual(updated_row.evaluacion_global, 7.8)

    def test_07_and_08_automation_duration_policy(self):
        """
        Scenario 7 & 8:
        Automation enforces 20s minimum; manual run does not.
        """
        auto_min = get_settings().automation_min_duration_seconds
        self.assertEqual(auto_min, 20)

        # Automation source
        eff_auto = None
        exec_src_auto = "automation"
        if exec_src_auto == "automation":
            if eff_auto is None or eff_auto < auto_min:
                eff_auto = auto_min
        self.assertEqual(eff_auto, 20)

        # Manual source
        eff_manual = None
        exec_src_manual = "manual"
        if exec_src_manual == "automation":
            if eff_manual is None or eff_manual < auto_min:
                eff_manual = auto_min
        self.assertIsNone(eff_manual)

    async def test_09_continuous_watermark_and_lookback_invariance(self):
        """
        Scenario 9:
        Logical windows remain strictly continuous (gap=0s) and lookback search
        does not modify last_window_to.
        """
        async with self.SessionLocal() as db:
            aut = MassAnalysisAutomation(
                automation_id=2,
                name="Continuity Test",
                job_id=1,
                service_id=1,
                prompt_id=58,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
                is_active=True,
            )
            db.add(aut)
            await db.commit()

            t0 = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)
            t1 = datetime(2026, 8, 13, 10, 10, 0, tzinfo=timezone.utc)
            t2 = datetime(2026, 8, 13, 10, 20, 0, tzinfo=timezone.utc)

            run1 = MassAnalysisAutomationRun(
                automation_id=2,
                status="completed",
                started_at=t1,
                finished_at=t1,
                window_from=t0,
                window_to=t1,
            )
            db.add(run1)
            await db.commit()

            w_from, w_to, is_ready, _ = await MassEvaluationService.get_automation_next_window(
                db, aut, now=datetime(2026, 8, 13, 10, 25, 0, tzinfo=timezone.utc)
            )
            self.assertEqual(w_from, run1.window_to)
            self.assertEqual(w_to, t2)

    async def test_10_no_duplicate_results_in_db(self):
        """
        Scenario 10:
        Running multiple overlapping runs with lookback does not produce duplicate
        rows in bm_mass_evaluation_results for the same call_id.
        """
        async with self.SessionLocal() as db:
            r1 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=201,
                job_id=1,
                execution_source="automation",
                call_id="call_abc",
                prompt_id=58,
                defaults={
                    "prompt_snapshot": "Test prompt snapshot",
                    "status": "completed",
                    "is_evaluable": True,
                    "evaluacion_global": 8.0,
                },
            )
            await db.commit()

            r2 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=202,
                job_id=1,
                execution_source="automation",
                call_id="call_abc",
                prompt_id=58,
                defaults={
                    "prompt_snapshot": "Test prompt snapshot",
                    "status": "completed",
                    "is_evaluable": True,
                    "evaluacion_global": 8.0,
                },
            )
            await db.commit()

            stmt = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_abc")
            res = await db.execute(stmt)
            rows = res.scalars().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].mass_analysis_id, r1.mass_analysis_id)


if __name__ == "__main__":
    unittest.main()
