"""
Historical Reconciliation Script for False Failed Stale Runs (ERR-02).
======================================================================
Reconciles runs marked 'failed' due to legacy heartbeat abandonment when their
call results were already evaluated and persisted into bm_mass_evaluation_results.

Default targets: Runs #53, #80, #81, #82, #84, #120.

Modes:
  - DRY-RUN (default): Safe preview, no modifications.
  - EXECUTE: Modifies only metadata/status when --confirm-execute=CONFIRM_RECONCILE_ABANDONED is passed.

Usage:
  # Safe dry-run preview:
  python app/utils/reconcile_abandoned_runs.py --dry-run

  # Execute reconciliation with confirmation:
  python app/utils/reconcile_abandoned_runs.py --execute --confirm-execute CONFIRM_RECONCILE_ABANDONED
"""
import sys
import argparse
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, text, update, func, case
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.models.mass_evaluations import (
    MassEvaluationRun,
    MassEvaluationResult,
    MassAnalysisAutomationRun,
    MassAnalysisAutomation,
)

MADRID_TZ = ZoneInfo("Europe/Madrid")
logger = logging.getLogger("reconcile_abandoned_runs")

DEFAULT_TARGET_RUN_IDS = [53, 80, 81, 82, 84, 120]


async def plan_reconciliation(db: AsyncSession, run_ids: list[int]) -> list[dict]:
    plans = []
    for rid in run_ids:
        # 1. Fetch current run
        stmt_run = select(MassEvaluationRun).where(MassEvaluationRun.run_id == rid)
        res_run = await db.execute(stmt_run)
        run = res_run.scalar()
        if not run:
            logger.warning("Run ID %d not found in DB.", rid)
            continue

        # 2. Count actual results
        stmt_res = select(
            func.count(MassEvaluationResult.mass_analysis_id).label("total"),
            func.count(case((MassEvaluationResult.status == "completed", 1))).label("completed"),
            func.count(case((MassEvaluationResult.status == "failed", 1))).label("failed"),
            func.count(case((MassEvaluationResult.status == "skipped", 1))).label("skipped"),
        ).where(MassEvaluationResult.run_id == rid)
        res_counts = (await db.execute(stmt_res)).first()

        total_res = res_counts.total if res_counts else 0
        completed_res = res_counts.completed if res_counts else 0
        failed_res = res_counts.failed if res_counts else 0
        skipped_res = res_counts.skipped if res_counts else 0

        # Determine proposed status
        if completed_res > 0 and failed_res == 0:
            proposed_status = "completed"
            proposed_error = None
        elif completed_res > 0 and failed_res > 0:
            proposed_status = "completed_with_errors"
            proposed_error = f"Completed with {failed_res} error(s) before heartbeat timeout."
        else:
            proposed_status = run.status
            proposed_error = run.error_message

        # Check linked auto_run
        stmt_auto = select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.run_id == rid)
        res_auto = await db.execute(stmt_auto)
        auto_run = res_auto.scalar()

        plans.append({
            "run_id": rid,
            "job_id": run.job_id,
            "current_status": run.status,
            "current_error": run.error_message,
            "current_analyzed": run.calls_analyzed or 0,
            "current_failed": run.calls_failed or 0,
            "total_results_db": total_res,
            "completed_results_db": completed_res,
            "failed_results_db": failed_res,
            "skipped_results_db": skipped_res,
            "proposed_status": proposed_status,
            "proposed_error": proposed_error,
            "proposed_analyzed": max(run.calls_analyzed or 0, completed_res + failed_res),
            "proposed_failed": failed_res,
            "has_auto_run": auto_run is not None,
            "auto_run_id": auto_run.automation_run_id if auto_run else None,
            "needs_update": run.status != proposed_status,
        })
    return plans


