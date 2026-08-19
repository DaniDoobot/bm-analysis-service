"""
Safe Automation Historical Gap Backfill Tool.
=============================================
Identifies all un-evaluated historical time gaps between consecutive automation runs
and executes safe, non-destructive backfill for specific gap windows.

By default, runs in DRY-RUN mode (safe, read-only, no mutations).
With --execute and --confirm-execute=CONFIRM_BACKFILL_EXECUTE, processes the selected gaps
with strict deduplication, audit traceability, and hard secret validation.

Safety rules:
  1. In --execute mode, strictly requires HUBSPOT_ACCESS_TOKEN, Twilio, and LLM secrets.
     If any secret is missing, aborts immediately BEFORE creating any DB runs.
  2. In --dry-run mode, works without secrets for DB gap auditing, with clear warnings.
  3. Runs marked failed/invalid with missing tokens NEVER close/mask historical gaps.

Usage:
  # Dry-run preview (SAFE, NO MUTATIONS):
  python app/utils/backfill_automation_gaps.py --automation-id 8 --dry-run

  # Dry-run for specific gaps #460 and #540:
  python app/utils/backfill_automation_gaps.py --automation-id 8 --gap-indexes 460,540 --dry-run

  # Execute pilot gaps (requires production secrets):
  python app/utils/backfill_automation_gaps.py --automation-id 8 --gap-indexes 460,540 --execute --confirm-execute CONFIRM_BACKFILL_EXECUTE
"""
import os
import sys
import argparse
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

MADRID_TZ = ZoneInfo("Europe/Madrid")
logger = logging.getLogger("automation_gap_backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def format_dt(dt: datetime | None, target_tz: ZoneInfo | timezone) -> str:
    if dt is None:
        return "None"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(target_tz).strftime("%Y-%m-%d %H:%M:%S")


def is_working_day(dt_madrid: datetime) -> bool:
    """Return True if Monday to Friday and not Aug 15 national holiday."""
    isoweekday = dt_madrid.isoweekday()
    if isoweekday in (6, 7):
        return False
    if dt_madrid.month == 8 and dt_madrid.day == 15:
        return False
    return True


def is_working_hours(dt_madrid: datetime) -> bool:
    """Return True if between 08:00 and 20:00 Madrid time."""
    return 8 <= dt_madrid.hour < 20


def validate_backfill_environment(is_execute: bool = False) -> tuple[bool, list[str]]:
    """
    Validates availability of production credentials.
    In execute mode, missing credentials will block execution.
    """
    from app.config import get_settings
    settings = get_settings()

    missing = []
    token = settings.hubspot_access_token or os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        missing.append("HUBSPOT_ACCESS_TOKEN")

    twilio_sid = settings.twilio_account_sid or os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_tok = settings.twilio_auth_token or os.environ.get("TWILIO_AUTH_TOKEN")
    if not twilio_sid:
        missing.append("TWILIO_ACCOUNT_SID")
    if not twilio_tok:
        missing.append("TWILIO_AUTH_TOKEN")

    ai_provider = (settings.ai_provider or "gemini").lower()
    if ai_provider == "gemini":
        gem_key = settings.gemini_api_key or os.environ.get("GEMINI_API_KEY")
        if not gem_key:
            missing.append("GEMINI_API_KEY")
    elif ai_provider == "azure":
        az_key = settings.azure_openai_audio_api_key or os.environ.get("AZURE_OPENAI_AUDIO_API_KEY")
        if not az_key:
            missing.append("AZURE_OPENAI_AUDIO_API_KEY")

    if missing and is_execute:
        return False, missing
    return True, missing


async def plan_gap_backfill(
    db: AsyncSession,
    automation_id: int = 8,
    days_back: int = 7,
    min_gap_minutes: float = 1.0,
    only_working_hours: bool = False,
    only_high_priority: bool = False,
    gap_indexes: list[int] | None = None,
    high_priority_indexes: list[int] | None = None,
    max_gaps_to_process: int = 100,
) -> dict[str, Any]:
    from app.models.mass_evaluations import (
        MassAnalysisAutomation,
        MassAnalysisAutomationRun,
        MassEvaluationJob,
    )

    aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == automation_id)
    aut_res = await db.execute(aut_stmt)
    aut = aut_res.scalars().first()
    if not aut:
        return {"error": f"Automation {automation_id} not found"}

    job_stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == aut.job_id)
    job_res = await db.execute(job_stmt)
    job = job_res.scalars().first()

    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=days_back)

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

    min_gap_seconds = min_gap_minutes * 60.0
    detected_gaps = []
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
                    "gap_index": len(detected_gaps) + 1,
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
                    "prev_automation_run_id": prev_run.automation_run_id,
                    "next_automation_run_id": r.automation_run_id,
                }
                detected_gaps.append(gap_item)

                if not work_day:
                    weekend_holiday_gaps.append(gap_item)
                elif work_hrs:
                    weekday_workhours_gaps.append(gap_item)
                else:
                    weekday_offhours_gaps.append(gap_item)

        prev_run = r

    # Assign high priority indexes (1 to N)
    high_priority_gaps = [g for g in weekday_workhours_gaps if g["priority"] == "HIGH"]
    for idx, hg in enumerate(high_priority_gaps, 1):
        hg["high_priority_index"] = idx

    # Filter candidates
    if high_priority_indexes:
        candidates = [g for g in high_priority_gaps if g.get("high_priority_index") in high_priority_indexes]
    elif only_high_priority:
        candidates = high_priority_gaps
    elif gap_indexes:
        candidates = [g for g in detected_gaps if g["gap_index"] in gap_indexes or g.get("high_priority_index") in gap_indexes]
    elif only_working_hours:
        candidates = weekday_workhours_gaps
    else:
        candidates = detected_gaps

    planned_batches = candidates[:max_gaps_to_process]

    return {
        "automation_id": automation_id,
        "automation_name": aut.name,
        "job_id": aut.job_id,
        "job_name": job.job_name if job else None,
        "days_back": days_back,
        "total_runs_analyzed": len(runs),
        "total_gaps_found": len(detected_gaps),
        "total_gap_minutes": round(total_gap_seconds / 60.0, 2),
        "total_gap_hours": round(total_gap_seconds / 3600.0, 2),
        "weekday_workhours_count": len(weekday_workhours_gaps),
        "weekday_workhours_minutes": round(sum(g["gap_seconds"] for g in weekday_workhours_gaps) / 60.0, 2),
        "weekday_offhours_count": len(weekday_offhours_gaps),
        "weekday_offhours_minutes": round(sum(g["gap_seconds"] for g in weekday_offhours_gaps) / 60.0, 2),
        "weekend_holiday_count": len(weekend_holiday_gaps),
        "weekend_holiday_minutes": round(sum(g["gap_seconds"] for g in weekend_holiday_gaps) / 60.0, 2),
        "planned_batches_count": len(planned_batches),
        "planned_batches": planned_batches,
    }


