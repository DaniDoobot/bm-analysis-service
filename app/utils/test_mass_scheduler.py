"""Verification test script for Mass Evaluations Scheduler."""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

# Add app to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine
from app.services.mass_evaluation_service import MassEvaluationService
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun
from app.schemas.mass_evaluations import MassEvaluationJobCreate
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

async def run_scheduler_verification():
    print("=== STARTING MASS EVALUATIONS SCHEDULER VERIFICATION ===")
    
    # 1. Check if database is configured
    try:
        engine = get_engine()
    except RuntimeError as re:
        print(f"Skipping DB verification (DATABASE_URL is not set): {re}")
        return

    async with AsyncSession(engine) as db:
        # Create a job that is active and scheduled for a minute ago (due now)
        print("\nStep 1: Creating a test due scheduled job...")
        now = datetime.now(timezone.utc)
        due_time = now - timedelta(minutes=5)
        
        # We manually create a job record with schedule settings
        job = MassEvaluationJob(
            job_name="Scheduler Test Due Job",
            prompt_id=1,
            is_active=True,
            schedule_enabled=True,
            schedule_type="cron",
            schedule_cron="*/5 * * * *", # Every 5 minutes
            timezone="UTC",
            next_run_at=due_time,
            max_calls=1,
            created_by="Tester"
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        
        print(f"Created Job: ID={job.job_id}, next_run_at={job.next_run_at}, is_active={job.is_active}")
        
        # Call run_due_jobs
        print("\nStep 2: Triggering run_due_jobs()...")
        stats = await MassEvaluationService.run_due_jobs(db)
        print(f"Trigger stats: {stats}")
        
        # Verify job scheduling fields were updated immediately
        await db.refresh(job)
        print(f"Job scheduling after trigger: last_run_at={job.last_run_at}, next_run_at={job.next_run_at}")
        assert job.last_run_at is not None, "last_run_at was not updated!"
        assert job.next_run_at > now, "next_run_at was not advanced to the future!"
        
        # Verify run record was created
        stmt_run = select(MassEvaluationRun).where(MassEvaluationRun.job_id == job.job_id)
        res_run = await db.execute(stmt_run)
        runs = res_run.scalars().all()
        print(f"Runs spawned for this job: {len(runs)}")
        assert len(runs) == 1, "Run was not spawned!"
        run = runs[0]
        print(f"Spawned Run: ID={run.run_id}, status={run.status}, trigger_type={run.trigger_type}")
        assert run.trigger_type == "scheduled", "Trigger type is not 'scheduled'!"
        
        # Step 3: Trigger run_due_jobs() again immediately and ensure no duplicate runs are spawned
        print("\nStep 3: Triggering run_due_jobs() again immediately...")
        # Artificially set next_run_at to the past again to make it due, but with the active run still "running"
        job.next_run_at = now - timedelta(minutes=1)
        await db.commit()
        
        stats2 = await MassEvaluationService.run_due_jobs(db)
        print(f"Immediate second trigger stats: {stats2}")
        assert stats2["launched_jobs_count"] == 0, "Launched a job when a run is already active!"
        
        # Cleanup
        print("\nStep 4: Cleaning up database records...")
        await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.job_id == job.job_id))
        await db.delete(job)
        await db.commit()
        
        print("\n=== MASS EVALUATIONS SCHEDULER VERIFICATION COMPLETED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(run_scheduler_verification())