async def execute_reconciliation(db: AsyncSession, plans: list[dict]) -> list[dict]:
    executed = []
    now_utc = datetime.now(timezone.utc)
    for p in plans:
        if not p["needs_update"]:
            executed.append(p)
            continue

        rid = p["run_id"]
        # Update MassEvaluationRun
        await db.execute(
            update(MassEvaluationRun).where(MassEvaluationRun.run_id == rid).values(
                status=p["proposed_status"],
                error_message=p["proposed_error"],
                calls_analyzed=p["proposed_analyzed"],
                calls_failed=p["proposed_failed"],
                finished_at=MassEvaluationRun.finished_at or now_utc,
            )
        )

        # Update linked MassAnalysisAutomationRun if exists
        if p["has_auto_run"] and p["auto_run_id"]:
            await db.execute(
                update(MassAnalysisAutomationRun).where(
                    MassAnalysisAutomationRun.automation_run_id == p["auto_run_id"]
                ).values(
                    status=p["proposed_status"],
                    error_message=p["proposed_error"],
                    finished_at=MassAnalysisAutomationRun.finished_at or now_utc,
                )
            )

        executed.append(p)

    await db.commit()
    return executed


async def main():
    parser = argparse.ArgumentParser(description="Reconcile false failed stale mass evaluation runs.")
    parser.add_argument("--run-ids", type=str, default=None, help="Comma-separated run IDs to reconcile (default: 53,80,81,82,84,120)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview changes without writing to DB (default: True)")
    parser.add_argument("--execute", action="store_true", help="Execute the status and metric reconciliation")
    parser.add_argument("--confirm-execute", type=str, default="", help="Safety confirmation string: CONFIRM_RECONCILE_ABANDONED")

    args = parser.parse_args()

    if args.run_ids:
        target_ids = [int(x.strip()) for x in args.run_ids.split(",") if x.strip().isdigit()]
    else:
        target_ids = DEFAULT_TARGET_RUN_IDS

    import os
    from app.config import get_settings
    settings = get_settings()
    db_url = os.environ.get("DATABASE_URL") or settings.database_url
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if "localhost" in db_url or "127.0.0.1" in db_url:
        db_url = "postgresql+asyncpg://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"
    engine = create_async_engine(db_url, echo=False)

    async with AsyncSession(engine) as db:
        plans = await plan_reconciliation(db, target_ids)

        is_execute = args.execute and args.confirm_execute == "CONFIRM_RECONCILE_ABANDONED"
        mode_str = "EXECUTION (MUTACIONES REALES)" if is_execute else "DRY-RUN (SOLO LECTURA)"

        print("=" * 135)
        print(f"RECONCILIACIÓN HISTÓRICA DE RUNS FALSOS FAILED (ERR-02) - MODO: {mode_str}")
        print("=" * 135)
        print(f"{'Run#':<6} | {'Job#':<6} | {'Status Actual':<15} | {'Total DB':<9} | {'Comp DB':<8} | {'Fail DB':<8} | {'Status Propuesto':<23} | {'Acción'}")
        print("-" * 135)

        for p in plans:
            action = "ACTUALIZAR" if p["needs_update"] else "SIN CAMBIO"
            print(
                f"{p['run_id']:<6} | {p['job_id']:<6} | {p['current_status']:<15} | {p['total_results_db']:<9} | "
                f"{p['completed_results_db']:<8} | {p['failed_results_db']:<8} | {p['proposed_status']:<23} | {action}"
            )

        print("=" * 135)

        if args.execute:
            if args.confirm_execute != "CONFIRM_RECONCILE_ABANDONED":
                print("[SAFETY ABORT] --execute requiere --confirm-execute=CONFIRM_RECONCILE_ABANDONED. Operación cancelada.")
                sys.exit(1)

            print(f"[EXECUTION] Aplicando reconciliación a {len([p for p in plans if p['needs_update']])} run(s)...")
            await execute_reconciliation(db, plans)
            print("[EXECUTION] Reconciliación completada exitosamente.")
        else:
            print("[INFO] Modo Dry-Run finalizado. Ningún dato ha sido modificado en la base de datos.")


if __name__ == "__main__":
    asyncio.run(main())
