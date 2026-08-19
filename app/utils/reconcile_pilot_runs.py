"""
Reconcile pilot backfill runs in production database:
- MassRun 1729 / AutoRun 1650: verify completed results (2/2) and restore status='completed', error_message=None.
- MassRun 1730 / AutoRun 1651: verify status='completed' with 0 calls.
"""
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from app.models.mass_evaluations import MassAnalysisAutomationRun, MassEvaluationRun, MassEvaluationResult

async def reconcile():
    db_url = "postgresql+asyncpg://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"
    engine = create_async_engine(db_url, echo=False)
    async with AsyncSession(engine) as db:
        print("=== RECONCILING PILOT RUNS IN PRODUCTION DB ===\n")

        # 1. Reconcile MassRun 1729 & AutoRun 1650
        q_res = select(MassEvaluationResult).where(MassEvaluationResult.run_id == 1729)
        results = (await db.execute(q_res)).scalars().all()
        print(f"MassRun 1729 has {len(results)} results in bm_mass_evaluation_results.")
        for r in results:
            print(f"  Result #{r.mass_analysis_id}: call_id={r.call_id}, status={r.status}, score={r.evaluacion_global}")

        if len(results) == 2 and all(r.status == "completed" for r in results):
            print("\nReconciling MassEvaluationRun 1729 -> status='completed', error_message=None...")
            await db.execute(
                update(MassEvaluationRun)
                .where(MassEvaluationRun.run_id == 1729)
                .values(
                    status="completed",
                    error_message=None,
                    calls_found=2,
                    calls_selected=2,
                    calls_analyzed=2,
                    calls_failed=0,
                    calls_skipped=0
                )
            )

            print("Reconciling MassAnalysisAutomationRun 1650 -> status='completed', error_message=None...")
            await db.execute(
                update(MassAnalysisAutomationRun)
                .where(MassAnalysisAutomationRun.automation_run_id == 1650)
                .values(
                    status="completed",
                    error_message=None,
                    calls_found=2,
                    calls_selected=2,
                    calls_skipped=0
                )
            )
            await db.commit()
            print("Reconciliation of MassRun 1729 / AutoRun 1650 committed successfully.")
        else:
            print("WARNING: Unexpected results count or status for MassRun 1729. Skipping update.")

        # 2. Check MassRun 1730 & AutoRun 1651
        m_run_1730 = (await db.execute(select(MassEvaluationRun).where(MassEvaluationRun.run_id == 1730))).scalars().first()
        a_run_1651 = (await db.execute(select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == 1651))).scalars().first()
        print(f"\nMassRun 1730 status: {m_run_1730.status}, found={m_run_1730.calls_found}, analyzed={m_run_1730.calls_analyzed}")
        print(f"AutoRun 1651 status: {a_run_1651.status}, found={a_run_1651.calls_found}")

        print("\n=== FINAL VERIFICATION AFTER RECONCILIATION ===")
        m_1729_post = (await db.execute(select(MassEvaluationRun).where(MassEvaluationRun.run_id == 1729))).scalars().first()
        a_1650_post = (await db.execute(select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == 1650))).scalars().first()
        print(f"MassRun 1729: status={m_1729_post.status}, error_message={m_1729_post.error_message}")
        print(f"AutoRun 1650: status={a_1650_post.status}, error_message={a_1650_post.error_message}")

if __name__ == "__main__":
    asyncio.run(reconcile())