async def execute_gap_backfill(
    db: AsyncSession,
    automation_id: int,
    planned_gaps: list[dict[str, Any]],
    max_calls_per_gap: int = 100,
) -> list[dict[str, Any]]:
    """
    Executes real backfill for the specified planned gap intervals.
    Enforces deduplication, creates audited MassEvaluationRun & MassAnalysisAutomationRun,
    and analyzes calls via HubSpot & AI model.
    """
    # Hard safety check for execution mode: validate required secrets before any DB actions
    ok, missing = validate_backfill_environment(is_execute=True)
    if not ok:
        err_msg = f"Cannot execute backfill: Required production secret(s) missing in execution environment: {', '.join(missing)}"
        logger.error("[automation_gap_backfill] %s", err_msg)
        raise RuntimeError(err_msg)

    from app.models.mass_evaluations import (
        MassAnalysisAutomation,
        MassAnalysisAutomationRun,
        MassEvaluationJob,
        MassEvaluationRun,
        MassEvaluationResult,
    )
    from app.models.prompts import PromptVersion, Prompt
    from app.models.services import Service
    from app.services.hubspot_service import HubSpotService
    from app.services.mass_evaluation_service import MassEvaluationService

    aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == automation_id)
    aut_res = await db.execute(aut_stmt)
    aut = aut_res.scalars().first()
    if not aut:
        raise ValueError(f"Automation {automation_id} not found")

    job_stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == aut.job_id)
    job_res = await db.execute(job_stmt)
    job = job_res.scalars().first()
    if not job:
        raise ValueError(f"Linked Job ID {aut.job_id} not found")

    # Snapshot all attributes before any commit/await to prevent MissingGreenlet
    aut_automation_id = aut.automation_id
    job_id = job.job_id
    job_company_id = job.company_id
    job_service_id = job.service_id

    # Resolve Prompt Version snapshot
    if job.prompt_version_id:
        v_stmt = select(PromptVersion).where(PromptVersion.id == job.prompt_version_id)
    else:
        v_stmt = (
            select(PromptVersion)
            .where(PromptVersion.prompt_id == job.prompt_id)
            .order_by(PromptVersion.is_current.desc(), PromptVersion.id.desc())
        )
    v_res = await db.execute(v_stmt)
    v_obj = v_res.scalars().first()
    if not v_obj:
        raise ValueError(f"Could not resolve prompt version for Prompt ID {job.prompt_id}")

    prompt_id = job.prompt_id
    prompt_snapshot = v_obj.prompt
    prompt_version_id = v_obj.id
    prompt_name = job.prompt_name or v_obj.version_name or f"Prompt {job.prompt_id}"
    prompt_version_name = job.prompt_version_name or v_obj.version_name or f"v{v_obj.id}"
    prompt_version_label = job.prompt_version_label or v_obj.version_label or f"v{v_obj.id}"

    # Default Front owner IDs
    from app.utils.hubspot_owners import OWNER_TO_NAME
    owner_ids = job.agent_owner_ids if job.agent_owner_ids else list(OWNER_TO_NAME.keys())

    hs_service = HubSpotService()
    execution_results = []

    for gap in planned_gaps:
        gap_idx = gap["gap_index"]
        w_from = gap["gap_from_utc"]
        w_to = gap["gap_to_utc"]
        dur_min = gap["gap_minutes"]

        logger.info(
            "[automation_gap_backfill] Starting execution for Gap #%d: %s -> %s (%s min)",
            gap_idx,
            format_dt(w_from, MADRID_TZ),
            format_dt(w_to, MADRID_TZ),
            dur_min,
        )

        effective_filters = {
            "job_mode": "standard",
            "date_from": w_from.isoformat(),
            "date_to": w_to.isoformat(),
            "agent_owner_ids": owner_ids,
            "duration_min_seconds": 120,
            "duration_max_seconds": None,
            "direction": "all",
            "only_with_recording": True,
            "max_calls": max_calls_per_gap,
            "timezone": "Europe/Madrid",
            "execution_source": "backfill",
        }

        # 1. Create MassEvaluationRun
        mass_run = MassEvaluationRun(
            job_id=job_id,
            company_id=job_company_id,
            service_id=job_service_id,
            trigger_type="backfill",
            status="running",
            started_at=datetime.now(timezone.utc),
            effective_filters=effective_filters,
            execution_source="backfill",
        )
        db.add(mass_run)
        await db.commit()
        await db.refresh(mass_run)
        cur_mass_run_id = mass_run.run_id

        # 2. Create MassAnalysisAutomationRun
        auto_run = MassAnalysisAutomationRun(
            automation_id=aut_automation_id,
            job_id=job_id,
            run_id=cur_mass_run_id,
            status="running",
            window_from=w_from,
            window_to=w_to,
            started_at=datetime.now(timezone.utc),
        )
        db.add(auto_run)
        await db.commit()
        await db.refresh(auto_run)
        cur_auto_run_id = auto_run.automation_run_id

        # 3. Search HubSpot calls
        try:
            calls = await hs_service.search_calls_for_mass_evaluation(effective_filters)
            calls_found_cnt = len(calls)
            await db.execute(
                update(MassEvaluationRun).where(MassEvaluationRun.run_id == cur_mass_run_id).values(calls_found=calls_found_cnt)
            )
            await db.execute(
                update(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == cur_auto_run_id).values(calls_found=calls_found_cnt)
            )
            await db.commit()

            # Deduplicate against existing completed results in DB
            unique_calls = []
            seen_ids = set()
            for c in calls:
                cid = c.get("call_id")
                if not cid or cid in seen_ids:
                    continue
                seen_ids.add(cid)

                # Check if already completed in DB
                q_dup = select(MassEvaluationResult.mass_analysis_id).where(
                    MassEvaluationResult.call_id == str(cid),
                    MassEvaluationResult.status == "completed"
                )
                dup_res = await db.execute(q_dup)
                if dup_res.scalar() is not None:
                    logger.info("[automation_gap_backfill] Call %s already completed in DB. Skipping duplicate.", cid)
                    continue

                unique_calls.append(c)

            selected_calls = unique_calls[:max_calls_per_gap]
            calls_sel_cnt = len(selected_calls)
            await db.execute(
                update(MassEvaluationRun).where(MassEvaluationRun.run_id == cur_mass_run_id).values(calls_selected=calls_sel_cnt)
            )
            await db.execute(
                update(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == cur_auto_run_id).values(calls_selected=calls_sel_cnt)
            )
            await db.commit()

            calls_analyzed = 0
            calls_failed = 0
            calls_skipped = 0
            now_finished = datetime.now(timezone.utc)

            if len(selected_calls) == 0:
                logger.info("[automation_gap_backfill] 0 un-evaluated calls found for Gap #%d.", gap_idx)
                final_status = "completed"
                await db.execute(
                    update(MassEvaluationRun).where(MassEvaluationRun.run_id == cur_mass_run_id).values(
                        status=final_status, finished_at=now_finished
                    )
                )
                await db.execute(
                    update(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == cur_auto_run_id).values(
                        status=final_status, finished_at=now_finished
                    )
                )
                await db.commit()
            else:
                logger.info("[automation_gap_backfill] Processing %d calls for Gap #%d...", len(selected_calls), gap_idx)
                # Delegate to MassEvaluationService background runner
                await MassEvaluationService._execute_background_run(job_id, cur_mass_run_id, effective_filters)

                # Fetch updated metrics from DB
                m_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == cur_mass_run_id).execution_options(populate_existing=True)
                m_res = await db.execute(m_stmt)
                m_fresh = m_res.scalar()

                final_status = m_fresh.status if m_fresh else "completed"
                calls_analyzed = m_fresh.calls_analyzed if m_fresh else len(selected_calls)
                calls_failed = m_fresh.calls_failed if m_fresh else 0
                calls_skipped = m_fresh.calls_skipped if m_fresh else 0

                await db.execute(
                    update(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == cur_auto_run_id).values(
                        status=final_status,
                        calls_skipped=calls_skipped,
                        finished_at=now_finished,
                    )
                )
                await db.commit()

            execution_results.append({
                "gap_index": gap_idx,
                "mass_run_id": cur_mass_run_id,
                "automation_run_id": cur_auto_run_id,
                "gap_from_madrid": format_dt(w_from, MADRID_TZ),
                "gap_to_madrid": format_dt(w_to, MADRID_TZ),
                "duration_minutes": dur_min,
                "status": final_status,
                "calls_found": calls_found_cnt,
                "calls_selected": calls_sel_cnt,
                "calls_analyzed": calls_analyzed,
                "calls_failed": calls_failed,
                "calls_skipped": calls_skipped,
                "error_message": None,
            })

        except Exception as e_gap:
            logger.error("[automation_gap_backfill] Error executing Gap #%d: %s", gap_idx, e_gap, exc_info=True)
            now_err = datetime.now(timezone.utc)
            err_msg = str(e_gap)
            recovered_status = "failed"
            try:
                # Check if MassEvaluationRun actually completed before marking as failed
                m_chk_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == cur_mass_run_id).execution_options(populate_existing=True)
                m_chk_res = await db.execute(m_chk_stmt)
                m_chk = m_chk_res.scalar()

                if m_chk and m_chk.status in ["completed", "completed_with_errors"]:
                    logger.warning("[automation_gap_backfill] MassEvaluationRun %d was completed, preserving status despite bookkeeping error.", cur_mass_run_id)
                    recovered_status = m_chk.status
                    if cur_auto_run_id:
                        await db.execute(
                            update(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == cur_auto_run_id).values(
                                status=m_chk.status, finished_at=m_chk.finished_at or now_err, error_message=None
                            )
                        )
                else:
                    if cur_mass_run_id:
                        await db.execute(
                            update(MassEvaluationRun).where(MassEvaluationRun.run_id == cur_mass_run_id).values(
                                status="failed", error_message=err_msg, finished_at=now_err
                            )
                        )
                    if cur_auto_run_id:
                        await db.execute(
                            update(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == cur_auto_run_id).values(
                                status="failed", error_message=err_msg, finished_at=now_err
                            )
                        )
                await db.commit()
            except Exception as e_commit:
                logger.error("[automation_gap_backfill] Error marking runs failed/recovered: %s", e_commit)

            execution_results.append({
                "gap_index": gap_idx,
                "mass_run_id": cur_mass_run_id,
                "automation_run_id": cur_auto_run_id,
                "gap_from_madrid": format_dt(w_from, MADRID_TZ),
                "gap_to_madrid": format_dt(w_to, MADRID_TZ),
                "duration_minutes": dur_min,
                "status": recovered_status,
                "calls_found": calls_found_cnt if "calls_found_cnt" in locals() else 0,
                "calls_selected": calls_sel_cnt if "calls_sel_cnt" in locals() else 0,
                "calls_analyzed": calls_analyzed if "calls_analyzed" in locals() else 0,
                "calls_failed": calls_failed if "calls_failed" in locals() else 0,
                "calls_skipped": calls_skipped if "calls_skipped" in locals() else 0,
                "error_message": None if recovered_status in ["completed", "completed_with_errors"] else err_msg,
            })

    return execution_results


