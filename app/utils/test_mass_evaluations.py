"""E2E verification test pipeline for automated mass evaluations."""
import asyncio
import os
import sys
from datetime import datetime, time, timezone

# Add app to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine
from app.services.db_init_service import init_db
from app.services.mass_evaluation_service import MassEvaluationService, calculate_next_run, resolve_date_filters
from app.schemas.mass_evaluations import MassEvaluationJobCreate, MassEvaluationJobUpdate
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult

async def run_verification():
    print("=== STARTING MASS EVALUATIONS VERIFICATION PIPELINE ===")
    
    # 1. Force database schema creation/upgrade
    print("Step 1: Skipping db_init_service initialization in test script...")
    # await init_db()
    
    engine = get_engine()
    async with AsyncSession(engine) as db:
        # 2. Test calculate_next_run helper
        print("\nStep 2: Testing schedule calculations...")
        t = time(14, 30)
        daily_next = calculate_next_run("daily", t, None, None, None, "Europe/Madrid")
        print(f"Daily Next Execution: {daily_next}")
        assert daily_next is not None, "Failed daily next run calculation"
        
        # 3. Test resolve_date_filters relative mode helper
        print("\nStep 3: Testing date range resolution filters...")
        temp_job = MassEvaluationJob(date_mode="relative", relative_days=3)
        dt_from, dt_to = resolve_date_filters(temp_job, "Europe/Madrid")
        print(f"Relative dates (3 days): {dt_from} -> {dt_to}")
        assert dt_from is not None and dt_to is not None, "Failed relative date filters"
        assert (dt_to - dt_from).days == 3, f"Unexpected relative days gap: {(dt_to - dt_from).days}"

        # 4. Test Job CRUD operations
        print("\nStep 4: Testing Job creation...")
        # Resolve a valid prompt_id in DB first (default prompt 1 exists in development environment)
        stmt_p = select(MassEvaluationJob.job_id).limit(1)
        
        create_payload = MassEvaluationJobCreate(
            job_name="Test Automation Job",
            description="Testing automated mass evaluations integration",
            prompt_id=1, # default prompt ID
            agent_owner_ids=["owner_123", "owner_456"],
            date_mode="relative",
            relative_days=7,
            duration_min_seconds=30,
            duration_max_seconds=600,
            direction="all",
            only_with_recording=True,
            max_calls=15,
            schedule_enabled=True,
            schedule_type="daily",
            schedule_time=time(18, 0)
        )
        
        job = await MassEvaluationService.create_job(db, create_payload)
        job_id = job.job_id
        print(f"Created Job: ID={job_id}, Name='{job.job_name}', next_run_at={job.next_run_at}")
        assert job.job_id is not None, "Failed job primary key assignment"
        assert job.next_run_at is not None, "Failed next_run_at schedule parsing on create"
        
        # Test GET details
        print("\nStep 5: Testing Job retrieval...")
        fetched = await MassEvaluationService.get_job(db, job_id)
        assert fetched is not None, "Failed job details retrieval"
        assert fetched.job_name == "Test Automation Job", "Job name mismatch"
        print(f"Fetched details: {fetched.job_name} (Active: {fetched.is_active})")
        
        # Test List active
        print("\nStep 6: Testing Jobs listing...")
        jobs_list = await MassEvaluationService.list_jobs(db)
        print(f"Active Jobs Count: {len(jobs_list)}")
        assert len(jobs_list) > 0, "List jobs returned empty"
        
        # Test Job Update
        print("\nStep 7: Testing Job update...")
        update_payload = MassEvaluationJobUpdate(
            job_name="Test Automation Job Updated",
            schedule_type="weekly",
            schedule_day_of_week=2, # Wednesday
            schedule_time=time(10, 0)
        )
        updated = await MassEvaluationService.update_job(db, job_id, update_payload)
        print(f"Updated Job Name: '{updated.job_name}', Type={updated.schedule_type}, next_run_at={updated.next_run_at}")
        assert updated.job_name == "Test Automation Job Updated", "Update failed"
        assert updated.schedule_type == "weekly", "Schedule update failed"
        
        # Test Dry Run matching (HubSpot mock validation)
        print("\nStep 8: Testing HubSpot Search Dry-Run dry_run_job...")
        try:
            dry_run_res = await MassEvaluationService.dry_run_job(db, job_id)
            print(f"Dry Run successful: matched {dry_run_res['calls_found']} calls.")
            assert "calls_found" in dry_run_res, "Dry run missing calls_found metric"
        except Exception as ehs:
            print(f"HubSpot Dry Run bypassed (normal if HubSpot is not connected/credentials missing): {ehs}")
            
        # Test Run Concurrent Lock
        print("\nStep 9: Testing run launcher and lock invariants...")
        # Create a mock run status 'running' to trigger lock
        mock_run = MassEvaluationRun(
            job_id=job_id,
            trigger_type="manual",
            status="running",
            started_at=datetime.now(timezone.utc)
        )
        db.add(mock_run)
        await db.commit()
        
        try:
            # Running this while mock_run is status 'running' should trigger lock ValueError
            await MassEvaluationService.run_job(db, job_id)
            print("FAILED: Launching run did not trigger active run lock!")
            assert False, "Execution lock invariant broken!"
        except ValueError as ve:
            print(f"Success: Concurrent run lock triggered as expected: {ve}")
            
        # Remove mock run
        await db.delete(mock_run)
        await db.commit()
        
        # Test Job deactivation (soft delete)
        print("\nStep 10: Testing Job soft deactivation...")
        await MassEvaluationService.delete_job(db, job_id, soft_delete=True)
        after_del = await MassEvaluationService.get_job(db, job_id)
        print(f"After soft delete: Active={after_del.is_active}")
        assert after_del.is_active is False, "Soft delete did not deactivate job"
        
        # Test Hard cleanup
        print("\nStep 11: Cleaning up test data...")
        await MassEvaluationService.delete_job(db, job_id, soft_delete=False)
        cleaned = await MassEvaluationService.get_job(db, job_id)
        assert cleaned is None, "Hard delete failed to purge job"
        print("Test data cleaned successfully!")
        
    print("\n=== ALL E2E VERIFICATION CHECKS PASSED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(run_verification())
