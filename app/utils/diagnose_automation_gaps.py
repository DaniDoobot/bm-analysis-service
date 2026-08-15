"""
Diagnose historical gaps between automation runs.
================================================
Scans MassAnalysisAutomationRun history for an automation and detects all
uncovered time intervals (gaps where prev.window_to < next.window_from).
Categorizes by Weekdays vs Weekends/Holidays and Working Hours vs Off-Hours.
"""
import os
import sys
import argparse
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

MADRID_TZ = ZoneInfo("Europe/Madrid")


def format_dt(dt: datetime | None, target_tz: ZoneInfo | timezone) -> str:
    if dt is None:
        return "None"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(target_tz).strftime("%Y-%m-%d %H:%M:%S")


def is_working_day(dt_madrid: datetime) -> bool:
    """Return True if Monday to Friday and not Aug 15 national holiday."""
    # 1=Monday, ..., 5=Friday, 6=Saturday, 7=Sunday
    isoweekday = dt_madrid.isoweekday()
    if isoweekday in (6, 7):
        return False
    # August 15 is national holiday in Spain
    if dt_madrid.month == 8 and dt_madrid.day == 15:
        return False
    return True


def is_working_hours(dt_madrid: datetime) -> bool:
    """Return True if between 08:00 and 20:00 Madrid time."""
    return 8 <= dt_madrid.hour < 20


async def scan_automation_gaps(
    db: AsyncSession,
    automation_id: int = 8,
    min_gap_seconds: float = 60.0,
    days_back: int = 7
) -> dict[str, Any]:
    from app.models.mass_evaluations import MassAnalysisAutomation, MassAnalysisAutomationRun

    aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == automation_id)
    aut_res = await db.execute(aut_stmt)
    aut = aut_res.scalars().first()
    if not aut:
        return {"error": f"Automation {automation_id} not found"}

    valid_status_list = ["completed", "completed_empty", "completed_with_errors"]
    runs_stmt = (
        select(MassAnalysisAutomationRun)
        .where(
            MassAnalysisAutomationRun.automation_id == automation_id,
            MassAnalysisAutomationRun.status.in_(valid_status_list),
            MassAnalysisAutomationRun.window_from.isnot(None),
            MassAnalysisAutomationRun.window_to.isnot(None),
            MassAnalysisAutomationRun.window_from >= cutoff_utc,
        )
        .order_by(MassAnalysisAutomationRun.window_from.asc(), MassAnalysisAutomationRun.automation_run_id.asc())
    )
    runs_res = await db.execute(runs_stmt)
    raw_runs = runs_res.scalars().all()

    # Exclude any runs marked with invalid/missing token errors
    runs = []
    for r in raw_runs:
        err = (r.error_message or "").lower()
        if "missing" in err or "invalid" in err or "token" in err:
            continue
        runs.append(r)

    gaps = []
    total_gap_seconds = 0.0
    weekday_workhours_gaps = []
    weekday_offhours_gaps = []
    weekend_holiday_gaps = []

    prev_run = None
    for r in runs:
        w_from = r.window_from
        w_to = r.window_to
        if w_from.tzinfo is None:
            w_from = w_from.replace(tzinfo=timezone.utc)
        if w_to.tzinfo is None:
            w_to = w_to.replace(tzinfo=timezone.utc)

        if prev_run is not None:
            prev_to = prev_run.window_to
            if prev_to.tzinfo is None:
                prev_to = prev_to.replace(tzinfo=timezone.utc)

            gap_sec = (w_from - prev_to).total_seconds()
            if gap_sec >= min_gap_seconds:
                total_gap_seconds += gap_sec
                f_mad = prev_to.astimezone(MADRID_TZ)
                t_mad = w_from.astimezone(MADRID_TZ)
                work_day = is_working_day(f_mad)
                work_hrs = is_working_hours(f_mad) or is_working_hours(t_mad)

                # Priority categorization
                if not work_day:
                    category = "weekend_or_holiday"
                    priority = "IGNORE"
                elif work_hrs:
                    category = "weekday_working_hours"
                    priority = "HIGH" if gap_sec >= 300.0 else "MEDIUM"
                else:
                    category = "weekday_off_hours"
                    priority = "LOW"

                gap_item = {
                    "gap_from_utc": prev_to,
                    "gap_to_utc": w_from,
                    "gap_from_madrid": f_mad,
                    "gap_to_madrid": t_mad,
                    "gap_seconds": gap_sec,
                    "gap_minutes": round(gap_sec / 60.0, 2),
                    "is_working_day": work_day,
                    "is_working_hours": work_hrs,
                    "category": category,
                    "priority": priority,
                    "prev_run_id": prev_run.automation_run_id,
                    "next_run_id": r.automation_run_id,
                }
                gaps.append(gap_item)

                if not work_day:
                    weekend_holiday_gaps.append(gap_item)
                elif work_hrs:
                    weekday_workhours_gaps.append(gap_item)
                else:
                    weekday_offhours_gaps.append(gap_item)

        prev_run = r

    return {
        "automation_id": automation_id,
        "automation_name": aut.name,
        "days_back": days_back,
        "total_runs_analyzed": len(runs),
        "total_gaps_count": len(gaps),
        "total_gap_minutes": round(total_gap_seconds / 60.0, 2),
        "total_gap_hours": round(total_gap_seconds / 3600.0, 2),
        "weekday_workhours_gaps": weekday_workhours_gaps,
        "weekday_offhours_gaps": weekday_offhours_gaps,
        "weekend_holiday_gaps": weekend_holiday_gaps,
        "gaps": gaps,
    }


