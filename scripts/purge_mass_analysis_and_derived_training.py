#!/usr/bin/env python
"""
Script to purge mass evaluation history and derived training data from the database.
Ensures FK-safe deletion in reverse dependency order, makes backups, and performs pre/post safety checks.
"""
import sys
import os
import argparse
import asyncio
import subprocess
import json
from datetime import datetime, timezone
from urllib.parse import urlparse
from decimal import Decimal

# Ensure app is importable
sys.path.insert(0, os.path.abspath("."))

from app.config import get_settings
from app.db import get_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

# Import all models to ensure metadata is populated and we can query/delete
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun
)
from app.models.personalized_training import (
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingSchedulerSetting,
    TrainingAgentSetting,
    TrainingEvaluationPrompt,
    TrainingCallSession,
    TrainingCallEvaluation
)
from app.models.analyses import (
    Analysis,
    CallAnalysisCurrent,
    AnalysisResult,
    AnalysisCriterionResult
)
from app.models.users import (
    User,
    UserAudit
)


CONFIRMATION_STRING = "DELETE_MASS_ANALYSIS_AND_DERIVED_TRAINING"

# Affected tables for purge
AFFECTED_TABLES = [
    "bm_training_call_evaluations",
    "bm_training_call_sessions",
    "bm_training_completion_status",
    "bm_training_simulation_prompts",
    "bm_training_agent_reports",
    "bm_training_runs",
    "bm_mass_evaluation_criterion_results",
    "bm_mass_evaluation_results",
    "bm_mass_evaluation_runs",
    "bm_mass_evaluation_jobs",
    "bm_mass_analysis_automation_runs",
    "bm_mass_analysis_automations"
]

# Preserved tables to verify safety
PRESERVED_TABLES = [
    "bm_analyses",
    "bm_analysis_criterion_results",
    "bm_call_analysis_current",
    "bm_users",
    "bm_training_agent_settings",
    "bm_training_scheduler_settings",
    "bm_training_evaluation_prompts",
    "bm_user_audits"
]


def serialize_value(v):
    """Helper to serialize values to JSON-friendly format."""
    if isinstance(v, Decimal):
        return str(v)
    elif hasattr(v, "isoformat"):
        return v.isoformat()
    return v


async def perform_python_backup(db: AsyncSession, backup_path: str):
    """Queries all database tables and serializes them to JSON as a robust fallback backup."""
    print(f"Creating Python JSON fallback backup at {backup_path}...")
    backup_data = {}
    
    models_to_backup = [
        MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult,
        MassAnalysisAutomation, MassAnalysisAutomationRun, TrainingRun, TrainingAgentReport,
        TrainingSimulationPrompt, TrainingCompletionStatus, TrainingCallSession, TrainingCallEvaluation,
        Analysis, CallAnalysisCurrent, AnalysisResult, AnalysisCriterionResult,
        TrainingAgentSetting, TrainingSchedulerSetting, TrainingEvaluationPrompt, User, UserAudit
    ]
    
    for model in models_to_backup:
        tbl_name = model.__tablename__
        stmt = select(model)
        result = await db.execute(stmt)
        rows = result.scalars().all()
        
        serialized_rows = []
        for row in rows:
            row_dict = {}
            for col in model.__table__.columns:
                val = getattr(row, col.name)
                row_dict[col.name] = serialize_value(val)
            serialized_rows.append(row_dict)
            
        backup_data[tbl_name] = serialized_rows
        
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, indent=2, ensure_ascii=False)
    print("Python JSON backup completed successfully.")


