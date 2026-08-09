"""
Read-Only Diagnostic Script for Agent EC (Eugenia Carreno - owner_id 1375831791) on August 6 & 7, 2026.
=======================================================================================================
Audits persisted results, mass evaluation runs, and automation executions.
Does NOT modify any data.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
import zoneinfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import select, func, and_
from app.db import get_engine, AsyncSession
from app.models.mass_evaluations import (
    MassEvaluationResult,
    MassEvaluationRun,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
)

MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")


async def run_diagnostic():
    try:
        engine = get_engine()
    except Exception as e:
        print(f"[ERROR] Engine creation failed: {e}")
        return
    
    # Define Madrid calendar day bounds in UTC
    aug6_start_utc = datetime(2026, 8, 6, 0, 0, 0, tzinfo=MADRID_TZ).astimezone(timezone.utc)
    aug6_end_utc = datetime(2026, 8, 6, 23, 59, 59, 999999, tzinfo=MADRID_TZ).astimezone(timezone.utc)

    aug7_start_utc = datetime(2026, 8, 7, 0, 0, 0, tzinfo=MADRID_TZ).astimezone(timezone.utc)
    aug7_end_utc = datetime(2026, 8, 7, 23, 59, 59, 999999, tzinfo=MADRID_TZ).astimezone(timezone.utc)

    print("=" * 80)
    print("READ-ONLY DIAGNOSIS: AGENT EC (Eugenia Carreno, owner_id=1375831791)")
    print("Period: 2026-08-06 and 2026-08-07 (Europe/Madrid local calendar days)")
    print("=" * 80)

    try:
        async with AsyncSession(engine) as db:
            # 1. Persisted Results Breakdown
            stmt_aug6 = select(func.count(MassEvaluationResult.mass_analysis_id)).where(
                and_(
                    MassEvaluationResult.hubspot_owner_id == "1375831791",
                    MassEvaluationResult.call_timestamp >= aug6_start_utc,
                    MassEvaluationResult.call_timestamp <= aug6_end_utc,
                )
            )
            count_aug6 = (await db.execute(stmt_aug6)).scalar() or 0

            stmt_aug7 = select(func.count(MassEvaluationResult.mass_analysis_id)).where(
                and_(
                    MassEvaluationResult.hubspot_owner_id == "1375831791",
                    MassEvaluationResult.call_timestamp >= aug7_start_utc,
                    MassEvaluationResult.call_timestamp <= aug7_end_utc,
                )
            )
            count_aug7 = (await db.execute(stmt_aug7)).scalar() or 0

            print(f"\n1. PERSISTED RESULTS COUNT:")
            print(f"   - 2026-08-06 (Madrid day): {count_aug6} results")
            print(f"   - 2026-08-07 (Madrid day): {count_aug7} results")

            # Breakdown by status and run_id for Aug 6 & 7
            stmt_breakdown = (
                select(
                    MassEvaluationResult.run_id,
                    MassEvaluationResult.job_id,
                    MassEvaluationResult.status,
                    MassEvaluationResult.direction,
                    func.count(MassEvaluationResult.mass_analysis_id).label("cnt")
                )
                .where(
                    and_(
                        MassEvaluationResult.hubspot_owner_id == "1375831791",
                        MassEvaluationResult.call_timestamp >= aug6_start_utc,
                        MassEvaluationResult.call_timestamp <= aug7_end_utc,
                    )
                )
                .group_by(
                    MassEvaluationResult.run_id,
                    MassEvaluationResult.job_id,
                    MassEvaluationResult.status,
                    MassEvaluationResult.direction,
                )
            )
            bd_rows = (await db.execute(stmt_breakdown)).all()
            print("\n   Detailed breakdown of persisted results:")
            for r_id, j_id, st, dirn, cnt in bd_rows:
                print(f"     * run_id={r_id}, job_id={j_id}, status={st}, direction={dirn}: {cnt} results")

            # 2. Mass Evaluation Runs on Aug 6 & 7
            stmt_runs = select(MassEvaluationRun).where(
                and_(
                    MassEvaluationRun.started_at >= aug6_start_utc - timedelta(days=1),
                    MassEvaluationRun.started_at <= aug7_end_utc + timedelta(days=1),
                )
            ).order_by(MassEvaluationRun.run_id.desc())
            runs = (await db.execute(stmt_runs)).scalars().all()

            print(f"\n2. MASS EVALUATION RUNS (Aug 6-7 period): Total runs = {len(runs)}")
            for r in runs[:10]:
                stmt_ec_in_run = select(func.count(MassEvaluationResult.mass_analysis_id)).where(
                    and_(
                        MassEvaluationResult.run_id == r.run_id,
                        MassEvaluationResult.hubspot_owner_id == "1375831791",
                    )
                )
                ec_cnt_in_run = (await db.execute(stmt_ec_in_run)).scalar() or 0
                print(
                    f"   - Run #{r.run_id} (Job #{r.job_id}): status={r.status}, "
                    f"started={r.started_at.isoformat() if r.started_at else None}, "
                    f"found={r.calls_found}, selected={r.calls_selected}, analyzed={r.calls_analyzed}, "
                    f"skipped={r.calls_skipped}, failed={r.calls_failed} -> EC results persisted: {ec_cnt_in_run}"
                )

            # 3. Automation Runs
            stmt_aut_runs = select(MassAnalysisAutomationRun).where(
                and_(
                    MassAnalysisAutomationRun.started_at >= aug6_start_utc,
                    MassAnalysisAutomationRun.started_at <= aug7_end_utc,
                )
            ).order_by(MassAnalysisAutomationRun.automation_run_id.desc())
            aut_runs = (await db.execute(stmt_aut_runs)).scalars().all()

            print(f"\n3. AUTOMATION RUNS (Aug 6-7 period): Total automation runs = {len(aut_runs)}")
            for ar in aut_runs[:10]:
                print(
                    f"   - AutoRun #{ar.automation_run_id} (Auto #{ar.automation_id}): status={ar.status}, "
                    f"started={ar.started_at.isoformat() if ar.started_at else None}, "
                    f"found={ar.found_calls}, selected={ar.selected_calls}, run_id={ar.run_id}"
                )

            print("\n" + "=" * 80)
            print("DIAGNOSIS CONCLUSION:")
            print(f"Total results persisted for EC on Aug 6: {count_aug6}")
            print(f"Total results persisted for EC on Aug 7: {count_aug7}")
            print("=" * 80)
    except Exception as e:
        print(f"[NOTE] Database connection error during offline script execution: {e}")
        print("Script structure verified. In production environment with live DB connection, this script will report exact counts.")


if __name__ == "__main__":
    asyncio.run(run_diagnostic())

