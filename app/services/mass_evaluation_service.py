"""Mass Evaluation Service for managing jobs, runs, and background analyses."""
import asyncio
import logging
import sys
import zoneinfo
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select, update, delete, desc, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.models.prompts import Prompt, PromptVersion
from app.schemas.mass_evaluations import MassEvaluationJobCreate, MassEvaluationJobUpdate
from app.services.hubspot_service import HubSpotService
from app.services.twilio_service import TwilioService
from app.services.openai_service import analyze_audio_bytes
from app.utils.dates import safe_parse_datetime
from app.utils.json_utils import safe_parse_json
from app.services.analysis_results_mapper import map_criterion_value
from app.services.criteria_service import get_active_criteria
from app.utils.hubspot_owners import resolve_agent_display

logger = logging.getLogger(__name__)

MAX_AUDIO_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB


def calculate_next_run(
    schedule_type: str | None,
    schedule_time: time | None,
    schedule_day_of_week: int | None,
    schedule_day_of_month: int | None,
    schedule_cron: str | None,
    timezone_name: str = "Europe/Madrid"
) -> datetime | None:
    if not schedule_type or schedule_type == "manual":
        return None

    try:
        tz = zoneinfo.ZoneInfo(timezone_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("Europe/Madrid")
        
    now = datetime.now(tz)
    t = schedule_time or time(0, 0)

    if schedule_type == "daily":
        dt = datetime.combine(now.date(), t).replace(tzinfo=tz)
        if dt <= now:
            dt += timedelta(days=1)
        return dt

    elif schedule_type == "weekly":
        target_wd = schedule_day_of_week if schedule_day_of_week is not None else 0
        days_ahead = target_wd - now.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        dt = datetime.combine(now.date() + timedelta(days=days_ahead), t).replace(tzinfo=tz)
        if dt <= now:
            dt += timedelta(days=7)
        return dt

    elif schedule_type == "monthly":
        target_dom = schedule_day_of_month if schedule_day_of_month is not None else 1
        try:
            dt = datetime(now.year, now.month, target_dom, t.hour, t.minute, t.second, tzinfo=tz)
        except ValueError:
            # Day out of range for current month, go to next month
            if now.month == 12:
                dt = datetime(now.year + 1, 1, 1, t.hour, t.minute, t.second, tzinfo=tz)
            else:
                dt = datetime(now.year, now.month + 1, 1, t.hour, t.minute, t.second, tzinfo=tz)

        if dt <= now:
            # Advance to next month
            if now.month == 12:
                dt = datetime(now.year + 1, 1, target_dom, t.hour, t.minute, t.second, tzinfo=tz)
            else:
                try:
                    dt = datetime(now.year, now.month + 1, target_dom, t.hour, t.minute, t.second, tzinfo=tz)
                except ValueError:
                    # If next month has fewer days than target_dom, roll to 1st of next-next month
                    if now.month + 1 == 12:
                        dt = datetime(now.year + 1, 1, 1, t.hour, t.minute, t.second, tzinfo=tz)
                    else:
                        dt = datetime(now.year, now.month + 2, 1, t.hour, t.minute, t.second, tzinfo=tz)
        return dt

    elif schedule_type == "cron":
        # Simple fallback: next hour
        dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return dt

    return None


def resolve_date_filters(job: MassEvaluationJob, timezone_name: str = "Europe/Madrid") -> tuple[datetime | None, datetime | None]:
    try:
        tz = zoneinfo.ZoneInfo(timezone_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("Europe/Madrid")
        
    now = datetime.now(tz)

    if job.date_mode == "relative":
        days = job.relative_days or 1
        date_from = now - timedelta(days=days)
        return date_from, now

    elif job.date_mode == "previous_day":
        yesterday = now - timedelta(days=1)
        date_from = datetime.combine(yesterday.date(), time.min).replace(tzinfo=tz)
        date_to = datetime.combine(yesterday.date(), time.max).replace(tzinfo=tz)
        return date_from, date_to

    elif job.date_mode == "previous_week":
        start_of_this_week = now - timedelta(days=now.weekday())
        start_of_prev_week = start_of_this_week - timedelta(days=7)
        date_from = datetime.combine(start_of_prev_week.date(), time.min).replace(tzinfo=tz)
        date_to = datetime.combine((start_of_this_week - timedelta(days=1)).date(), time.max).replace(tzinfo=tz)
        return date_from, date_to

    elif job.date_mode in ["fixed_range", "custom"]:
        return job.date_from, job.date_to

    return None, None


async def enrich_job_prompt_info(db: AsyncSession, job: MassEvaluationJob) -> None:
    """Enrich the job with prompt/version name details based on prompt_id."""
    stmt = select(Prompt).where(Prompt.prompt_id == job.prompt_id)
    res = await db.execute(stmt)
    prompt = res.scalars().first()
    if prompt:
        job.prompt_name = prompt.prompt_name
        # Find specified version or active one
        if job.prompt_version_id:
            stmt_v = select(PromptVersion).where(PromptVersion.id == job.prompt_version_id)
        else:
            stmt_v = select(PromptVersion).where(PromptVersion.prompt_id == job.prompt_id, PromptVersion.is_current == True)
            
        res_v = await db.execute(stmt_v)
        v = res_v.scalars().first()
        if v:
            job.prompt_version_id = v.id
            job.prompt_version_name = v.version_name
            job.prompt_version_label = v.version_label


class MassEvaluationService:
    @staticmethod
    async def create_job(db: AsyncSession, payload: MassEvaluationJobCreate) -> MassEvaluationJob:
        job = MassEvaluationJob(**payload.model_dump())
        await enrich_job_prompt_info(db, job)
        
        # Calculate schedule
        if job.schedule_enabled:
            job.next_run_at = calculate_next_run(
                job.schedule_type,
                job.schedule_time,
                job.schedule_day_of_week,
                job.schedule_day_of_month,
                job.schedule_cron,
                job.timezone
            )
            
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job

    @staticmethod
    async def update_job(db: AsyncSession, job_id: int, payload: MassEvaluationJobUpdate) -> MassEvaluationJob | None:
        stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
        res = await db.execute(stmt)
        job = res.scalars().first()
        if not job:
            return None
            
        update_data = payload.model_dump(exclude_unset=True)
        for k, v in update_data.items():
            setattr(job, k, v)
            
        if "prompt_id" in update_data or "prompt_version_id" in update_data:
            await enrich_job_prompt_info(db, job)
            
        if job.schedule_enabled:
            job.next_run_at = calculate_next_run(
                job.schedule_type,
                job.schedule_time,
                job.schedule_day_of_week,
                job.schedule_day_of_month,
                job.schedule_cron,
                job.timezone
            )
        else:
            job.next_run_at = None
            
        await db.commit()
        await db.refresh(job)
        return job

    @staticmethod
    async def delete_job(db: AsyncSession, job_id: int, soft_delete: bool = True) -> bool:
        stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
        res = await db.execute(stmt)
        job = res.scalars().first()
        if not job:
            return False
            
        if soft_delete:
            job.is_active = False
            await db.commit()
        else:
            await db.delete(job)
            await db.commit()
        return True

    @staticmethod
    async def list_jobs(db: AsyncSession, limit: int = 100) -> list[MassEvaluationJob]:
        stmt = select(MassEvaluationJob).where(MassEvaluationJob.is_active == True).order_by(desc(MassEvaluationJob.job_id)).limit(limit)
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_job(db: AsyncSession, job_id: int) -> MassEvaluationJob | None:
        stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
        res = await db.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def dry_run_job(db: AsyncSession, job_id: int, override_date_from: datetime | None = None, override_date_to: datetime | None = None) -> dict[str, Any]:
        stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
        res = await db.execute(stmt)
        job = res.scalars().first()
        if not job:
            raise ValueError(f"Job ID {job_id} not found")
            
        date_from, date_to = resolve_date_filters(job, job.timezone)
        if override_date_from:
            date_from = override_date_from
        if override_date_to:
            date_to = override_date_to
            
        filters = {
            "date_from": date_from,
            "date_to": date_to,
            "agent_owner_ids": job.agent_owner_ids,
            "duration_min_seconds": job.duration_min_seconds,
            "duration_max_seconds": job.duration_max_seconds,
            "direction": job.direction,
            "only_with_recording": job.only_with_recording,
            "max_calls": job.max_calls
        }
        
        hs_service = HubSpotService()
        calls = await hs_service.search_calls_for_mass_evaluation(filters)
        
        return {
            "job_id": job_id,
            "calls_found": len(calls),
            "effective_filters": {
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "agent_owner_ids": job.agent_owner_ids,
                "direction": job.direction,
                "only_with_recording": job.only_with_recording,
                "max_calls": job.max_calls
            },
            "calls": [{"call_id": c["call_id"], "recording_url": c["recording_url"], "hubspot_owner_id": c["hubspot_owner_id"]} for c in calls]
        }

    @staticmethod
    async def run_job(db: AsyncSession, job_id: int, trigger_type: str = "manual", override_date_from: datetime | None = None, override_date_to: datetime | None = None) -> MassEvaluationRun:
        # Check active execution lock
        stmt_lock = select(MassEvaluationRun).where(MassEvaluationRun.job_id == job_id, MassEvaluationRun.status == "running")
        res_lock = await db.execute(stmt_lock)
        active_run = res_lock.scalars().first()
        if active_run:
            raise ValueError(f"Job {job_id} is already running with run_id {active_run.run_id}")
            
        stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
        res = await db.execute(stmt)
        job = res.scalars().first()
        if not job:
            raise ValueError(f"Job ID {job_id} not found")
            
        date_from, date_to = resolve_date_filters(job, job.timezone)
        if override_date_from:
            date_from = override_date_from
        if override_date_to:
            date_to = override_date_to
            
        effective_filters = {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "agent_owner_ids": job.agent_owner_ids,
            "duration_min_seconds": job.duration_min_seconds,
            "duration_max_seconds": job.duration_max_seconds,
            "direction": job.direction,
            "only_with_recording": job.only_with_recording,
            "max_calls": job.max_calls
        }
        
        # Create Run record
        run = MassEvaluationRun(
            job_id=job_id,
            trigger_type=trigger_type,
            status="running",
            started_at=datetime.now(timezone.utc),
            effective_filters=effective_filters
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        
        # Launch background task
        asyncio.create_task(MassEvaluationService._execute_background_run(job_id, run.run_id, effective_filters))
        
        return run

    @staticmethod
    async def _execute_background_run(job_id: int, run_id: int, filters_payload: dict[str, Any]) -> None:
        """Background executor for mass analyses."""
        from app.db import get_engine
        engine = get_engine()
        
        # We need a new session in background
        async with AsyncSession(engine) as db:
            run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id)
            run_res = await db.execute(run_stmt)
            run = run_res.scalars().first()
            if not run:
                logger.error("Run ID %d not found in background task", run_id)
                return
                
            job_stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
            job_res = await db.execute(job_stmt)
            job = job_res.scalars().first()
            if not job:
                logger.error("Job ID %d not found in background task", job_id)
                run.status = "failed"
                run.error_message = f"Job ID {job_id} not found."
                run.finished_at = datetime.now(timezone.utc)
                await db.commit()
                return

            try:
                # 1. Resolve prompt snapshot
                if job.prompt_version_id:
                    v_stmt = select(PromptVersion).where(PromptVersion.id == job.prompt_version_id)
                else:
                    v_stmt = select(PromptVersion).where(PromptVersion.prompt_id == job.prompt_id, PromptVersion.is_current == True)
                    
                v_res = await db.execute(v_stmt)
                v = v_res.scalars().first()
                if not v or not v.prompt:
                    raise ValueError(f"Could not resolve prompt text for Prompt ID {job.prompt_id}")
                    
                prompt_snapshot = v.prompt
                prompt_version_id = v.id
                
                # 2. Query HubSpot
                hs_service = HubSpotService()
                
                # Parse filter dates back to datetime
                date_from_str = filters_payload.get("date_from")
                date_to_str = filters_payload.get("date_to")
                
                date_from = safe_parse_datetime(date_from_str) if date_from_str else None
                date_to = safe_parse_datetime(date_to_str) if date_to_str else None
                
                search_filters = {
                    "date_from": date_from,
                    "date_to": date_to,
                    "agent_owner_ids": filters_payload.get("agent_owner_ids"),
                    "duration_min_seconds": job.duration_min_seconds,
                    "duration_max_seconds": job.duration_max_seconds,
                    "direction": filters_payload.get("direction"),
                    "only_with_recording": filters_payload.get("only_with_recording"),
                    "max_calls": filters_payload.get("max_calls")
                }
                
                calls = await hs_service.search_calls_for_mass_evaluation(search_filters)
                run.calls_found = len(calls)
                
                # 3. Filter duplicates within the same execution
                seen_call_ids = set()
                selected_calls = []
                for c in calls:
                    c_id = c["call_id"]
                    if c_id not in seen_call_ids:
                        seen_call_ids.add(c_id)
                        selected_calls.append(c)
                        
                run.calls_selected = len(selected_calls)
                await db.commit()
                
                calls_analyzed = 0
                calls_skipped = 0
                calls_failed = 0
                
                # Process sequentially to avoid heavy concurrency issues
                for call in selected_calls:
                    call_id = call["call_id"]
                    recording_url = call["recording_url"]
                    
                    if not recording_url:
                        # Skip
                        res_row = MassEvaluationResult(
                            run_id=run_id,
                            job_id=job_id,
                            call_id=call_id,
                            hs_object_id=call["hs_object_id"],
                            hubspot_owner_id=call["hubspot_owner_id"],
                            call_timestamp=safe_parse_datetime(call["call_timestamp"]),
                            call_duration_seconds=call["call_duration_seconds"],
                            direction=call["direction"],
                            prompt_id=job.prompt_id,
                            prompt_version_id=prompt_version_id,
                            prompt_name=job.prompt_name,
                            prompt_version_name=job.prompt_version_name,
                            prompt_version_label=job.prompt_version_label,
                            prompt_snapshot=prompt_snapshot,
                            status="skipped",
                            error_message="No recording URL present."
                        )
                        db.add(res_row)
                        calls_skipped += 1
                        continue
                        
                    # Process call analysis
                    try:
                        twilio_service = TwilioService()
                        audio_bytes = await twilio_service.download_audio(recording_url)
                        
                        audio_size = sys.getsizeof(audio_bytes)
                        if audio_size > MAX_AUDIO_SIZE_BYTES:
                            raise ValueError("El audio supera el tamaño máximo permitido por Azure OpenAI (20 MB)")
                            
                        audio_format = "mp3"
                        if recording_url.endswith(".wav") or recording_url.endswith(".WAV"):
                            audio_format = "wav"
                            
                        # Call Azure / OpenAI
                        raw_response = await analyze_audio_bytes(
                            audio_bytes=audio_bytes,
                            prompt_text=prompt_snapshot,
                            audio_format=audio_format
                        )
                        
                        parsed = safe_parse_json(raw_response)
                        if not parsed:
                            raise ValueError("El modelo no devolvió un JSON válido.")
                            
                        # Strip legacy keys from result
                        from app.services.analysis_persistence import _strip_legacy_keys
                        clean_result = _strip_legacy_keys(parsed)
                        
                        # Resolve active criteria items
                        items = []
                        criteria = await get_active_criteria(db, job.prompt_id)
                        for criterion in criteria:
                            output_key = criterion.output_key
                            feed_key = criterion.feed_key

                            raw_value = clean_result.get(output_key) if output_key else None
                            feed_value = clean_result.get(feed_key) if feed_key else None

                            # Get clean/typed value
                            typed = map_criterion_value(raw_value, criterion.criterion_type or "text")
                            
                            # Resolve actual value
                            resolved_val = None
                            if criterion.criterion_type == "number":
                                resolved_val = float(typed["value_number"]) if typed["value_number"] is not None else None
                            elif criterion.criterion_type == "boolean":
                                resolved_val = typed["value_boolean"]
                            else:
                                resolved_val = typed["value_text"] or typed["value_category"] or typed["raw_value"]

                            items.append({
                                "criterion_key": criterion.criterion_key,
                                "name": criterion.criterion_name,
                                "type": criterion.criterion_type,
                                "output_key": output_key,
                                "value": resolved_val,
                                "feed": str(feed_value) if feed_value is not None else None
                            })
                            
                        # Resolve agent name display
                        owner_id = call["hubspot_owner_id"]
                        resolved_agent = OWNER_TO_NAME.get(owner_id) or owner_id
                        
                        # Persist Result
                        res_row = MassEvaluationResult(
                            run_id=run_id,
                            job_id=job_id,
                            call_id=call_id,
                            hs_object_id=call["hs_object_id"],
                            recording_url=recording_url,
                            hubspot_owner_id=owner_id,
                            agent_name=resolved_agent,
                            call_timestamp=safe_parse_datetime(call["call_timestamp"]),
                            call_duration_seconds=call["call_duration_seconds"],
                            direction=call["direction"],
                            prompt_id=job.prompt_id,
                            prompt_version_id=prompt_version_id,
                            prompt_name=job.prompt_name,
                            prompt_version_name=job.prompt_version_name,
                            prompt_version_label=job.prompt_version_label,
                            prompt_snapshot=prompt_snapshot,
                            status="completed",
                            result_json=clean_result,
                            items_json=items,
                            hubspot_metadata=call
                        )
                        db.add(res_row)
                        calls_analyzed += 1
                        
                    except Exception as e_call:
                        logger.warning("Call %s failed in mass evaluation job %d: %s", call_id, job_id, e_call)
                        res_row = MassEvaluationResult(
                            run_id=run_id,
                            job_id=job_id,
                            call_id=call_id,
                            hs_object_id=call["hs_object_id"],
                            recording_url=recording_url,
                            hubspot_owner_id=call["hubspot_owner_id"],
                            call_timestamp=safe_parse_datetime(call["call_timestamp"]),
                            call_duration_seconds=call["call_duration_seconds"],
                            direction=call["direction"],
                            prompt_id=job.prompt_id,
                            prompt_version_id=prompt_version_id,
                            prompt_name=job.prompt_name,
                            prompt_version_name=job.prompt_version_name,
                            prompt_version_label=job.prompt_version_label,
                            prompt_snapshot=prompt_snapshot,
                            status="failed",
                            error_message=str(e_call)
                        )
                        db.add(res_row)
                        calls_failed += 1
                        
                    # Commit per-call results to prevent loss of progress
                    await db.commit()
                    
                # Update final run summary counters
                run.calls_analyzed = calls_analyzed
                run.calls_skipped = calls_skipped
                run.calls_failed = calls_failed
                
                if calls_failed > 0:
                    run.status = "completed_with_errors"
                else:
                    run.status = "completed"
                    
                run.finished_at = datetime.now(timezone.utc)
                run.run_summary = {
                    "analyzed": calls_analyzed,
                    "skipped": calls_skipped,
                    "failed": calls_failed,
                    "total": len(selected_calls)
                }
                
                # Update last run timestamp in Job
                job.last_run_at = datetime.now(timezone.utc)
                if job.schedule_enabled:
                    job.next_run_at = calculate_next_run(
                        job.schedule_type,
                        job.schedule_time,
                        job.schedule_day_of_week,
                        job.schedule_day_of_month,
                        job.schedule_cron,
                        job.timezone
                    )
                await db.commit()
                logger.info("Mass evaluation job %d, run %d finished with status: %s", job_id, run_id, run.status)
                
            except Exception as e_run:
                logger.error("Mass evaluation job %d run %d failed in background: %s", job_id, run_id, e_run, exc_info=True)
                run.status = "failed"
                run.error_message = str(e_run)
                run.finished_at = datetime.now(timezone.utc)
                await db.commit()

    @staticmethod
    async def list_runs(db: AsyncSession, job_id: int | None = None, status: str | None = None, limit: int = 100) -> list[MassEvaluationRun]:
        stmt = select(MassEvaluationRun)
        filters = []
        if job_id is not None:
            filters.append(MassEvaluationRun.job_id == job_id)
        if status is not None:
            filters.append(MassEvaluationRun.status == status)
            
        if filters:
            stmt = stmt.where(and_(*filters))
            
        stmt = stmt.order_by(desc(MassEvaluationRun.run_id)).limit(limit)
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_run(db: AsyncSession, run_id: int) -> MassEvaluationRun | None:
        stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id)
        res = await db.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def list_results(
        db: AsyncSession,
        run_id: int | None = None,
        job_id: int | None = None,
        agent_owner_id: str | None = None,
        call_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 100
    ) -> list[MassEvaluationResult]:
        stmt = select(MassEvaluationResult)
        filters = []
        if run_id is not None:
            filters.append(MassEvaluationResult.run_id == run_id)
        if job_id is not None:
            filters.append(MassEvaluationResult.job_id == job_id)
        if agent_owner_id is not None:
            filters.append(MassEvaluationResult.hubspot_owner_id == agent_owner_id)
        if call_id is not None:
            filters.append(MassEvaluationResult.call_id == call_id)
        if date_from is not None:
            filters.append(MassEvaluationResult.call_timestamp >= date_from)
        if date_to is not None:
            filters.append(MassEvaluationResult.call_timestamp <= date_to)
            
        if filters:
            stmt = stmt.where(and_(*filters))
            
        stmt = stmt.order_by(desc(MassEvaluationResult.mass_analysis_id)).limit(limit)
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_result(db: AsyncSession, mass_analysis_id: int) -> MassEvaluationResult | None:
        stmt = select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == mass_analysis_id)
        res = await db.execute(stmt)
        return res.scalars().first()
