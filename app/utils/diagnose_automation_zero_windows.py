"""
Diagnostic tool for zero-call automation windows (Zero Real vs Bug).
====================================================================
Audits automation_id=8 (job_id=48, Service Front).
Checks DB runs, timezone conversions, HubSpot searches, and whole-day call distribution.
"""
import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

MADRID_TZ = ZoneInfo("Europe/Madrid")


def format_dt(dt: datetime | None, target_tz: ZoneInfo | timezone) -> str:
    if dt is None:
        return "None"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(target_tz).strftime("%Y-%m-%d %H:%M:%S %Z")


async def main():
    from app.config import get_settings
    from app.models.mass_evaluations import (
        MassAnalysisAutomation,
        MassAnalysisAutomationRun,
        MassEvaluationJob,
        MassEvaluationRun,
    )
    from app.services.hubspot_service import HubSpotService
    import httpx

    settings = get_settings()
    db_url = os.environ.get("DIAG_DATABASE_URL") or settings.database_url
    if not db_url or "localhost" in db_url:
        db_url = "postgresql+asyncpg://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    print("=" * 80)
    print("DIAGNÓSTICO DE AUTOMATIZACIÓN (CERO REAL VS BUG) - AUTOMATION ID = 8")
    print("=" * 80)

    async with async_session() as db:
        # 1. Fetch automation & job metadata
        aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == 8)
        aut_res = await db.execute(aut_stmt)
        aut = aut_res.scalars().first()

        if not aut:
            print("[ERROR] Automation ID 8 not found in database!")
            return

        print(f"Automation: ID={aut.automation_id}, Name='{aut.name}', Active={aut.is_active}")
        print(f"  Interval: {aut.interval_minutes}m, Lookback: {aut.lookback_minutes}m, Delay: {aut.delay_minutes}m")
        print(f"  Job ID: {aut.job_id}, Service ID: {aut.service_id}, Prompt ID: {aut.prompt_id}")
        print(f"  Last run at: {format_dt(aut.last_run_at, MADRID_TZ)}")

        job_stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == aut.job_id)
        job_res = await db.execute(job_stmt)
        job = job_res.scalars().first()
        if job:
            print(f"Job: ID={job.job_id}, Name='{job.job_name}', Duration min: {job.duration_min_seconds}s, Direction: {job.direction}")
            print(f"  Owners: {job.agent_owner_ids}")
            print(f"  Time window: {job.time_window_start} - {job.time_window_end}, Timezone: {job.timezone}")

        # 2. Fetch last 30 automation runs
        runs_stmt = (
            select(MassAnalysisAutomationRun)
            .where(MassAnalysisAutomationRun.automation_id == 8)
            .order_by(desc(MassAnalysisAutomationRun.automation_run_id))
            .limit(30)
        )
        runs_res = await db.execute(runs_stmt)
        recent_runs = list(reversed(runs_res.scalars().all()))

        print("\n" + "-" * 80)
        print(f"ÚLTIMAS {len(recent_runs)} EJECUCIONES EN BASE DE DATOS:")
        print("-" * 80)
        print(f"{'Run ID':<8} | {'Window From (Madrid)':<20} | {'Window To (Madrid)':<20} | {'Found':<5} | {'Sel':<4} | {'Status':<10} | {'Gap with prev'}")
        print("-" * 80)

        prev_w_to = None
        for r in recent_runs:
            w_from_mad = format_dt(r.window_from, MADRID_TZ)
            w_to_mad = format_dt(r.window_to, MADRID_TZ)
            
            gap_str = "N/A"
            if prev_w_to is not None and r.window_from is not None:
                diff_sec = (r.window_from - prev_w_to).total_seconds()
                if abs(diff_sec) < 1.0:
                    gap_str = "0s (continuous)"
                else:
                    gap_str = f"{diff_sec:+.1f}s"
            
            print(f"{r.automation_run_id:<8} | {w_from_mad:<20} | {w_to_mad:<20} | {r.calls_found or 0:<5} | {r.calls_selected or 0:<4} | {r.status:<10} | {gap_str}")
            prev_w_to = r.window_to

        # 3. Test sample windows directly against HubSpot
        print("\n" + "=" * 80)
        print("COMPARACIÓN CONTRA HUBSPOT DE VENTANAS MUESTRA:")
        print("=" * 80)

        hs_service = HubSpotService()
        
        # Select 5 sample runs from today
        sample_runs = [r for r in recent_runs if r.window_from is not None][-5:]
        if not sample_runs:
            sample_runs = recent_runs[-5:]

        owner_ids = job.agent_owner_ids if job and job.agent_owner_ids else [1375831790, 1375831791, 33013276, 1539993532, 33013277]

        for r in sample_runs:
            w_from = r.window_from
            w_to = r.window_to
            if not w_from or not w_to:
                continue

            if w_from.tzinfo is None:
                w_from = w_from.replace(tzinfo=timezone.utc)
            if w_to.tzinfo is None:
                w_to = w_to.replace(tzinfo=timezone.utc)

            from_ms = int(w_from.timestamp() * 1000)
            to_ms = int(w_to.timestamp() * 1000)

            print(f"\nAuditando Run ID {r.automation_run_id}:")
            print(f"  Window UTC:    {format_dt(w_from, timezone.utc)} -> {format_dt(w_to, timezone.utc)} (ms: {from_ms} -> {to_ms})")
            print(f"  Window Madrid: {format_dt(w_from, MADRID_TZ)} -> {format_dt(w_to, MADRID_TZ)}")
            print(f"  DB Found: {r.calls_found}, DB Selected: {r.calls_selected}, Status: {r.status}")

            # Direct search with full filters
            search_filters = {
                "date_from": w_from,
                "date_to": w_to,
                "agent_owner_ids": owner_ids,
                "duration_min_seconds": 120,
                "direction": job.direction if job else "all",
                "only_with_recording": True,
                "time_window_start": job.time_window_start.strftime("%H:%M:%S") if job and job.time_window_start else None,
                "time_window_end": job.time_window_end.strftime("%H:%M:%S") if job and job.time_window_end else None,
                "timezone": "Europe/Madrid",
            }

            try:
                calls = await hs_service.search_calls_for_mass_evaluation(search_filters)
                hs_count = len(calls)
                diag_verdict = "zero_source (HubSpot genuinely 0)" if hs_count == 0 else f"selection_bug (HubSpot has {hs_count} calls!)"
                print(f"  => HubSpot search with full filters: {hs_count} llamadas. Veredicto: {diag_verdict}")

                # Step by step filter breakdown for this window
                # Step A: Window ONLY (no owner, no duration, no recording filter)
                raw_filters = {"date_from": w_from, "date_to": w_to, "only_with_recording": False}
                raw_calls = await hs_service.search_calls_for_mass_evaluation(raw_filters)
                print(f"     [Breakdown] Raw calls in window (all owners, all durations, no rec filter): {len(raw_calls)}")

                # Step B: Window + 5 Owners only
                owner_calls = await hs_service.search_calls_for_mass_evaluation({
                    "date_from": w_from, "date_to": w_to, "agent_owner_ids": owner_ids, "only_with_recording": False
                })
                print(f"     [Breakdown] Calls for the 5 Front owners (any duration): {len(owner_calls)}")

                # Step C: Window + 5 Owners + duration >= 120s
                dur_calls = await hs_service.search_calls_for_mass_evaluation({
                    "date_from": w_from, "date_to": w_to, "agent_owner_ids": owner_ids,
                    "duration_min_seconds": 120, "only_with_recording": False
                })
                print(f"     [Breakdown] Calls for 5 owners + duration >= 120s (no rec filter): {len(dur_calls)}")

                # Step D: Window + 5 Owners + duration >= 120s + recording
                rec_calls = await hs_service.search_calls_for_mass_evaluation({
                    "date_from": w_from, "date_to": w_to, "agent_owner_ids": owner_ids,
                    "duration_min_seconds": 120, "only_with_recording": True
                })
                print(f"     [Breakdown] Calls for 5 owners + duration >= 120s + has recording: {len(rec_calls)}")

            except Exception as e:
                print(f"  [ERROR querying HubSpot]: {e}")

        # 4. Whole-day distribution for 15/08/2026 in HubSpot
        print("\n" + "=" * 80)
        print("DISTRIBUCIÓN HUBSPOT DEL DÍA COMPLETO (15/08/2026):")
        print("=" * 80)

        day_start_madrid = datetime(2026, 8, 15, 0, 0, 0, tzinfo=MADRID_TZ)
        day_end_madrid = datetime(2026, 8, 15, 23, 59, 59, tzinfo=MADRID_TZ)
        day_start_utc = day_start_madrid.astimezone(timezone.utc)
        day_end_utc = day_end_madrid.astimezone(timezone.utc)

        print(f"Rango completo del día 15/08/2026:")
        print(f"  Madrid: {format_dt(day_start_madrid, MADRID_TZ)} a {format_dt(day_end_madrid, MADRID_TZ)}")
        print(f"  UTC:    {format_dt(day_start_utc, timezone.utc)} a {format_dt(day_end_utc, timezone.utc)}")

        # A) Total calls across entire portal on 15/08/2026
        all_portal_calls = await hs_service.search_calls_for_mass_evaluation({
            "date_from": day_start_utc, "date_to": day_end_utc, "only_with_recording": False, "max_calls": 5000
        })
        print(f"\nTotal llamadas en TODO el portal HubSpot (15/08/2026): {len(all_portal_calls)}")

        # B) Front owners on 15/08/2026
        front_day_calls = await hs_service.search_calls_for_mass_evaluation({
            "date_from": day_start_utc, "date_to": day_end_utc, "agent_owner_ids": owner_ids,
            "only_with_recording": False, "max_calls": 5000
        })
        print(f"Total llamadas de los 5 agentes Front (15/08/2026): {len(front_day_calls)}")

        dur_gt_30 = [c for c in front_day_calls if float(c.get("duration") or 0) >= 30]
        dur_gt_60 = [c for c in front_day_calls if float(c.get("duration") or 0) >= 60]
        dur_gt_120 = [c for c in front_day_calls if float(c.get("duration") or 0) >= 120]
        with_rec = [c for c in dur_gt_120 if c.get("recording_url")]

        print(f"  - Duración >= 30s:  {len(dur_gt_30)}")
        print(f"  - Duración >= 60s:  {len(dur_gt_60)}")
        print(f"  - Duración >= 120s: {len(dur_gt_120)}")
        print(f"  - Duración >= 120s con grabación: {len(with_rec)}")

        # Breakdown by owner
        print("\nDesglose por agente (15/08/2026):")
        from app.utils.hubspot_owners import OWNER_TO_NAME
        for oid in owner_ids:
            name = OWNER_TO_NAME.get(oid, str(oid))
            o_calls = [c for c in front_day_calls if str(c.get("owner_id")) == str(oid)]
            o_120 = [c for c in o_calls if float(c.get("duration") or 0) >= 120]
            print(f"  - Agente {oid} ({name}): {len(o_calls)} llamadas totales, {len(o_120)} con >=120s")

        # Breakdown by hour
        print("\nDesglose por hora (Europe/Madrid) para los 5 agentes Front:")
        hour_buckets: dict[int, list] = {h: [] for h in range(24)}
        for c in front_day_calls:
            ts_str = c.get("call_timestamp")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    dt_mad = dt.astimezone(MADRID_TZ)
                    hour_buckets[dt_mad.hour].append(c)
                except Exception:
                    pass

        for h in range(24):
            c_h = hour_buckets[h]
            c_h_120 = [c for c in c_h if float(c.get("duration") or 0) >= 120]
            if len(c_h) > 0:
                print(f"  {h:02d}:00 - {h:02d}:59 : {len(c_h)} llamadas ({len(c_h_120)} con >=120s)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