def run_pg_dump(db_url: str, backup_path: str) -> bool:
    """Runs pg_dump command to create a standard SQL backup of the affected and preserved tables."""
    try:
        parsed = urlparse(db_url)
        username = parsed.username
        password = parsed.password
        host = parsed.hostname
        port = parsed.port or 5432
        database = parsed.path.lstrip('/')
        
        # We specify both affected and preserved tables to have a complete snapshot
        tables = AFFECTED_TABLES + PRESERVED_TABLES
        
        pg_dump_paths = [
            r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe",
            "pg_dump"
        ]
        
        pg_dump_exe = None
        for path in pg_dump_paths:
            try:
                res = subprocess.run([path, "--version"], capture_output=True, text=True)
                if res.returncode == 0:
                    pg_dump_exe = path
                    break
            except Exception:
                continue
                
        if not pg_dump_exe:
            print("WARNING: pg_dump executable not found on path or in default PostgreSQL installation. Skipping pg_dump.")
            return False
            
        cmd = [
            pg_dump_exe,
            "-h", host,
            "-p", str(port),
            "-U", username,
            "-d", database,
            "-F", "c",
            "-f", backup_path
        ]
        for t in tables:
            cmd.extend(["-t", t])
            
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        
        print(f"Running pg_dump command using {pg_dump_exe}...")
        res = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"pg_dump failed with exit code {res.returncode}: {res.stderr}")
            return False
            
        print(f"pg_dump backup completed successfully and saved to {backup_path}")
        return True
    except Exception as e:
        print(f"Error executing pg_dump: {e}")
        return False


async def get_db_counts(db: AsyncSession):
    """Retrieves current row counts for all affected and preserved tables."""
    counts = {}
    
    # Deleted tables
    counts["bm_training_call_evaluations"] = (await db.execute(select(func.count()).select_from(TrainingCallEvaluation))).scalar()
    counts["bm_training_call_sessions"] = (await db.execute(select(func.count()).select_from(TrainingCallSession))).scalar()
    counts["bm_training_completion_status"] = (await db.execute(select(func.count()).select_from(TrainingCompletionStatus))).scalar()
    counts["bm_training_simulation_prompts"] = (await db.execute(select(func.count()).select_from(TrainingSimulationPrompt))).scalar()
    counts["bm_training_agent_reports"] = (await db.execute(select(func.count()).select_from(TrainingAgentReport))).scalar()
    counts["bm_training_runs"] = (await db.execute(select(func.count()).select_from(TrainingRun))).scalar()
    counts["bm_mass_evaluation_criterion_results"] = (await db.execute(select(func.count()).select_from(MassEvaluationCriterionResult))).scalar()
    counts["bm_mass_evaluation_results"] = (await db.execute(select(func.count()).select_from(MassEvaluationResult))).scalar()
    counts["bm_mass_evaluation_runs"] = (await db.execute(select(func.count()).select_from(MassEvaluationRun))).scalar()
    counts["bm_mass_evaluation_jobs"] = (await db.execute(select(func.count()).select_from(MassEvaluationJob))).scalar()
    counts["bm_mass_analysis_automation_runs"] = (await db.execute(select(func.count()).select_from(MassAnalysisAutomationRun))).scalar()
    counts["bm_mass_analysis_automations"] = (await db.execute(select(func.count()).select_from(MassAnalysisAutomation))).scalar()
    
    # Preserved tables
    counts["bm_analyses"] = (await db.execute(select(func.count()).select_from(Analysis))).scalar()
    counts["bm_analysis_criterion_results"] = (await db.execute(select(func.count()).select_from(AnalysisCriterionResult))).scalar()
    counts["bm_call_analysis_current"] = (await db.execute(select(func.count()).select_from(CallAnalysisCurrent))).scalar()
    counts["bm_users"] = (await db.execute(select(func.count()).select_from(User))).scalar()
    counts["bm_training_agent_settings"] = (await db.execute(select(func.count()).select_from(TrainingAgentSetting))).scalar()
    counts["bm_training_scheduler_settings"] = (await db.execute(select(func.count()).select_from(TrainingSchedulerSetting))).scalar()
    counts["bm_training_evaluation_prompts"] = (await db.execute(select(func.count()).select_from(TrainingEvaluationPrompt))).scalar()
    counts["bm_user_audits"] = (await db.execute(select(func.count()).select_from(UserAudit))).scalar()
    
    return counts


async def verify_preserved_users(db: AsyncSession):
    """Verifies that the core 9 users are preserved, specifically HubSpot IDs for Fernanda and Luci."""
    expected_users = {
        "1539993532": "Fernanda Rodrigues",
        "1375831790": "Luci Dos Santos Furtado"
    }
    for owner_id, name in expected_users.items():
        stmt = select(User).where(User.hubspot_owner_id == owner_id)
        user = (await db.execute(stmt)).scalars().first()
        if not user:
            raise ValueError(f"Safety Check Failed: Expected user '{name}' with HubSpot ID '{owner_id}' not found.")
        print(f"Verified preserved user: {user.name} (HubSpot ID: {user.hubspot_owner_id})")