async def main():
    parser = argparse.ArgumentParser(description="Audit and backfill historical automation gaps.")
    parser.add_argument("--automation-id", type=int, default=8, help="Automation ID to backfill (default: 8)")
    parser.add_argument("--days-back", type=int, default=7, help="Days to look back for gaps (default: 7)")
    parser.add_argument("--min-gap-minutes", type=float, default=1.0, help="Minimum gap in minutes to detect (default: 1.0)")
    parser.add_argument("--gap-indexes", type=str, default="", help="Comma-separated global gap indexes to process (e.g. '148,150')")
    parser.add_argument("--high-priority-indexes", type=str, default="", help="Comma-separated high-priority gap indexes (e.g. '9,10')")
    parser.add_argument("--only-high-priority", action="store_true", default=False, help="Filter only HIGH priority gaps (weekday working hours >= 5 min)")
    parser.add_argument("--only-working-hours", action="store_true", default=False, help="Filter only weekday working hours (08:00 - 20:00)")
    parser.add_argument("--max-gaps", type=int, default=100, help="Max gap windows to include in plan (default: 100)")
    parser.add_argument("--max-calls-per-gap", type=int, default=100, help="Max calls per gap window (default: 100)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Perform dry-run only without making changes (default: True)")
    parser.add_argument("--execute", action="store_true", default=False, help="Explicit flag to execute backfill")
    parser.add_argument("--confirm-execute", type=str, default="", help="Confirmation string required for execution ('CONFIRM_BACKFILL_EXECUTE')")

    args = parser.parse_args()

    parsed_gap_indexes = []
    if args.gap_indexes:
        try:
            parsed_gap_indexes = [int(x.strip()) for x in args.gap_indexes.split(",") if x.strip()]
        except ValueError:
            print(f"[ERROR] Invalid --gap-indexes format: {args.gap_indexes}. Must be comma-separated integers.")
            sys.exit(1)

    parsed_high_indexes = []
    if args.high_priority_indexes:
        try:
            parsed_high_indexes = [int(x.strip()) for x in args.high_priority_indexes.split(",") if x.strip()]
        except ValueError:
            print(f"[ERROR] Invalid --high-priority-indexes format: {args.high_priority_indexes}. Must be comma-separated integers.")
            sys.exit(1)

    from app.config import get_settings
    settings = get_settings()
    db_url = os.environ.get("DIAG_DATABASE_URL") or settings.database_url
    if not db_url or "localhost" in db_url:
        db_url = "postgresql+asyncpg://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url, echo=False)
    async with AsyncSession(engine) as db:
        plan = await plan_gap_backfill(
            db,
            automation_id=args.automation_id,
            days_back=args.days_back,
            min_gap_minutes=args.min_gap_minutes,
            only_working_hours=args.only_working_hours,
            only_high_priority=args.only_high_priority,
            gap_indexes=parsed_gap_indexes if parsed_gap_indexes else None,
            high_priority_indexes=parsed_high_indexes if parsed_high_indexes else None,
            max_gaps_to_process=args.max_gaps,
        )

        print("\n" + "=" * 125)
        print(f"[automation_gap_backfill] PLAN DE BACKFILL HISTÓRICO - AUTOMATION ID {plan.get('automation_id')}")
        print("=" * 125)
        print(f"Automatización: '{plan.get('automation_name')}' (Job ID: {plan.get('job_id')}, '{plan.get('job_name')}')")
        print(f"Período analizado: últimos {plan.get('days_back')} días ({plan.get('total_runs_analyzed')} runs evaluados)")
        print(f"Total Gaps detectados >= {args.min_gap_minutes} min: {plan.get('total_gaps_found')} ({plan.get('total_gap_minutes')} min / {plan.get('total_gap_hours')} horas)")
        print(f"  - Días laborables (horario operativo 08-20h): {plan.get('weekday_workhours_count')} gaps ({plan.get('weekday_workhours_minutes')} min)")
        print(f"  - Días laborables (horario nocturno 20-08h):  {plan.get('weekday_offhours_count')} gaps ({plan.get('weekday_offhours_minutes')} min)")
        print(f"  - Fines de semana / Festivos:                {plan.get('weekend_holiday_count')} gaps ({plan.get('weekend_holiday_minutes')} min)")
        print(f"Ventanas seleccionadas para procesar: {plan.get('planned_batches_count')} ventanas")
        print(f"Modo: {'DRY-RUN (SOLO LECTURA - SIN CAMBIOS)' if (not args.execute or args.confirm_execute != 'CONFIRM_BACKFILL_EXECUTE') else 'EXECUTION (CAMBIOS REALES)'}")
        print("-" * 125)

        batches = plan.get("planned_batches", [])
        if batches:
            print(f"{'High#':<6} | {'Global#':<8} | {'Gap From (Madrid)':<20} | {'Gap To (Madrid)':<20} | {'Duración':<10} | {'Categoría':<25} | {'Prioridad':<10} | {'Persistidas'}")
            print("-" * 135)
            for b in batches:
                q_cnt = text("""
                    SELECT count(*) FROM bm_mass_evaluation_results
                    WHERE call_timestamp >= :w_from AND call_timestamp <= :w_to
                """)
                db_cnt_res = await db.execute(q_cnt, {"w_from": b["gap_from_utc"], "w_to": b["gap_to_utc"]})
                persisted_cnt = db_cnt_res.scalar() or 0

                h_idx_str = str(b.get("high_priority_index") or "-")
                g_idx_str = str(b["gap_index"])
                f_mad = b["gap_from_madrid"].strftime("%Y-%m-%d %H:%M:%S")
                t_mad = b["gap_to_madrid"].strftime("%Y-%m-%d %H:%M:%S")
                print(f"{h_idx_str:<6} | {g_idx_str:<8} | {f_mad:<20} | {t_mad:<20} | {b['gap_minutes']:>6.2f} min | {b['category']:<25} | {b['priority']:<10} | {persisted_cnt} calls")

        print("\n" + "=" * 125)

        # Execution condition
        if args.execute:
            if args.confirm_execute != "CONFIRM_BACKFILL_EXECUTE":
                print("[SAFETY ABORT] --execute requires --confirm-execute=CONFIRM_BACKFILL_EXECUTE. Execution blocked.")
                sys.exit(1)

            # Validate production credentials before anything
            ok_env, missing_secrets = validate_backfill_environment(is_execute=True)
            if not ok_env:
                print(f"[SECURITY ABORT] Cannot execute backfill: Required production secret(s) missing in execution environment: {', '.join(missing_secrets)}.")
                print("Execution blocked BEFORE creating any DB runs or calling LLMs.")
                sys.exit(1)

            if not batches:
                print("[INFO] No gap windows selected to execute.")
                return

            print(f"[EXECUTION] Starting backfill execution for {len(batches)} gap window(s)...")
            exec_results = await execute_gap_backfill(
                db,
                automation_id=args.automation_id,
                planned_gaps=batches,
                max_calls_per_gap=args.max_calls_per_gap,
            )

            print("\n" + "=" * 125)
            print("RESULTADOS DE LA EJECUCIÓN DE BACKFILL:")
            print("=" * 125)
            print(f"{'Gap#':<5} | {'MassRun':<8} | {'AutoRun':<8} | {'Ventana Madrid':<42} | {'Status':<10} | {'Found':<5} | {'Sel':<4} | {'Analyzed':<8}")
            print("-" * 125)
            for er in exec_results:
                win_str = f"{er['gap_from_madrid']} -> {er['gap_to_madrid']}"
                print(f"{er['gap_index']:<5} | {er['mass_run_id']:<8} | {er['automation_run_id']:<8} | {win_str:<42} | {er['status']:<10} | {er['calls_found']:<5} | {er['calls_selected']:<4} | {er['calls_analyzed']:<8}")

            print("=" * 125)
        else:
            ok_env, missing_secrets = validate_backfill_environment(is_execute=False)
            if "HUBSPOT_ACCESS_TOKEN" in missing_secrets:
                print("[WARNING] Dry-run running without HUBSPOT_ACCESS_TOKEN: cannot query live HubSpot API to estimate actual source calls count.")
            print("[automation_gap_backfill] Dry-run finalizado con éxito. NO se realizaron modificaciones ni llamadas masivas.")
            print("=" * 125)


if __name__ == "__main__":
    asyncio.run(main())