async def main():
    parser = argparse.ArgumentParser(description="Diagnose historical automation gaps.")
    parser.add_argument("--automation-id", type=int, default=8)
    parser.add_argument("--days-back", type=int, default=7)
    parser.add_argument("--min-gap-minutes", type=float, default=1.0)
    args = parser.parse_args()

    from app.config import get_settings
    settings = get_settings()
    db_url = os.environ.get("DIAG_DATABASE_URL") or settings.database_url
    if not db_url or "localhost" in db_url:
        db_url = "postgresql+asyncpg://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url, echo=False)
    async with AsyncSession(engine) as db:
        res = await scan_automation_gaps(
            db,
            automation_id=args.automation_id,
            min_gap_seconds=args.min_gap_minutes * 60.0,
            days_back=args.days_back
        )

        print("=" * 110)
        print(f"DIAGNÓSTICO DE GAPS HISTÓRICOS - AUTOMATION ID = {res.get('automation_id')} ({res.get('automation_name')})")
        print("=" * 110)
        print(f"Período analizado: últimos {res.get('days_back')} días ({res.get('total_runs_analyzed')} runs evaluados)")
        print(f"Total Gaps detectados >= {args.min_gap_minutes} min: {res.get('total_gaps_count')}")
        print(f"Tiempo total omitido acumulado: {res.get('total_gap_minutes')} min ({res.get('total_gap_hours')} horas)")
        print("-" * 110)

        ww_gaps = res.get("weekday_workhours_gaps", [])
        wo_gaps = res.get("weekday_offhours_gaps", [])
        wh_gaps = res.get("weekend_holiday_gaps", [])

        ww_min = sum(g["gap_seconds"] for g in ww_gaps) / 60.0
        wo_min = sum(g["gap_seconds"] for g in wo_gaps) / 60.0
        wh_min = sum(g["gap_seconds"] for g in wh_gaps) / 60.0

        print(f"1. DÍAS LABORABLES - Horario Operativo (08:00 - 20:00 Madrid):")
        print(f"   - Total gaps: {len(ww_gaps)} gaps")
        print(f"   - Tiempo omitido: {ww_min:.2f} min ({ww_min/60.0:.2f} horas)")
        print(f"   - Gaps >= 5 min (Prioridad ALTA): {len([g for g in ww_gaps if g['priority'] == 'HIGH'])}")
        print(f"   - Gaps < 5 min / 1m ticks (Prioridad MEDIA): {len([g for g in ww_gaps if g['priority'] == 'MEDIUM'])}")

        print(f"\n2. DÍAS LABORABLES - Horario Nocturno / Fuera de Servicio (20:00 - 08:00 Madrid):")
        print(f"   - Total gaps: {len(wo_gaps)} gaps")
        print(f"   - Tiempo omitido: {wo_min:.2f} min ({wo_min/60.0:.2f} horas) (Prioridad BAJA)")

        print(f"\n3. FINES DE SEMANA Y FESTIVOS (Sábados, Domingos y 15/08):")
        print(f"   - Total gaps: {len(wh_gaps)} gaps")
        print(f"   - Tiempo omitido: {wh_min:.2f} min ({wh_min/60.0:.2f} horas) (Prioridad IGNORAR - Sin llamadas)")
        print("-" * 110)

        # Check existing results in DB for weekday workhours gaps
        high_gaps = [g for g in ww_gaps if g["priority"] == "HIGH"]
        print(f"\nDETALLE DE GAPS DE ALTA PRIORIDAD (Días laborables, Horario operativo, >= 5 min):")
        print(f"{'#':<4} | {'Gap From (Madrid)':<20} | {'Gap To (Madrid)':<20} | {'Duración':<10} | {'Persistidas en DB'}")
        print("-" * 110)
        for idx, g in enumerate(high_gaps, 1):
            q_cnt = text("""
                SELECT count(*) FROM bm_mass_evaluation_results
                WHERE call_timestamp >= :w_from AND call_timestamp <= :w_to
            """)
            db_cnt_res = await db.execute(q_cnt, {"w_from": g["gap_from_utc"], "w_to": g["gap_to_utc"]})
            persisted_cnt = db_cnt_res.scalar() or 0
            f_mad = g["gap_from_madrid"].strftime("%Y-%m-%d %H:%M:%S")
            t_mad = g["gap_to_madrid"].strftime("%Y-%m-%d %H:%M:%S")
            print(f"{idx:<4} | {f_mad:<20} | {t_mad:<20} | {g['gap_minutes']:>6.2f} min | {persisted_cnt} llamadas")


if __name__ == "__main__":
    asyncio.run(main())