async def insert_audit_log(db: AsyncSession, changes: dict):
    """Inserts a record in the bm_user_audits table mapping to an existing admin user."""
    from app.models.users import User, UserAudit
    
    stmt = select(User).where(User.is_active == True)
    result = await db.execute(stmt)
    users = result.scalars().all()
    if not users:
        print("WARNING: No active user found for audit log.")
        return
        
    admin_user = next((u for u in users if u.role in ("administrador", "admin")), users[0])
    
    audit = UserAudit(
        admin_user_id=admin_user.user_id,
        target_user_id=admin_user.user_id,
        action="purge_mass_evaluation",
        changes_json={
            "description": "Purged all mass evaluation jobs, runs, results and derived training data.",
            "deleted_records": changes,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    )
    db.add(audit)
    await db.flush()
    print(f"Audit log entry created for user {admin_user.username} (ID {admin_user.user_id})")


async def execute_purge(db: AsyncSession, execute: bool, dry_run: bool):
    """Performs safety checks, backup, and deletion in a safe transaction."""
    counts = await get_db_counts(db)
    
    print("\n=== CURRENT DATABASE COUNTS ===")
    print("Tables to be purged:")
    for t in AFFECTED_TABLES:
        print(f"  - {t}: {counts[t]}")
        
    print("\nTables to be preserved:")
    for t in PRESERVED_TABLES:
        print(f"  - {t}: {counts[t]}")
        
    # Verify core users exist before deletion
    await verify_preserved_users(db)
    
    if dry_run or not execute:
        print("\n[DRY RUN] No records were deleted. Run with --execute --confirm DELETE_MASS_ANALYSIS_AND_DERIVED_TRAINING to commit changes.")
        return counts

    # Execution phase
    print("\nStarting execution phase...")
    
    # 1. Create backup dir
    os.makedirs("backups", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    pg_backup_path = f"backups/db_backup_{ts}.dump"
    json_backup_path = f"backups/db_backup_{ts}_fallback.json"
    
    # Save fallback json backup first
    await perform_python_backup(db, json_backup_path)
    
    # Try pg_dump backup
    settings = get_settings()
    pg_dump_success = run_pg_dump(settings.database_url, pg_backup_path)
    
    # Require at least one backup to succeed
    if not os.path.exists(json_backup_path) and not pg_dump_success:
        raise RuntimeError("CRITICAL ERROR: Backup failed completely. Aborting execution for safety.")
        
    # Double check that we have a preservation baseline
    base_analyses = counts["bm_analyses"]
    base_criteria = counts["bm_analysis_criterion_results"]
    base_current = counts["bm_call_analysis_current"]
    base_users = counts["bm_users"]
    
    # 2. Safety constraint validation
    if base_analyses < 94:
        raise ValueError(f"Safety Check Failed: bm_analyses count ({base_analyses}) is less than expected baseline (94).")
    if base_criteria < 4466:
        raise ValueError(f"Safety Check Failed: bm_analysis_criterion_results count ({base_criteria}) is less than expected baseline (4466).")
    if base_current < 14:
        raise ValueError(f"Safety Check Failed: bm_call_analysis_current count ({base_current}) is less than expected baseline (14).")
    if base_users < 9:
        raise ValueError(f"Safety Check Failed: bm_users count ({base_users}) is less than expected baseline (9).")

    print("\nBaseline verification passed. Executing deletions in reverse-dependency order inside transaction...")
    
    # Deletion operations
    # 1. Training evaluations
    res = await db.execute(delete(TrainingCallEvaluation))
    print(f"  - Deleted bm_training_call_evaluations: {res.rowcount} rows")
    
    # 2. Training call sessions
    res = await db.execute(delete(TrainingCallSession))
    print(f"  - Deleted bm_training_call_sessions: {res.rowcount} rows")
    
    # 3. Training completion status
    res = await db.execute(delete(TrainingCompletionStatus))
    print(f"  - Deleted bm_training_completion_status: {res.rowcount} rows")
    
    # 4. Training simulation prompts
    res = await db.execute(delete(TrainingSimulationPrompt))
    print(f"  - Deleted bm_training_simulation_prompts: {res.rowcount} rows")
    
    # 5. Training agent reports
    res = await db.execute(delete(TrainingAgentReport))
    print(f"  - Deleted bm_training_agent_reports: {res.rowcount} rows")
    
    # 6. Training runs
    res = await db.execute(delete(TrainingRun))
    print(f"  - Deleted bm_training_runs: {res.rowcount} rows")
    
    # 7. Mass evaluation criterion results
    res = await db.execute(delete(MassEvaluationCriterionResult))
    print(f"  - Deleted bm_mass_evaluation_criterion_results: {res.rowcount} rows")
    
    # 8. Mass evaluation results
    res = await db.execute(delete(MassEvaluationResult))
    print(f"  - Deleted bm_mass_evaluation_results: {res.rowcount} rows")
    
    # 9. Mass evaluation runs
    res = await db.execute(delete(MassEvaluationRun))
    print(f"  - Deleted bm_mass_evaluation_runs: {res.rowcount} rows")
    
    # 10. Mass evaluation jobs
    res = await db.execute(delete(MassEvaluationJob))
    print(f"  - Deleted bm_mass_evaluation_jobs: {res.rowcount} rows")
    
    # 11. Mass analysis automation runs
    res = await db.execute(delete(MassAnalysisAutomationRun))
    print(f"  - Deleted bm_mass_analysis_automation_runs: {res.rowcount} rows")
    
    # 12. Mass analysis automations
    res = await db.execute(delete(MassAnalysisAutomation))
    print(f"  - Deleted bm_mass_analysis_automations: {res.rowcount} rows")

    # Post-deletion checks
    print("\nRunning post-deletion validation...")
    post_counts = await get_db_counts(db)
    
    # Verify deleted tables are fully emptied
    for t in AFFECTED_TABLES:
        if post_counts[t] != 0:
            raise ValueError(f"Validation Failed: Table '{t}' still has {post_counts[t]} records after deletion.")
            
    # Verify preserved tables are intact
    if post_counts["bm_analyses"] != base_analyses:
        raise ValueError(f"Validation Failed: bm_analyses count changed from {base_analyses} to {post_counts['bm_analyses']}.")
    if post_counts["bm_analysis_criterion_results"] != base_criteria:
        raise ValueError(f"Validation Failed: bm_analysis_criterion_results count changed from {base_criteria} to {post_counts['bm_analysis_criterion_results']}.")
    if post_counts["bm_call_analysis_current"] != base_current:
        raise ValueError(f"Validation Failed: bm_call_analysis_current count changed from {base_current} to {post_counts['bm_call_analysis_current']}.")
    if post_counts["bm_users"] != base_users:
        raise ValueError(f"Validation Failed: bm_users count changed from {base_users} to {post_counts['bm_users']}.")
        
    await verify_preserved_users(db)
    
    # Create audit record
    deleted_summary = {t: counts[t] for t in AFFECTED_TABLES}
    await insert_audit_log(db, deleted_summary)
    
    print("\nAll post-deletion checks passed successfully! Ready to commit transaction.")
    return deleted_summary


async def main():
    parser = argparse.ArgumentParser(description="Purge mass evaluations and derived training data safely.")
    parser.add_argument("--dry-run", action="store_true", help="Perform pre-checks and count records without deletion.")
    parser.add_argument("--execute", action="store_true", help="Execute the deletion transaction.")
    parser.add_argument("--confirm", type=str, help="Required safety confirmation string to execute.")
    
    args = parser.parse_args()
    
    # Set default behavior if no flags provided
    if not args.dry_run and not args.execute:
        args.dry_run = True
        
    if args.execute:
        if not args.confirm or args.confirm != CONFIRMATION_STRING:
            print(f"ERROR: Execution requires confirmation string: --confirm {CONFIRMATION_STRING}")
            sys.exit(1)
            
    engine = get_engine()
    
    try:
        async with AsyncSession(engine) as db:
            async with db.begin():
                result = await execute_purge(db, args.execute, args.dry_run)
                if args.execute:
                    print("\nTRANSACTION COMMITTED SUCCESSFULLY!")
                    print(f"Summary of purged records: {json.dumps(result, indent=2)}")
                else:
                    print("\nDRY RUN COMPLETED SUCCESSFULLY! No changes were made.")
    except Exception as e:
        print(f"\nCRITICAL ERROR ENCOUNTERED: {e}")
        print("TRANSACTION ROLLED BACK. No changes were committed to the database.")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
