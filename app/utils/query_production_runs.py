import asyncio
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

MADRID_TZ = ZoneInfo("Europe/Madrid")


def format_dt(dt, target_tz):
    if dt is None:
        return "None"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(target_tz).strftime("%Y-%m-%d %H:%M:%S")


async def main():
    from app.models.mass_evaluations import MassAnalysisAutomationRun, MassEvaluationRun, MassAnalysisAutomation

    db_url = "postgresql+asyncpg://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"
    engine = create_async_engine(db_url, echo=False)
    async with AsyncSession(engine) as db:
        # Get Automation
        aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == 8)
        aut_res = await db.execute(aut_stmt)
        aut = aut_res.scalars().first()
        print("=" * 140)
        print(f"AUTOMATION: ID={aut.automation_id}, Name='{aut.name}', Active={aut.is_active}, Interval={aut.interval_minutes}m, Lookback={aut.lookback_minutes}m, Delay={aut.delay_minutes}m")
        print(f"LAST RUN AT: UTC={format_dt(aut.last_run_at, timezone.utc)} | MADRID={format_dt(aut.last_run_at, MADRID_TZ)}")
        print("=" * 140)

        # Get Runs
        stmt = (
            select(MassAnalysisAutomationRun)
            .where(MassAnalysisAutomationRun.automation_id == 8)
            .order_by(desc(MassAnalysisAutomationRun.automation_run_id))
            .limit(30)
        )
        res = await db.execute(stmt)
        runs = list(reversed(res.scalars().all()))
        print(f"Total runs fetched: {len(runs)}")
        print(f"{'AutoRunID':<10} | {'MassRunID':<10} | {'UTC from':<20} | {'UTC to':<20} | {'Madrid from':<20} | {'Madrid to':<20} | {'Found':<5} | {'Sel':<4} | {'Status':<10} | {'Gap with prev'}")
        print("-" * 140)
        prev = None
        for r in runs:
            gap_sec = (r.window_from - prev.window_to).total_seconds() if (prev and prev.window_to and r.window_from) else 0
            gap_str = "0.0s (CONTINUOUS)" if abs(gap_sec) < 1.0 else f"{gap_sec:+.1f}s GAP"
            print(f"{r.automation_run_id:<10} | {str(r.run_id):<10} | {format_dt(r.window_from, timezone.utc):<20} | {format_dt(r.window_to, timezone.utc):<20} | {format_dt(r.window_from, MADRID_TZ):<20} | {format_dt(r.window_to, MADRID_TZ):<20} | {r.calls_found or 0:<5} | {r.calls_selected or 0:<4} | {r.status:<10} | {gap_str}")
            prev = r

        # Check the underlying MassEvaluationRun details for the latest 5 runs
        print("\n" + "=" * 140)
        print("DETALLES DE LOS ÚLTIMOS 5 MASS EVALUATION RUNS (effective_filters y resultados):")
        print("=" * 140)
        for r in runs[-5:]:
            if r.run_id:
                m_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == r.run_id)
                m_res = await db.execute(m_stmt)
                m_run = m_res.scalars().first()
                if m_run:
                    print(f"\nAutoRun #{r.automation_run_id} -> MassRun #{m_run.run_id}:")
                    print(f"  Status: {m_run.status}, Found: {m_run.calls_found}, Selected: {m_run.calls_selected}, Analyzed: {m_run.calls_analyzed}")
                    print(f"  Effective Filters: {m_run.effective_filters}")
                    print(f"  Error message: {m_run.error_message}")


if __name__ == "__main__":
    asyncio.run(main())
