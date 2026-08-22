"""Mass Evaluation Service for managing jobs, runs, and background analyses."""
import asyncio
import logging
import sys
import zoneinfo
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update, delete, desc, asc, func, and_, or_, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, defer

from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
)
from app.models.prompts import Prompt, PromptVersion
from app.schemas.mass_evaluations import (
    MassEvaluationJobCreate,
    MassEvaluationJobUpdate,
    MassAnalysisAutomationCreate,
    MassAnalysisAutomationUpdate,
)
from app.services.hubspot_service import HubSpotService
from app.services.twilio_service import TwilioService
from app.services.openai_service import analyze_audio_bytes
from app.utils.dates import safe_parse_datetime
from app.utils.json_utils import safe_parse_json
from app.services.analysis_results_mapper import map_criterion_value
from app.services.criteria_service import get_active_criteria
from app.utils.hubspot_owners import resolve_agent_display
from app.utils.normalizers import normalize_direction
from app.utils.memory_utils import log_process_memory, track_memory_async, get_process_rss_mb, check_and_log_memory_thresholds
import gc

logger = logging.getLogger(__name__)

MAX_AUDIO_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB


def normalize_tipo_llamada(val: Any) -> str | None:
    """
    Robustly normalize a raw typology key from the LLM or criteria outputs.
    Supports capitalization, accents, whitespace, and common synonyms.
    Returns the normalized key mapping or the cleaned key if no mapping is found.
    """
    if val is None:
        return None
    val_str = str(val).strip().lower()
    if not val_str:
        return None
    
    # Remove accents / diacritics
    import unicodedata
    val_str = "".join(
        c for c in unicodedata.normalize("NFD", val_str)
        if unicodedata.category(c) != "Mn"
    )
    
    # Check for specific variations and synonyms
    if any(x in val_str for x in ["confirm", "confir"]):
        return "confirmacion"
    if any(x in val_str for x in ["reprogram", "reagend", "cambi"]):
        return "reagendo"
    if any(x in val_str for x in ["cancel", "anul"]):
        return "cancelacion"
    if any(x in val_str for x in ["falta", "no show", "noshow", "ausencia", "no asist"]):
        return "falta"
    if any(x in val_str for x in ["transfer", "traspas", "deriv"]):
        return "transferencia"
    if any(x in val_str for x in ["intento", "contacto", "no disponible"]):
        return "intento_contacto"
    if "cita" in val_str:
        return "cita"
    if any(x in val_str for x in ["otro", "general", "preci"]):
        return "otros"
        
    return val_str



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


def normalize_resolved_dates(date_from: datetime | None, date_to: datetime | None, timezone_name: str = "Europe/Madrid") -> tuple[datetime | None, datetime | None]:
    try:
        tz = zoneinfo.ZoneInfo(timezone_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("Europe/Madrid")

    def is_date_only_val(dt: datetime, target_tz) -> bool:
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
            return True
        dt_utc = dt.astimezone(timezone.utc)
        if dt_utc.hour == 0 and dt_utc.minute == 0 and dt_utc.second == 0 and dt_utc.microsecond == 0:
            return True
        dt_tz = dt.astimezone(target_tz)
        if dt_tz.hour == 0 and dt_tz.minute == 0 and dt_tz.second == 0 and dt_tz.microsecond == 0:
            return True
        return False

    if date_from is not None:
        dt_from_tz = date_from.astimezone(tz) if date_from.tzinfo else date_from.replace(tzinfo=tz)
        if is_date_only_val(date_from, tz):
            date_from = datetime.combine(dt_from_tz.date(), time.min).replace(tzinfo=tz)
        else:
            date_from = dt_from_tz

    if date_to is not None:
        dt_to_tz = date_to.astimezone(tz) if date_to.tzinfo else date_to.replace(tzinfo=tz)
        if is_date_only_val(date_to, tz):
            date_to = datetime.combine(dt_to_tz.date(), time(23, 59, 59, 999000)).replace(tzinfo=tz)
        else:
            date_to = dt_to_tz

    return date_from, date_to


def resolve_date_filters(job: MassEvaluationJob, timezone_name: str = "Europe/Madrid") -> tuple[datetime | None, datetime | None]:
    try:
        tz = zoneinfo.ZoneInfo(timezone_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("Europe/Madrid")
        
    now = datetime.now(tz)

    # 1. Si date_from o date_to vienen informados explícitamente, tienen prioridad sobre last_n_days.
    # 2. Solo calcular date_from/date_to usando last_n_days cuando no exista ningún rango manual.
    if job.date_from is not None or job.date_to is not None:
        return normalize_resolved_dates(job.date_from, job.date_to, timezone_name)

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

    # Fallback default: past 24 hours (1 relative day)
    date_from = now - timedelta(days=1)
    return date_from, now


async def enrich_job_prompt_info(db: AsyncSession, job: MassEvaluationJob) -> None:
    """Enrich the job with prompt/version name details based on prompt_id."""
    stmt = select(Prompt).where(Prompt.prompt_id == job.prompt_id)
    res = await db.execute(stmt)
    prompt = res.scalars().first()
    if prompt:
        job.prompt_name = prompt.prompt_name
        if job.prompt_version_id:
            stmt_v = select(PromptVersion).where(PromptVersion.id == job.prompt_version_id)
        else:
            stmt_v = (
                select(PromptVersion)
                .where(PromptVersion.prompt_id == job.prompt_id)
                .order_by(PromptVersion.is_current.desc(), PromptVersion.id.desc())
            )
            
        res_v = await db.execute(stmt_v)
        v = res_v.scalars().first()
        if v:
            job.prompt_version_id = v.id
            job.prompt_version_name = v.version_name
            job.prompt_version_label = v.version_label


class MassEvaluationService:
    _running_tasks: set[asyncio.Task] = set()

    # Advisory lock key for single-worker enforcement of the automation scheduler.
    # hash('speech_bm_automation_scheduler') mod 2^31 — any stable int is fine.
    _SCHEDULER_LOCK_KEY: int = 1234567890
    # threading.Lock used as fallback in SQLite test environments (no pg_advisory_lock).
    # threading.Lock.acquire(blocking=False) is the non-blocking try-acquire method.
    import threading as _threading
    _threading_scheduler_lock: "threading.Lock" = _threading.Lock()
    del _threading  # clean up class namespace after use


    @staticmethod
    async def _try_acquire_scheduler_lock(db: AsyncSession) -> bool:
        """Legacy helper: non-blocking check. Kept for backwards-compatibility."""
        lock = MassEvaluationService._threading_scheduler_lock
        if lock is None:
            return True
        return lock.acquire(blocking=False)

    @staticmethod
    async def _release_scheduler_lock(db: AsyncSession) -> None:
        """Legacy helper: release threading lock. Kept for backwards-compatibility."""
        lock = MassEvaluationService._threading_scheduler_lock
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


    @staticmethod
    async def create_job(
        db: AsyncSession,
        payload: MassEvaluationJobCreate,
        company_id: int | None = None,
        service_id: int | None = None,
    ) -> MassEvaluationJob:
        target_company_id: int | None = None
        target_service_id: int | None = None

        if payload.prompt_id:
            stmt = select(Prompt).where(Prompt.prompt_id == payload.prompt_id)
            res = await db.execute(stmt)
            prompt = res.scalars().first()
            if not prompt or prompt.is_archived or prompt.deleted_at is not None:
                raise ValueError("La estructura seleccionada no existe o está archivada.")

            # Check for active draft warning
            from app.models.drafts import PromptDraft
            draft_stmt = select(PromptDraft).where(
                PromptDraft.prompt_id == prompt.prompt_id,
                PromptDraft.status.in_(["draft", "pending", "active"])
            ).limit(1)
            draft_res = await db.execute(draft_stmt)
            active_draft = draft_res.scalars().first()
            if active_draft:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(
                    f"¡ADVERTENCIA! El prompt ID {prompt.prompt_id} tiene un borrador activo (Draft ID {active_draft.draft_id}). "
                    f"El job de análisis masivo usará la versión publicada en producción (current_version_id), NO el borrador activo de trabajo."
                )

            prompt_service_id = prompt.service_id or service_id or payload.service_id
            prompt_company_id = prompt.company_id or company_id or payload.company_id

            if not prompt_company_id and prompt_service_id:
                from app.models.services import Service
                s_res = await db.execute(select(Service.company_id).where(Service.service_id == prompt_service_id))
                company_from_service = s_res.scalar()
                if company_from_service:
                    prompt_company_id = company_from_service
                    prompt.company_id = prompt_company_id
                    db.add(prompt)
                    await db.flush()

            if not prompt_company_id:
                raise ValueError("La estructura seleccionada no tiene empresa asociada.")

            if not prompt_service_id:
                raise ValueError("No se pudo determinar el servicio para el job.")

            target_company_id = prompt_company_id
            target_service_id = prompt_service_id
        else:
            target_company_id = company_id or payload.company_id
            target_service_id = service_id or payload.service_id

        if not target_company_id or not target_service_id:
            raise ValueError("Debe seleccionar una estructura específica o especificar empresa y servicio.")

        # Validate selection mode and call_ids
        if payload.selection_mode == "manual_call_ids":
            if not payload.call_ids:
                raise ValueError("Debe proporcionar al menos un ID de llamada para la selección manual.")
            
            seen = set()
            cleaned_ids = []
            for cid in payload.call_ids:
                trimmed = str(cid).strip()
                if not trimmed:
                    continue
                if trimmed not in seen:
                    seen.add(trimmed)
                    cleaned_ids.append(trimmed)
            
            if len(cleaned_ids) > 200:
                raise ValueError("El máximo permitido es de 200 IDs de llamada por job.")
            
            payload.call_ids = cleaned_ids

        # Remove override flags and transient fields from payload before db insert
        job_data = payload.model_dump()
        job_data.pop("allow_inactive_prompt", None)
        job_data.pop("test_mode", None)
        job_data.pop("min_duration_minutes", None)
        job_data.pop("max_duration_minutes", None)

        if job_data.get("job_mode") == "random_quality_monitoring":
            c_per_day = job_data.get("calls_per_day")
            if not c_per_day or c_per_day <= 0:
                raise ValueError("Se requiere 'calls_per_day' mayor que 0 para la monitorización aleatoria de calidad.")
            d_from = job_data.get("date_from")
            d_to = job_data.get("date_to")
            rel_days = job_data.get("relative_days")
            d_mode = job_data.get("date_mode")
            if not d_from and not d_to and not rel_days and d_mode not in ("relative", "previous_day", "previous_week"):
                raise ValueError("Se requiere definir rango de fechas (date_from/date_to) o un período relativo para la monitorización aleatoria de calidad.")
            if d_from and d_to and d_to < d_from:
                raise ValueError("date_to no puede ser anterior a date_from.")

        job = MassEvaluationJob(**job_data)
        job.company_id = target_company_id
        job.service_id = target_service_id
        
        # Defensive safety cap on max_calls
        if job.max_calls is None or job.max_calls <= 0:
            job.max_calls = 10
        elif job.max_calls > 500:
            job.max_calls = 500

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
        allow_inactive = update_data.pop("allow_inactive_prompt", False) or False
        test_mode = update_data.pop("test_mode", False) or False
        update_data.pop("min_duration_minutes", None)
        update_data.pop("max_duration_minutes", None)

        effective_job_mode = update_data.get("job_mode", job.job_mode)
        if effective_job_mode == "random_quality_monitoring":
            eff_calls_per_day = update_data.get("calls_per_day", job.calls_per_day)
            eff_date_from = update_data.get("date_from", job.date_from)
            eff_date_to = update_data.get("date_to", job.date_to)
            eff_relative_days = update_data.get("relative_days", job.relative_days)
            eff_date_mode = update_data.get("date_mode", job.date_mode)

            if not eff_calls_per_day or eff_calls_per_day <= 0:
                raise ValueError("Se requiere 'calls_per_day' mayor que 0 para la monitorización aleatoria de calidad.")
            if not eff_date_from and not eff_date_to and not eff_relative_days and eff_date_mode not in ("relative", "previous_day", "previous_week"):
                raise ValueError("Se requiere definir rango de fechas (date_from/date_to) o un período relativo para la monitorización aleatoria de calidad.")
            if eff_date_from and eff_date_to and eff_date_to < eff_date_from:
                raise ValueError("date_to no puede ser anterior a date_from.")

        # If prompt_id is being updated, perform same checks
        prompt_id_to_check = update_data.get("prompt_id")
        if prompt_id_to_check is not None:
            prompt_stmt = select(Prompt).where(Prompt.prompt_id == prompt_id_to_check)
            prompt_res = await db.execute(prompt_stmt)
            prompt = prompt_res.scalars().first()
            if not prompt:
                raise ValueError(f"La estructura con ID {prompt_id_to_check} no existe.")

            if prompt.is_archived or prompt.deleted_at is not None:
                raise ValueError("La estructura seleccionada no existe o está archivada.")

            # Check for active draft warning
            from app.models.drafts import PromptDraft
            draft_stmt = select(PromptDraft).where(
                PromptDraft.prompt_id == prompt.prompt_id,
                PromptDraft.status.in_(["draft", "pending", "active"])
            ).limit(1)
            draft_res = await db.execute(draft_stmt)
            active_draft = draft_res.scalars().first()
            target_service_id = prompt.service_id or update_data.get("service_id")
            target_company_id = prompt.company_id or update_data.get("company_id")

            if not target_company_id and target_service_id:
                from app.models.services import Service
                s_res = await db.execute(select(Service.company_id).where(Service.service_id == target_service_id))
                company_from_service = s_res.scalar()
                if company_from_service:
                    target_company_id = company_from_service
                    prompt.company_id = target_company_id
                    db.add(prompt)
                    await db.flush()

            if target_company_id:
                update_data["company_id"] = target_company_id
            if target_service_id:
                update_data["service_id"] = target_service_id

        for k, v in update_data.items():
            setattr(job, k, v)
            
        # Defensive safety cap on max_calls
        if job.max_calls is None or job.max_calls <= 0:
            job.max_calls = 10
        elif job.max_calls > 500:
            job.max_calls = 500

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
    async def list_jobs(
        db: AsyncSession,
        limit: int = 100,
        execution_source: str = "on_demand",
        company_ids: list[int] | None = None,
        service_ids: list[int] | None = None,
    ) -> list[MassEvaluationJob]:
        stmt = (
            select(MassEvaluationJob)
            .where(
                MassEvaluationJob.is_active == True,
                MassEvaluationJob.execution_source == execution_source
            )
        )
        if service_ids is not None:
            stmt = stmt.where(MassEvaluationJob.service_id.in_(service_ids))
        elif company_ids:
            stmt = stmt.where(MassEvaluationJob.company_id.in_(company_ids))

        stmt = stmt.order_by(desc(MassEvaluationJob.job_id)).limit(limit)
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_job(db: AsyncSession, job_id: int) -> MassEvaluationJob | None:
        stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
        res = await db.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def select_random_calls_for_quality_monitoring(
        hs_service: HubSpotService,
        filters: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Select random calls per day for quality monitoring auditing.
        Iterates day-by-day between date_from and date_to.
        For each day:
          - Searches all candidate calls matching filters.
          - Samples min(calls_per_day, len(candidates)) randomly using random.sample.
          - Tracks candidate counts and selected call IDs per day.
        Returns (selected_calls_list, trace_metadata_dict).
        """
        import random
        import zoneinfo

        calls_per_day = filters.get("calls_per_day") or 20
        if isinstance(calls_per_day, str):
            try:
                calls_per_day = int(calls_per_day)
            except ValueError:
                calls_per_day = 20

        if calls_per_day <= 0:
            raise ValueError("Se requiere 'calls_per_day' mayor que 0 para la monitorización aleatoria de calidad.")

        timezone_name = filters.get("timezone") or "Europe/Madrid"
        try:
            tz = zoneinfo.ZoneInfo(timezone_name)
        except Exception:
            tz = zoneinfo.ZoneInfo("Europe/Madrid")

        date_from = filters.get("date_from")
        date_to = filters.get("date_to")

        if isinstance(date_from, str):
            date_from = safe_parse_datetime(date_from)
        if isinstance(date_to, str):
            date_to = safe_parse_datetime(date_to)

        if not date_from or not date_to:
            raise ValueError("date_from y date_to son obligatorios para la monitorización aleatoria de calidad.")

        date_from_local = date_from.astimezone(tz) if date_from.tzinfo else date_from.replace(tzinfo=tz)
        date_to_local = date_to.astimezone(tz) if date_to.tzinfo else date_to.replace(tzinfo=tz)

        start_date = date_from_local.date()
        end_date = date_to_local.date()

        if end_date < start_date:
            raise ValueError("date_to no puede ser anterior a date_from.")

        all_selected_calls = []
        candidates_count_by_day = {}
        selected_count_by_day = {}
        selected_call_ids_by_day = {}

        current_date = start_date
        while current_date <= end_date:
            day_str = current_date.strftime("%Y-%m-%d")

            day_start = datetime.combine(current_date, time.min).replace(tzinfo=tz)
            day_end = datetime.combine(current_date, time(23, 59, 59, 999000)).replace(tzinfo=tz)

            day_filters = dict(filters)
            day_filters["date_from"] = day_start
            day_filters["date_to"] = day_end
            day_filters["max_calls"] = 10000

            try:
                candidates_day = await hs_service.search_calls_for_mass_evaluation(day_filters)
            except Exception as e_hs:
                logger.warning("HubSpot search failed for date %s: %s", day_str, e_hs)
                candidates_day = []
            candidates_count = len(candidates_day)
            candidates_count_by_day[day_str] = candidates_count

            if candidates_count <= calls_per_day:
                selected_day = list(candidates_day)
            else:
                selected_day = random.sample(candidates_day, calls_per_day)

            selected_count_by_day[day_str] = len(selected_day)
            selected_call_ids_by_day[day_str] = [c["call_id"] for c in selected_day]

            all_selected_calls.extend(selected_day)
            current_date += timedelta(days=1)

        trace_metadata = {
            "job_mode": "random_quality_monitoring",
            "calls_per_day": calls_per_day,
            "date_from": date_from_local.strftime("%Y-%m-%d"),
            "date_to": date_to_local.strftime("%Y-%m-%d"),
            "total_candidates": sum(candidates_count_by_day.values()),
            "total_selected": len(all_selected_calls),
            "candidates_count_by_day": candidates_count_by_day,
            "selected_count_by_day": selected_count_by_day,
            "selected_call_ids_by_day": selected_call_ids_by_day,
        }

        return all_selected_calls, trace_metadata

    @staticmethod
    async def search_calls_for_job_preview(
        db: AsyncSession,
        job_id: int,
        override_date_from: datetime | None = None,
        override_date_to: datetime | None = None
    ) -> dict[str, Any]:
        stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
        res = await db.execute(stmt)
        job = res.scalars().first()
        if not job:
            raise ValueError(f"Job ID {job_id} not found")
            
        hs_service = HubSpotService()
        
        if job.selection_mode == "manual_call_ids":
            calls = []
            not_found_call_ids = []
            call_ids_list = job.call_ids or []
            for cid in call_ids_list:
                try:
                    call_meta = await hs_service.get_call(cid)
                    calls.append({
                        "call_id": cid,
                        "recording_url": call_meta.get("recording_url"),
                        "hubspot_owner_id": call_meta.get("hubspot_owner_id")
                    })
                except Exception:
                    not_found_call_ids.append(cid)
                    
            found_ids = [c["call_id"] for c in calls]
            return {
                "job_id": job_id,
                "calls_found": len(calls),
                "effective_filters": {
                    "selection_mode": "manual_call_ids",
                    "call_ids": call_ids_list
                },
                "calls": calls,
                "found_call_ids": found_ids,
                "not_found_call_ids": not_found_call_ids,
                "duplicate_input_call_ids": [],
                "normalized_call_ids": call_ids_list
            }
            
        # Normal filter selection mode
        date_from, date_to = resolve_date_filters(job, job.timezone)
        if override_date_from:
            date_from = override_date_from
        if override_date_to:
            date_to = override_date_to
            
        date_from, date_to = normalize_resolved_dates(date_from, date_to, job.timezone)

        filters = {
            "date_from": date_from,
            "date_to": date_to,
            "agent_owner_ids": job.agent_owner_ids,
            "duration_min_seconds": job.duration_min_seconds,
            "duration_max_seconds": job.duration_max_seconds,
            "direction": job.direction,
            "only_with_recording": job.only_with_recording,
            "max_calls": job.max_calls,
            "calls_per_day": job.calls_per_day,
            "time_window_start": job.time_window_start,
            "time_window_end": job.time_window_end,
            "timezone": job.timezone,
        }
        
        if job.job_mode == "random_quality_monitoring":
            calls, trace_meta = await MassEvaluationService.select_random_calls_for_quality_monitoring(hs_service, filters)
            effective_filters = dict(filters)
            effective_filters["date_from"] = date_from.isoformat() if date_from else None
            effective_filters["date_to"] = date_to.isoformat() if date_to else None
            effective_filters["time_window_start"] = job.time_window_start.strftime("%H:%M:%S") if job.time_window_start else None
            effective_filters["time_window_end"] = job.time_window_end.strftime("%H:%M:%S") if job.time_window_end else None
            effective_filters.update(trace_meta)

            return {
                "job_id": job_id,
                "calls_found": trace_meta.get("total_candidates", len(calls)),
                "effective_filters": effective_filters,
                "calls": [{"call_id": c["call_id"], "recording_url": c["recording_url"], "hubspot_owner_id": c["hubspot_owner_id"]} for c in calls],
                "found_call_ids": [c["call_id"] for c in calls],
                "not_found_call_ids": [],
                "duplicate_input_call_ids": [],
                "normalized_call_ids": [c["call_id"] for c in calls]
            }

        try:
            calls = await hs_service.search_calls_for_mass_evaluation(filters)
        except Exception as e_hs:
            logger.warning("HubSpot search failed during preview for job %s: %s", job_id, e_hs)
            calls = []
        
        return {
            "job_id": job_id,
            "calls_found": len(calls),
            "effective_filters": {
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "agent_owner_ids": job.agent_owner_ids,
                "direction": job.direction,
                "only_with_recording": job.only_with_recording,
                "max_calls": job.max_calls,
                "time_window_start": job.time_window_start.strftime("%H:%M:%S") if job.time_window_start else None,
                "time_window_end": job.time_window_end.strftime("%H:%M:%S") if job.time_window_end else None,
                "timezone": job.timezone,
            },
            "calls": [{"call_id": c["call_id"], "recording_url": c["recording_url"], "hubspot_owner_id": c["hubspot_owner_id"]} for c in calls],
            "found_call_ids": [c["call_id"] for c in calls],
            "not_found_call_ids": [],
            "duplicate_input_call_ids": [],
            "normalized_call_ids": [c["call_id"] for c in calls]
        }

    @staticmethod
    async def dry_run_job(
        db: AsyncSession,
        job_id: int,
        override_date_from: datetime | None = None,
        override_date_to: datetime | None = None
    ) -> dict[str, Any]:
        """
        Simulate/dry-run a mass evaluation job without launching actual analysis.
        Returns candidate counts, selected calls, and effective filters.
        """
        stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
        res = await db.execute(stmt)
        job = res.scalars().first()
        if not job:
            raise ValueError(f"Job ID {job_id} not found")

        preview = await MassEvaluationService.search_calls_for_job_preview(
            db=db,
            job_id=job_id,
            override_date_from=override_date_from,
            override_date_to=override_date_to
        )

        eff_filters = preview.get("effective_filters") or {}
        candidates_count = preview.get("calls_found", 0)
        calls_list = preview.get("calls") or []
        selected_count = len(calls_list)
        selected_count_by_day = eff_filters.get("selected_count_by_day")

        return {
            "ok": True,
            "job_id": job_id,
            "mode": job.job_mode or "standard",
            "estimated_calls": candidates_count,
            "candidates_count": candidates_count,
            "selected_count": selected_count,
            "selected_count_by_day": selected_count_by_day,
            "filters": eff_filters,
            "found_call_ids": preview.get("found_call_ids", []),
            "not_found_call_ids": preview.get("not_found_call_ids", []),
            "normalized_call_ids": preview.get("normalized_call_ids", []),
            "calls": calls_list
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
            
        date_from, date_to = normalize_resolved_dates(date_from, date_to, job.timezone)

        if job.selection_mode == "manual_call_ids":
            effective_filters = {
                "selection_mode": "manual_call_ids",
                "call_ids": job.call_ids,
                "max_calls": job.max_calls
            }
        else:
            effective_filters = {
                "job_mode": job.job_mode or "standard",
                "calls_per_day": job.calls_per_day,
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "agent_owner_ids": job.agent_owner_ids,
                "duration_min_seconds": job.duration_min_seconds,
                "duration_max_seconds": job.duration_max_seconds,
                "direction": job.direction,
                "only_with_recording": job.only_with_recording,
                "max_calls": job.max_calls,
                "time_window_start": job.time_window_start.strftime("%H:%M:%S") if job.time_window_start else None,
                "time_window_end": job.time_window_end.strftime("%H:%M:%S") if job.time_window_end else None,
                "timezone": job.timezone,
            }
        
        # Update scheduling fields immediately to avoid duplicate scheduler triggers during background task startup
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

        # Create Run record
        exec_src = "automation" if trigger_type == "automation" else ("on_demand" if trigger_type == "manual" else (job.execution_source or "on_demand"))
        run = MassEvaluationRun(
            job_id=job_id,
            company_id=job.company_id,
            service_id=job.service_id,
            trigger_type=trigger_type,
            status="running",
            started_at=datetime.now(timezone.utc),
            effective_filters=effective_filters,
            execution_source=exec_src
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        
        # Launch background task
        task = asyncio.create_task(MassEvaluationService._execute_background_run(job_id, run.run_id, effective_filters))
        MassEvaluationService._running_tasks.add(task)
        task.add_done_callback(MassEvaluationService._running_tasks.discard)
        
        return run
    @staticmethod
    async def _execute_background_run(job_id: int, run_id: int, filters_payload: dict[str, Any]) -> None:
        """Background executor for mass analyses."""
        from app.db import get_engine
        engine = get_engine()
        
        # We need a new session in background
        async with AsyncSession(engine) as db:
            try:
                run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id)
                run_res = await db.execute(run_stmt)
                run = run_res.scalars().first()
                if not run:
                    logger.error("Run ID %d not found in background task", run_id)
                    return
                    
                effective_filters_snapshot = dict(run.effective_filters or {})
                run.heartbeat_at = datetime.now(timezone.utc)
                await db.commit()
                    
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

                # 1. Resolve and extract ALL parameters to local variables BEFORE any commits.
                # This completely prevents any lazy-loading/expiration/greenlet errors.
                # Snapshot ALL scalar job fields BEFORE any commit/await that would expire the ORM object.
                # Accessing job.xxx after a db.commit() triggers a lazy-load outside an async greenlet
                # → MissingGreenlet error. Keep all reads here, never read job.xxx after this block.
                prompt_id = job.prompt_id
                prompt_name = job.prompt_name
                prompt_version_id = job.prompt_version_id
                prompt_version_name = job.prompt_version_name
                prompt_version_label = job.prompt_version_label
                company_id = job.company_id  # FIX: snapshot here to avoid MissingGreenlet after commits
                execution_source = run.execution_source or job.execution_source or "on_demand"
                duration_min_seconds = job.duration_min_seconds
                duration_max_seconds = job.duration_max_seconds
                
                # Automation-only minimum duration pre-filter threshold
                eff_duration_min_seconds = duration_min_seconds
                if execution_source == "automation":
                    from app.config import get_settings
                    auto_min = get_settings().automation_min_duration_seconds
                    if eff_duration_min_seconds is None or eff_duration_min_seconds < auto_min:
                        eff_duration_min_seconds = auto_min

                direction = job.direction
                only_with_recording = job.only_with_recording
                max_calls = job.max_calls
                timezone_name = job.timezone
                schedule_enabled = job.schedule_enabled
                schedule_type = job.schedule_type
                schedule_time = job.schedule_time          # FIX: snapshot – used in calculate_next_run
                schedule_day_of_week = job.schedule_day_of_week    # FIX: snapshot
                schedule_day_of_month = job.schedule_day_of_month  # FIX: snapshot
                schedule_cron = job.schedule_cron

                # Resolve prompt snapshot content
                if prompt_version_id:
                    v_stmt = select(PromptVersion).where(PromptVersion.id == prompt_version_id)
                else:
                    v_stmt = (
                        select(PromptVersion)
                        .where(PromptVersion.prompt_id == prompt_id)
                        .order_by(PromptVersion.is_current.desc(), PromptVersion.id.desc())
                    )
                    
                v_res = await db.execute(v_stmt)
                v = v_res.scalars().first()
                if not v or not v.prompt:
                    raise ValueError(f"Could not resolve prompt text for Prompt ID {prompt_id}")
                    
                prompt_snapshot = v.prompt
                prompt_version_id = v.id

                # Resolve prompt's service
                prompt_stmt = select(Prompt).where(Prompt.prompt_id == prompt_id)
                prompt_res = await db.execute(prompt_stmt)
                prompt_obj = prompt_res.scalars().first()
                service_id = prompt_obj.service_id if prompt_obj else None
                base_structure_id = prompt_obj.base_structure_id if prompt_obj else None

                # Fetch Service details
                from app.models.services import Service
                from app.models.typologies import Typology
                from app.models.criteria import PromptCriterionTypology
                from app.models.prompts import BaseStructureTypology

                # Fallback to default service 'front'
                if not service_id:
                    s_stmt = select(Service.service_id).where(Service.service_key == "front")
                    s_res = await db.execute(s_stmt)
                    service_id = s_res.scalar()

                service_key = "front"
                service_name = "Front"
                if service_id:
                    s_stmt = select(Service).where(Service.service_id == service_id)
                    s_res = await db.execute(s_stmt)
                    service_obj = s_res.scalars().first()
                    if service_obj:
                        service_key = service_obj.service_key
                        service_name = service_obj.service_name

                # Fetch active typologies — prioritize base_structure associations, fallback to service
                typology_list = []
                if base_structure_id:
                    t_stmt = (
                        select(Typology)
                        .join(BaseStructureTypology, BaseStructureTypology.typology_id == Typology.typology_id)
                        .where(
                            BaseStructureTypology.base_structure_id == base_structure_id,
                            Typology.is_active == True,
                        )
                    )
                    t_res = await db.execute(t_stmt)
                    typology_list = t_res.scalars().all()

                if not typology_list and service_id:
                    # FALLBACK: base structure has no associations → all active typologies of service
                    t_stmt = select(Typology).where(Typology.service_id == service_id, Typology.is_active == True)
                    t_res = await db.execute(t_stmt)
                    typology_list = t_res.scalars().all()

                typology_by_key = {
                    t.typology_key: {
                        "typology_id": t.typology_id,
                        "typology_key": t.typology_key,
                        "typology_name": t.typology_name,
                    }
                    for t in typology_list
                }

                # Fetch active criteria and item-typology associations
                criteria_orm = await get_active_criteria(db, prompt_id)
                c_ids = [c.criterion_id for c in criteria_orm]
                
                criteria_snapshot = []
                for c in criteria_orm:
                    criteria_snapshot.append({
                        "criterion_id": c.criterion_id,
                        "criterion_key": c.criterion_key,
                        "criterion_name": c.criterion_name,
                        "criterion_type": c.criterion_type,
                        "output_key": c.output_key,
                        "feed_key": c.feed_key,
                    })
                assoc_map = {}
                if c_ids:
                    assoc_stmt = select(PromptCriterionTypology).where(PromptCriterionTypology.criterion_id.in_(c_ids))
                    assoc_res = await db.execute(assoc_stmt)
                    for assoc in assoc_res.scalars().all():
                        if assoc.criterion_id not in assoc_map:
                            assoc_map[assoc.criterion_id] = set()
                        assoc_map[assoc.criterion_id].add(assoc.typology_id)

                # 2. Query HubSpot
                hs_service = HubSpotService()
                
                not_found_call_ids = []
                random_trace_metadata = None
                if filters_payload.get("selection_mode") == "manual_call_ids":
                    calls = []
                    call_ids_list = filters_payload.get("call_ids") or []
                    for cid in call_ids_list:
                        try:
                            call_meta = await hs_service.get_call(cid)
                            dur_ms = call_meta.get("call_duration")
                            dur_sec = int(float(dur_ms) / 1000.0) if dur_ms else None
                            
                            calls.append({
                                "call_id": cid,
                                "hs_object_id": cid,
                                "recording_url": call_meta.get("recording_url"),
                                "hubspot_owner_id": call_meta.get("hubspot_owner_id"),
                                "call_timestamp": call_meta.get("call_timestamp"),
                                "call_duration_seconds": dur_sec,
                                "direction": call_meta.get("call_direction") or "all",
                                "status": call_meta.get("status")
                            })
                        except Exception as e_get:
                            logger.warning("Manual call ID %s not found in HubSpot during run: %s", cid, e_get)
                            not_found_call_ids.append(cid)
                elif filters_payload.get("job_mode") == "random_quality_monitoring":
                    date_from_str = filters_payload.get("date_from")
                    date_to_str = filters_payload.get("date_to")
                    date_from = safe_parse_datetime(date_from_str) if date_from_str else None
                    date_to = safe_parse_datetime(date_to_str) if date_to_str else None

                    if not date_from or not date_to:
                        d_from, d_to = resolve_date_filters(job, timezone_name)
                        if not date_from:
                            date_from = d_from
                        if not date_to:
                            date_to = d_to

                    search_filters = {
                        "date_from": date_from,
                        "date_to": date_to,
                        "agent_owner_ids": filters_payload.get("agent_owner_ids"),
                        "duration_min_seconds": eff_duration_min_seconds,
                        "duration_max_seconds": duration_max_seconds,
                        "direction": filters_payload.get("direction"),
                        "only_with_recording": filters_payload.get("only_with_recording"),
                        "calls_per_day": filters_payload.get("calls_per_day") or 20,
                        "time_window_start": filters_payload.get("time_window_start"),
                        "time_window_end": filters_payload.get("time_window_end"),
                        "timezone": timezone_name,
                    }

                    calls, random_trace_metadata = await MassEvaluationService.select_random_calls_for_quality_monitoring(
                        hs_service, search_filters
                    )
                    selected_calls = calls

                    eff_filters_dict = dict(effective_filters_snapshot)
                    eff_filters_dict.update(random_trace_metadata)

                    # Refetch run freshly to avoid ORM expiration & MissingGreenlet
                    fresh_run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id)
                    fresh_run_res = await db.execute(fresh_run_stmt)
                    fresh_run_obj = fresh_run_res.scalars().first()
                    if fresh_run_obj:
                        fresh_run_obj.calls_found = random_trace_metadata.get("total_candidates", len(calls))
                        fresh_run_obj.calls_selected = len(selected_calls)
                        fresh_run_obj.effective_filters = eff_filters_dict
                        fresh_run_obj.run_summary = {
                            "candidates_count_by_day": random_trace_metadata.get("candidates_count_by_day"),
                            "selected_count_by_day": random_trace_metadata.get("selected_count_by_day"),
                            "total_candidates": random_trace_metadata.get("total_candidates"),
                            "total_selected": random_trace_metadata.get("total_selected"),
                        }
                    effective_filters_snapshot = eff_filters_dict
                    await db.commit()
                else:
                    # Parse filter dates back to datetime
                    date_from_str = filters_payload.get("date_from")
                    date_to_str = filters_payload.get("date_to")
                    
                    date_from = safe_parse_datetime(date_from_str) if date_from_str else None
                    date_to = safe_parse_datetime(date_to_str) if date_to_str else None

                    # Automation lookback search margin:
                    # Expands the effective search start back by automation_call_lookback_minutes (default 120m)
                    # to capture long calls or delayed recording availability while preserving the logical watermark.
                    effective_search_from = date_from
                    if execution_source == "automation" and date_from is not None:
                        from app.config import get_settings
                        lookback_margin = get_settings().automation_call_lookback_minutes
                        if lookback_margin and lookback_margin > 0:
                            effective_search_from = date_from - timedelta(minutes=lookback_margin)
                    
                    search_filters = {
                        "date_from": effective_search_from,
                        "date_to": date_to,
                        "agent_owner_ids": filters_payload.get("agent_owner_ids"),
                        "duration_min_seconds": eff_duration_min_seconds,
                        "duration_max_seconds": duration_max_seconds,
                        "direction": filters_payload.get("direction"),
                        "only_with_recording": filters_payload.get("only_with_recording"),
                        "max_calls": filters_payload.get("max_calls"),
                        "time_window_start": filters_payload.get("time_window_start"),
                        "time_window_end": filters_payload.get("time_window_end"),
                        "timezone": timezone_name,
                    }
                    
                    calls = await hs_service.search_calls_for_mass_evaluation(search_filters)
                    run.calls_found = len(calls)
                    
                    # 3. Filter duplicates within the same execution and against DB completed calls
                    max_calls_val = filters_payload.get("max_calls")
                    if max_calls_val is None or max_calls_val <= 0:
                        max_calls_val = 10
                    elif max_calls_val > 500:
                        max_calls_val = 500
     
                    seen_call_ids = set()
                    unique_calls = []
                    for c in calls:
                        c_id = c["call_id"]
                        if c_id not in seen_call_ids:
                            seen_call_ids.add(c_id)
                            unique_calls.append(c)

                    # Deduplication against database:
                    # In automations (with lookback search), identify calls that already have status='completed'
                    # for the exact canonical identity (call_id + prompt_id) to skip reprocessing them.
                    already_completed_call_ids = set()
                    candidate_call_ids = [c["call_id"] for c in unique_calls if c.get("call_id")]
                    if candidate_call_ids and execution_source == "automation":
                        stmt_completed = (
                            select(MassEvaluationResult.call_id)
                            .where(
                                MassEvaluationResult.call_id.in_(candidate_call_ids),
                                MassEvaluationResult.prompt_id == prompt_id,
                                MassEvaluationResult.status == "completed"
                            )
                        )
                        res_completed = await db.execute(stmt_completed)
                        already_completed_call_ids = set(res_completed.scalars().all())

                    new_candidate_calls = [
                        c for c in unique_calls if c["call_id"] not in already_completed_call_ids
                    ]
                    skipped_completed_count = len(unique_calls) - len(new_candidate_calls)

                    selected_calls = new_candidate_calls[:max_calls_val]
                    run.calls_selected = len(selected_calls)
                    run.calls_skipped = skipped_completed_count
                    await db.commit()
                
                calls_analyzed = 0
                calls_skipped = skipped_completed_count if "skipped_completed_count" in locals() else 0
                calls_failed = 0
                cancelled_by_user = False
                
                # Process sequentially to avoid heavy concurrency issues
                for call in selected_calls:
                    # Cooperative cancellation check before processing each call
                    try:
                        async with AsyncSession(engine) as check_db:
                            status_stmt = select(MassEvaluationRun.status).where(MassEvaluationRun.run_id == run_id)
                            status_res = await check_db.execute(status_stmt)
                            run_db_status = status_res.scalar()
                            if run_db_status in ["cancelling", "cancel_requested", "cancelled"]:
                                logger.info("Mass evaluation run %d cancelled cooperatively.", run_id)
                                cancelled_by_user = True
                                break
                    except Exception as e_status:
                        logger.warning("Failed to check run cancellation status: %s", e_status)
 
                    call_id = call["call_id"]
                    recording_url = call["recording_url"]

                    # Pre-deletion by job_id + call_id is disabled.
                    # We now perform a clean upsert based on call_id + prompt_id when saving results.
                    pass
                    
                    if not recording_url:
                        # Skip
                        res_row = await MassEvaluationService._upsert_mass_evaluation_result(
                            db=db,
                            run_id=run_id,
                            job_id=job_id,
                            execution_source=execution_source,
                            call_id=call_id,
                            prompt_id=prompt_id,
                            defaults={
                                "hs_object_id": call["hs_object_id"],
                                "hubspot_owner_id": call["hubspot_owner_id"],
                                "call_timestamp": safe_parse_datetime(call["call_timestamp"]),
                                "call_duration_seconds": call["call_duration_seconds"],
                                "direction": call["direction"],
                                "prompt_version_id": prompt_version_id,
                                "prompt_name": prompt_name,
                                "prompt_version_name": prompt_version_name,
                                "prompt_version_label": prompt_version_label,
                                "prompt_snapshot": prompt_snapshot,
                                "status": "skipped",
                                "is_evaluable": None,
                                "non_evaluable_reason": None,
                                "error_message": "No recording URL present.",
                                "company_id": company_id,  # FIX: use local snapshot, not job ORM (expired after commit)
                                "service_id": service_id,
                                "service_key": service_key,
                                "service_name": service_name,
                                "result_json": None,
                                "items_json": None,
                                "evaluacion_global": None,
                                "typology_id": None,
                                "typology_key": None,
                                "typology_name": None
                            }
                        )
                        calls_skipped += 1
                        
                        # Incrementally update metrics in DB for polling
                        try:
                            fresh_run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id)
                            fresh_run_res = await db.execute(fresh_run_stmt)
                            fresh_run_obj = fresh_run_res.scalars().first()
                            if fresh_run_obj:
                                fresh_run_obj.calls_analyzed = calls_analyzed
                                fresh_run_obj.calls_skipped = calls_skipped
                                fresh_run_obj.calls_failed = calls_failed
                                fresh_run_obj.heartbeat_at = datetime.now(timezone.utc)
                                fresh_run_obj.run_summary = {
                                    "analyzed": calls_analyzed,
                                    "skipped": calls_skipped,
                                    "failed": calls_failed,
                                    "total": len(selected_calls),
                                    "not_found_call_ids": not_found_call_ids
                                }
                        except Exception as e_progress:
                            logger.warning("Failed to update progress in DB: %s", e_progress)
                        
                        await db.commit()
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
                        
                        # 1. Try direct keys in clean_result
                        detected_typology_key_raw = clean_result.get("tipo_llamada")
                        
                        # 2. Fallback: find criterion with key 'tipo_llamada' and use its output_key to look up in clean_result
                        if not detected_typology_key_raw:
                            for criterion in criteria_snapshot:
                                if criterion.get("criterion_key") == "tipo_llamada":
                                    out_key = criterion.get("output_key")
                                    if out_key and out_key in clean_result:
                                        detected_typology_key_raw = clean_result.get(out_key)
                                        break
                                        
                        # 3. Fallback: case-insensitive keys in clean_result containing 'tipo' and 'llamada'
                        if not detected_typology_key_raw:
                            for k, v in clean_result.items():
                                k_norm = k.lower().replace("_", "").replace("-", "").replace(" ", "")
                                if "tipollamada" in k_norm:
                                    detected_typology_key_raw = v
                                    break

                        detected_typology_key = normalize_tipo_llamada(detected_typology_key_raw)
                        matched_typology = None
                        if detected_typology_key:
                            if detected_typology_key in typology_by_key:
                                matched_typology = typology_by_key[detected_typology_key]
                            else:
                                for k, typ in typology_by_key.items():
                                    if k.lower().strip() == detected_typology_key:
                                        matched_typology = typ
                                        break

                        logger.info(
                            "Mass eval typology resolution: job_id=%s run_id=%s call_id=%s service_id=%s detected_raw=%s detected_norm=%s typology_keys=%s matched=%s",
                            job_id,
                            run_id,
                            call_id,
                            service_id,
                            detected_typology_key_raw,
                            detected_typology_key,
                            list(typology_by_key.keys()),
                            matched_typology,
                        )

                        typology_id = matched_typology["typology_id"] if matched_typology else None
                        typology_key = matched_typology["typology_key"] if matched_typology else None
                        typology_name = matched_typology["typology_name"] if matched_typology else None

                        # Resolve active criteria items
                        items = []
                        for criterion in criteria_snapshot:
                            output_key = criterion["output_key"]
                            feed_key = criterion["feed_key"]
 
                            # Determine if criterion is applicable
                            is_applicable = True
                            if matched_typology:
                                allowed_typologies = assoc_map.get(criterion["criterion_id"], set())
                                if allowed_typologies:
                                    is_applicable = (matched_typology["typology_id"] in allowed_typologies)

                            if is_applicable:
                                raw_value = clean_result.get(output_key) if output_key else None
                                feed_value = clean_result.get(feed_key) if feed_key else None
 
                                # Get clean/typed value
                                typed = map_criterion_value(raw_value, criterion["criterion_type"] or "text")
                                
                                # Resolve actual value
                                resolved_val = None
                                if criterion["criterion_type"] == "number":
                                    resolved_val = float(typed["value_number"]) if typed["value_number"] is not None else None
                                elif criterion["criterion_type"] == "boolean":
                                    resolved_val = typed["value_boolean"]
                                else:
                                    resolved_val = typed["value_text"] or typed["value_category"] or typed["raw_value"]
 
                                items.append({
                                    "criterion_id": criterion["criterion_id"],
                                    "criterion_key": criterion["criterion_key"],
                                    "name": criterion["criterion_name"],
                                    "type": criterion["criterion_type"],
                                    "output_key": output_key,
                                    "value": resolved_val,
                                    "feed": str(feed_value) if feed_value is not None else None,
                                    "not_applicable": False,
                                    "numeric_value": typed["value_number"],
                                    "text_value": typed["value_text"],
                                    "boolean_value": typed["value_boolean"],
                                    "category_value": typed["value_category"],
                                    "percentage_value": typed["value_number"] if criterion["criterion_type"] == "percentage" else None,
                                    "raw_value": typed["raw_value"],
                                })
                            else:
                                items.append({
                                    "criterion_id": criterion["criterion_id"],
                                    "criterion_key": criterion["criterion_key"],
                                    "name": criterion["criterion_name"],
                                    "type": criterion["criterion_type"],
                                    "output_key": output_key,
                                    "value": None,
                                    "feed": None,
                                    "not_applicable": True,
                                    "numeric_value": None,
                                    "text_value": None,
                                    "boolean_value": None,
                                    "category_value": None,
                                    "percentage_value": None,
                                    "raw_value": None,
                                })
                            
                        # Resolve agent name display
                        owner_id = call["hubspot_owner_id"]
                        resolved_agent = resolve_agent_display(None, owner_id)
                        
                        # Determine evaluability dynamically across all services
                        from app.utils.evaluability import determine_evaluability
                        is_eval, non_eval_reason = determine_evaluability(
                            typology_key=typology_key,
                            result_json=clean_result,
                            items=items,
                            call_duration_seconds=call.get("call_duration_seconds"),
                            status="completed"
                        )
                        
                        if is_eval is True:
                            from app.utils.scores import calculate_score_from_items
                            eval_val = calculate_score_from_items(items)
                            eval_decimal = Decimal(str(eval_val)) if eval_val is not None else None
                        else:
                            eval_decimal = None

                        # Persist Result via Upsert Helper
                        res_row = await MassEvaluationService._upsert_mass_evaluation_result(
                            db=db,
                            run_id=run_id,
                            job_id=job_id,
                            execution_source=execution_source,
                            call_id=call_id,
                            prompt_id=prompt_id,
                            defaults={
                                "hs_object_id": call["hs_object_id"],
                                "recording_url": recording_url,
                                "hubspot_owner_id": owner_id,
                                "agent_name": resolved_agent,
                                "call_timestamp": safe_parse_datetime(call["call_timestamp"]),
                                "call_duration_seconds": call["call_duration_seconds"],
                                "direction": call["direction"],
                                "prompt_version_id": prompt_version_id,
                                "prompt_name": prompt_name,
                                "prompt_version_name": prompt_version_name,
                                "prompt_version_label": prompt_version_label,
                                "prompt_snapshot": prompt_snapshot,
                                "status": "completed",
                                "is_evaluable": is_eval,
                                "non_evaluable_reason": non_eval_reason if is_eval is False else None,
                                "result_json": clean_result,
                                "items_json": items,
                                "evaluacion_global": eval_decimal,
                                "hubspot_metadata": call,
                                "company_id": company_id,  # FIX: use local snapshot, not job ORM (expired after commit)
                                "service_id": service_id,
                                "service_key": service_key,
                                "service_name": service_name,
                                "typology_id": typology_id,
                                "typology_key": typology_key,
                                "typology_name": typology_name,
                                "error_message": None
                            }
                        )

                        # Persist normalized criteria
                        from app.models.mass_evaluations import MassEvaluationCriterionResult
                        for item in items:
                            crit_res = MassEvaluationCriterionResult(
                                mass_analysis_id=res_row.mass_analysis_id,
                                run_id=run_id,
                                job_id=job_id,
                                execution_source=execution_source,
                                call_id=call_id,
                                hs_object_id=call["hs_object_id"],
                                prompt_id=prompt_id,
                                prompt_version_id=prompt_version_id,
                                criterion_id=item.get("criterion_id"),
                                criterion_key=item["criterion_key"],
                                criterion_name=item["name"],
                                criterion_type=item["type"],
                                value_raw=item.get("raw_value"),
                                numeric_value=item.get("numeric_value"),
                                text_value=item.get("text_value"),
                                boolean_value=item.get("boolean_value"),
                                category_value=item.get("category_value"),
                                percentage_value=item.get("percentage_value"),
                                feedback=item.get("feed"),
                                feed_key=None,
                                is_applicable=not item.get("not_applicable", False),
                                not_applicable=item.get("not_applicable", False),
                                service_id=service_id,
                                service_key=service_key,
                                service_name=service_name,
                                typology_id=typology_id,
                                typology_key=typology_key,
                                typology_name=typology_name
                            )
                            db.add(crit_res)
                        
                        calls_analyzed += 1
                        
                    except Exception as e_call:
                        import traceback
                        logger.warning(
                            "Call %s failed in mass evaluation job %d: %s\nStacktrace:\n%s", 
                            call_id, job_id, e_call, traceback.format_exc()
                        )
                        res_row = await MassEvaluationService._upsert_mass_evaluation_result(
                            db=db,
                            run_id=run_id,
                            job_id=job_id,
                            execution_source=execution_source,
                            call_id=call_id,
                            prompt_id=prompt_id,
                            defaults={
                                "hs_object_id": call["hs_object_id"],
                                "recording_url": recording_url,
                                "hubspot_owner_id": call["hubspot_owner_id"],
                                "call_timestamp": safe_parse_datetime(call["call_timestamp"]),
                                "call_duration_seconds": call["call_duration_seconds"],
                                "direction": call["direction"],
                                "prompt_version_id": prompt_version_id,
                                "prompt_name": prompt_name,
                                "prompt_version_name": prompt_version_name,
                                "prompt_version_label": prompt_version_label,
                                "prompt_snapshot": prompt_snapshot,
                                "status": "failed",
                                "is_evaluable": None,
                                "non_evaluable_reason": None,
                                "error_message": str(e_call),
                                "company_id": company_id,  # FIX: use local snapshot, not job ORM (expired after commit)
                                "service_id": service_id,
                                "service_key": service_key,
                                "service_name": service_name,
                                "result_json": None,
                                "items_json": None,
                                "evaluacion_global": None,
                                "typology_id": None,
                                "typology_key": None,
                                "typology_name": None
                            }
                        )
                        calls_failed += 1
                    finally:
                        # Explicitly release large audio and LLM response buffers
                        if "audio_bytes" in locals():
                            del audio_bytes
                        if "raw_response" in locals():
                            del raw_response
                        if "clean_result" in locals():
                            del clean_result
                        if "items" in locals():
                            del items
                        
                    # Incrementally update metrics in DB for polling
                    try:
                        fresh_run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id)
                        fresh_run_res = await db.execute(fresh_run_stmt)
                        fresh_run_obj = fresh_run_res.scalars().first()
                        if fresh_run_obj:
                            fresh_run_obj.calls_analyzed = calls_analyzed
                            fresh_run_obj.calls_skipped = calls_skipped
                            fresh_run_obj.calls_failed = calls_failed
                            fresh_run_obj.heartbeat_at = datetime.now(timezone.utc)
                            fresh_run_obj.run_summary = {
                                "analyzed": calls_analyzed,
                                "skipped": calls_skipped,
                                "failed": calls_failed,
                                "total": len(selected_calls),
                                "not_found_call_ids": not_found_call_ids
                            }
                    except Exception as e_progress:
                        logger.warning("Failed to update progress in DB: %s", e_progress)

                    # Commit per-call results to prevent loss of progress
                    await db.commit()

                    # Periodic memory check every 10 calls
                    if (calls_analyzed + calls_skipped + calls_failed) % 10 == 0:
                        rss_now = get_process_rss_mb()
                        if rss_now > 500.0:
                            gc.collect()
                            log_process_memory(f"mass_eval_run_{run_id}_progress")
                    
                # 4. Fetch fresh copies of run and job for final updates.
                # Since the loop commits frequently, run and job are expired.
                # Loading fresh copies guarantees they are in the active transaction.
                fresh_run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id)
                fresh_run_res = await db.execute(fresh_run_stmt)
                fresh_run_obj = fresh_run_res.scalars().first()
                
                fresh_job_stmt = select(MassEvaluationJob).where(MassEvaluationJob.job_id == job_id)
                fresh_job_res = await db.execute(fresh_job_stmt)
                fresh_job_obj = fresh_job_res.scalars().first()

                if cancelled_by_user:
                    final_status = "cancelled"
                else:
                    final_status = "completed"
                    if calls_failed > 0:
                        final_status = "completed_with_errors"

                if fresh_run_obj:
                    fresh_run_obj.calls_analyzed = calls_analyzed
                    fresh_run_obj.calls_skipped = calls_skipped
                    fresh_run_obj.calls_failed = calls_failed
                    fresh_run_obj.status = final_status
                    fresh_run_obj.finished_at = datetime.now(timezone.utc)
                    run_summary_payload = {
                        "analyzed": calls_analyzed,
                        "skipped": calls_skipped,
                        "failed": calls_failed,
                        "total": len(selected_calls),
                        "not_found_call_ids": not_found_call_ids
                    }
                    if random_trace_metadata:
                        run_summary_payload.update({
                            "candidates_count_by_day": random_trace_metadata.get("candidates_count_by_day"),
                            "selected_count_by_day": random_trace_metadata.get("selected_count_by_day"),
                            "total_candidates": random_trace_metadata.get("total_candidates"),
                            "total_selected": random_trace_metadata.get("total_selected"),
                        })
                    fresh_run_obj.run_summary = run_summary_payload

                if fresh_job_obj:
                    fresh_job_obj.last_run_at = datetime.now(timezone.utc)
                    if schedule_enabled:
                        fresh_job_obj.next_run_at = calculate_next_run(
                            schedule_type,
                            schedule_time,
                            schedule_day_of_week,
                            schedule_day_of_month,
                            schedule_cron,
                            timezone_name
                        )

                await db.commit()
                # Run garbage collection at run completion to immediately free peak memory
                gc.collect()
                log_process_memory(f"mass_eval_run_{run_id}_completed")
                logger.info("Mass evaluation job %d, run %d finished with status: %s", job_id, run_id, final_status)

                # Synchronization hook for MassAnalysisAutomationRun
                if execution_source == "automation":
                    try:
                        auto_stmt = select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.run_id == run_id)
                        auto_res = await db.execute(auto_stmt)
                        auto_run = auto_res.scalars().first()
                        if auto_run:
                            auto_run.status = "completed" if final_status in ["completed", "completed_with_errors"] else final_status
                            auto_run.finished_at = datetime.now(timezone.utc)
                            auto_run.calls_found = len(calls) if 'calls' in locals() else 0
                            auto_run.calls_selected = len(selected_calls) if 'selected_calls' in locals() else 0
                            auto_run.calls_skipped = calls_skipped
                            
                            aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == auto_run.automation_id)
                            aut_res = await db.execute(aut_stmt)
                            aut_obj = aut_res.scalars().first()
                            if aut_obj:
                                aut_obj.last_success_at = datetime.now(timezone.utc)
                            await db.commit()
                    except Exception as e_hook:
                        logger.error("Failed to sync automation run status on success: %s", e_hook)
            except Exception as e_run:
                await db.rollback()
                logger.error("Mass evaluation job %d run %d failed in background: %s", job_id, run_id, e_run, exc_info=True)
                try:
                    # Fetch fresh instance of run to write status safely using a new session
                    async with AsyncSession(engine) as fail_db:
                        try:
                            fresh_run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id)
                            fresh_run_res = await fail_db.execute(fresh_run_stmt)
                            fresh_run_obj = fresh_run_res.scalars().first()
                            if fresh_run_obj:
                                fresh_run_obj.status = "failed"
                                fresh_run_obj.error_message = str(e_run)
                                fresh_run_obj.finished_at = datetime.now(timezone.utc)
                                await fail_db.commit()

                                # Synchronization hook for MassAnalysisAutomationRun
                                try:
                                    auto_stmt = select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.run_id == run_id)
                                    auto_res = await fail_db.execute(auto_stmt)
                                    auto_run = auto_res.scalars().first()
                                    if auto_run:
                                        auto_run.status = "failed"
                                        auto_run.finished_at = datetime.now(timezone.utc)
                                        auto_run.error_message = str(e_run)
                                        
                                        aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == auto_run.automation_id)
                                        aut_res = await fail_db.execute(aut_stmt)
                                        aut_obj = aut_res.scalars().first()
                                        if aut_obj:
                                            aut_obj.last_error_at = datetime.now(timezone.utc)
                                            aut_obj.last_error_message = str(e_run)
                                        await fail_db.commit()
                                except Exception as e_hook_fail:
                                    logger.error("Failed to sync automation run failure status: %s", e_hook_fail)
                        except Exception as e_fail_inner:
                            await fail_db.rollback()
                            raise e_fail_inner
                        finally:
                            await fail_db.close()
                except Exception as e_inner:
                    logger.error("Failed to mark run as failed in database: %s", e_inner)
            finally:
                await db.close()

    @staticmethod
    async def list_runs(
        db: AsyncSession,
        job_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
        company_ids: list[int] | None = None,
        service_ids: list[int] | None = None,
    ) -> list[MassEvaluationRun]:
        stmt = select(MassEvaluationRun)
        filters = []
        if job_id is not None:
            filters.append(MassEvaluationRun.job_id == job_id)
        if status is not None:
            filters.append(MassEvaluationRun.status == status)
        if service_ids is not None:
            filters.append(MassEvaluationRun.service_id.in_(service_ids))
        elif company_ids:
            filters.append(MassEvaluationRun.company_id.in_(company_ids))
            
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
    async def cancel_run(db: AsyncSession, run_id: int) -> MassEvaluationRun:
        stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id)
        res = await db.execute(stmt)
        run = res.scalars().first()
        if not run:
            raise ValueError(f"Run ID {run_id} not found.")
            
        if run.status not in ["running", "pending"]:
            raise ValueError(f"Run ID {run_id} cannot be cancelled because its status is '{run.status}'.")
            
        run.status = "cancelling"
        await db.commit()
        await db.refresh(run)
        return run

    @staticmethod
    async def list_results(
        db: AsyncSession,
        run_id: int | None = None,
        job_id: int | None = None,
        agent_owner_id: str | None = None,
        call_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        execution_source: str | None = None,
        limit: int = 100,
        global_score_min: float | None = None,
        global_score_max: float | None = None,
        service_id: int | None = None,
        service_key: str | None = None,
        typology_key: str | None = None,
        offset: int | None = None,
        typology_ids: list[int] | None = None,
        duration_min_seconds: int | None = None,
        duration_max_seconds: int | None = None,
        direction: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        company_ids: list[int] | None = None,
        service_ids: list[int] | None = None,
        allowed_agent_ids: list[str] | None = None,
        status: str | None = None,
        item_filters: str | list | dict | None = None,
        sort_by: str | None = None,
        sort_order: str | None = "desc",
    ) -> list[MassEvaluationResult]:
        stmt = select(MassEvaluationResult).options(defer(MassEvaluationResult.prompt_snapshot))
        filters = []
        if status is not None and status.strip() and status.strip().lower() != "all":
            filters.append(MassEvaluationResult.status == status.strip().lower())
        if run_id is not None:
            filters.append(or_(
                MassEvaluationResult.run_id == run_id,
                MassEvaluationResult.source_run_id == run_id
            ))
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
        if created_from is not None:
            filters.append(MassEvaluationResult.created_at >= created_from)
        if created_to is not None:
            filters.append(MassEvaluationResult.created_at <= created_to)
        if execution_source is not None:
            if execution_source == "on_demand":
                filters.append(or_(
                    MassEvaluationResult.execution_source == "on_demand",
                    MassEvaluationResult.execution_source.is_(None)
                ))
            else:
                filters.append(MassEvaluationResult.execution_source == execution_source)
        if global_score_min is not None:
            filters.append(MassEvaluationResult.evaluacion_global.is_not(None))
            filters.append(MassEvaluationResult.evaluacion_global >= global_score_min)
        if global_score_max is not None:
            filters.append(MassEvaluationResult.evaluacion_global.is_not(None))
            filters.append(MassEvaluationResult.evaluacion_global <= global_score_max)
        if service_id is not None:
            filters.append(MassEvaluationResult.service_id == service_id)
        if service_key is not None:
            filters.append(MassEvaluationResult.service_key == service_key)
        if typology_key is not None:
            filters.append(MassEvaluationResult.typology_key == typology_key)
        if typology_ids:
            filters.append(MassEvaluationResult.typology_id.in_(typology_ids))
        if duration_min_seconds is not None:
            filters.append(MassEvaluationResult.call_duration_seconds >= duration_min_seconds)
        if duration_max_seconds is not None:
            filters.append(MassEvaluationResult.call_duration_seconds <= duration_max_seconds)
        if direction is not None:
            norm_d = normalize_direction(direction)
            if norm_d:
                filters.append(or_(
                    func.lower(MassEvaluationResult.direction) == norm_d,
                    func.lower(func.coalesce(MassEvaluationResult.result_json["inbound_outbound"].astext, "")) == norm_d
                ))

        if item_filters is not None:
            from app.utils.item_score_filters import parse_item_score_filters, build_item_filters_sql
            parsed_item_filters = parse_item_score_filters(item_filters)
            if parsed_item_filters:
                item_sql_conds = build_item_filters_sql(parsed_item_filters)
                filters.extend(item_sql_conds)

        # Multitenancy filters
        if allowed_agent_ids is not None:
            filters.append(MassEvaluationResult.hubspot_owner_id.in_(allowed_agent_ids))
        if service_ids is not None:
            filters.append(MassEvaluationResult.service_id.in_(service_ids))
        elif company_ids:
            filters.append(or_(
                MassEvaluationResult.company_id.in_(company_ids),
                MassEvaluationResult.company_id.is_(None)
            ))

        if filters:
            stmt = stmt.where(and_(*filters))

        order_clauses = []
        if sort_by:
            SORT_MAP = {
                "date": MassEvaluationResult.call_timestamp,
                "agent": func.lower(MassEvaluationResult.agent_name),
                "call_id": func.lower(MassEvaluationResult.call_id),
                "duration": MassEvaluationResult.call_duration_seconds,
                "score": MassEvaluationResult.evaluacion_global,
                "typology": func.lower(MassEvaluationResult.typology_name),
                "direction": func.lower(MassEvaluationResult.direction),
                "status": func.lower(MassEvaluationResult.status),
                "service": func.lower(MassEvaluationResult.service_name),
                "execution_source": func.lower(MassEvaluationResult.execution_source),
            }
            col_expr = SORT_MAP.get(sort_by)
            if col_expr is not None:
                if sort_order == "asc":
                    order_clauses.append(asc(col_expr).nulls_last())
                else:
                    order_clauses.append(desc(col_expr).nulls_last())

            if sort_by != "date":
                order_clauses.append(desc(MassEvaluationResult.call_timestamp).nulls_last())
            order_clauses.append(desc(MassEvaluationResult.mass_analysis_id))
        else:
            order_clauses.append(desc(MassEvaluationResult.call_timestamp).nulls_last())
            order_clauses.append(desc(MassEvaluationResult.mass_analysis_id))

        stmt = stmt.order_by(*order_clauses).limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)

        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def count_results(
        db: AsyncSession,
        run_id: int | None = None,
        job_id: int | None = None,
        agent_owner_id: str | None = None,
        call_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        execution_source: str | None = None,
        global_score_min: float | None = None,
        global_score_max: float | None = None,
        service_id: int | None = None,
        service_key: str | None = None,
        typology_key: str | None = None,
        typology_ids: list[int] | None = None,
        duration_min_seconds: int | None = None,
        duration_max_seconds: int | None = None,
        direction: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        company_ids: list[int] | None = None,
        service_ids: list[int] | None = None,
        allowed_agent_ids: list[str] | None = None,
        status: str | None = None,
        item_filters: str | list | dict | None = None,
    ) -> int:
        from sqlalchemy import func
        stmt = select(func.count(MassEvaluationResult.mass_analysis_id))
        filters = []
        if status is not None and status.strip() and status.strip().lower() != "all":
            filters.append(MassEvaluationResult.status == status.strip().lower())
        if run_id is not None:
            filters.append(or_(
                MassEvaluationResult.run_id == run_id,
                MassEvaluationResult.source_run_id == run_id
            ))
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
        if created_from is not None:
            filters.append(MassEvaluationResult.created_at >= created_from)
        if created_to is not None:
            filters.append(MassEvaluationResult.created_at <= created_to)
        if execution_source is not None:
            if execution_source == "on_demand":
                filters.append(or_(
                    MassEvaluationResult.execution_source == "on_demand",
                    MassEvaluationResult.execution_source.is_(None)
                ))
            else:
                filters.append(MassEvaluationResult.execution_source == execution_source)
        if global_score_min is not None:
            filters.append(MassEvaluationResult.evaluacion_global.is_not(None))
            filters.append(MassEvaluationResult.evaluacion_global >= global_score_min)
        if global_score_max is not None:
            filters.append(MassEvaluationResult.evaluacion_global.is_not(None))
            filters.append(MassEvaluationResult.evaluacion_global <= global_score_max)
        if service_id is not None:
            filters.append(MassEvaluationResult.service_id == service_id)
        if service_key is not None:
            filters.append(MassEvaluationResult.service_key == service_key)
        if typology_key is not None:
            filters.append(MassEvaluationResult.typology_key == typology_key)
        if typology_ids:
            filters.append(MassEvaluationResult.typology_id.in_(typology_ids))
        if duration_min_seconds is not None:
            filters.append(MassEvaluationResult.call_duration_seconds >= duration_min_seconds)
        if duration_max_seconds is not None:
            filters.append(MassEvaluationResult.call_duration_seconds <= duration_max_seconds)
        if direction is not None:
            norm_d = normalize_direction(direction)
            if norm_d:
                filters.append(or_(
                    func.lower(MassEvaluationResult.direction) == norm_d,
                    func.lower(func.coalesce(MassEvaluationResult.result_json["inbound_outbound"].astext, "")) == norm_d
                ))

        if item_filters is not None:
            from app.utils.item_score_filters import parse_item_score_filters, build_item_filters_sql
            parsed_item_filters = parse_item_score_filters(item_filters)
            if parsed_item_filters:
                item_sql_conds = build_item_filters_sql(parsed_item_filters)
                filters.extend(item_sql_conds)

        # Multitenancy filters
        if allowed_agent_ids is not None:
            filters.append(MassEvaluationResult.hubspot_owner_id.in_(allowed_agent_ids))
        if service_ids is not None:
            filters.append(MassEvaluationResult.service_id.in_(service_ids))
        elif company_ids:
            filters.append(or_(
                MassEvaluationResult.company_id.in_(company_ids),
                MassEvaluationResult.company_id.is_(None)
            ))

        if filters:
            stmt = stmt.where(and_(*filters))

        res = await db.execute(stmt)
        return res.scalar() or 0


    @staticmethod
    async def get_result(db: AsyncSession, mass_analysis_id: int) -> MassEvaluationResult | None:
        stmt = select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == mass_analysis_id)
        res = await db.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def _upsert_mass_evaluation_result(
        db: AsyncSession,
        run_id: int,
        job_id: int,
        execution_source: str,
        call_id: str,
        prompt_id: int,
        defaults: dict[str, Any]
    ) -> MassEvaluationResult:
        """
        Locates an existing mass evaluation result for the same call_id and prompt_id.
        If found, clears its child criteria results, updates the record's fields (audit-aware),
        and returns it. Otherwise, creates and returns a new record.
        """
        from app.models.mass_evaluations import MassEvaluationCriterionResult
        
        from sqlalchemy import case
        # 1. Search for existing record (order by status='completed' first, then mass_analysis_id DESC to get the latest completed first)
        stmt = select(MassEvaluationResult).where(
            MassEvaluationResult.call_id == call_id
        ).order_by(
            case((MassEvaluationResult.status == "completed", 0), else_=1),
            MassEvaluationResult.mass_analysis_id.desc()
        )
        res = await db.execute(stmt)
        existing = res.scalars().first()
        
        now_utc = datetime.now(timezone.utc)
        
        if existing:
            new_status = defaults.get("status")
            if existing.status == "completed" and new_status in ("failed", "skipped"):
                logger.info(
                    "[mass_eval_upsert] Preserving existing 'completed' result ID=%d for call_id=%s. "
                    "Skipping overwrite with '%s' status.",
                    existing.mass_analysis_id, call_id, new_status
                )
                existing.updated_at = now_utc
                return existing

            # Delete child criteria records first
            await db.execute(
                delete(MassEvaluationCriterionResult).where(
                    MassEvaluationCriterionResult.mass_analysis_id == existing.mass_analysis_id
                )
            )
            await db.flush()
            
            # Set source audit fields if not already populated
            if existing.source_job_id is None:
                existing.source_job_id = existing.job_id
            if existing.source_run_id is None:
                existing.source_run_id = existing.run_id
                
            # Update parent fields
            existing.run_id = run_id
            existing.job_id = job_id
            existing.execution_source = execution_source
            existing.prompt_id = prompt_id
            existing.last_evaluated_at = now_utc
            existing.updated_at = now_utc
            
            for k, v in defaults.items():
                setattr(existing, k, v)
                
            return existing
        else:
            # Create new row
            new_row = MassEvaluationResult(
                run_id=run_id,
                job_id=job_id,
                execution_source=execution_source,
                call_id=call_id,
                prompt_id=prompt_id,
                source_job_id=job_id,
                source_run_id=run_id,
                last_evaluated_at=now_utc,
                created_at=now_utc,
                updated_at=now_utc,
                **defaults
            )
            db.add(new_row)
            await db.flush()
            return new_row

    @staticmethod
    async def run_due_jobs(db: AsyncSession) -> dict[str, int]:
        """Finds due scheduled jobs, launches them if not already running, and updates schedules."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        # Select all active scheduled jobs that are due
        stmt = select(MassEvaluationJob).where(
            MassEvaluationJob.is_active == True,
            MassEvaluationJob.schedule_enabled == True,
            MassEvaluationJob.schedule_type != "manual",
            MassEvaluationJob.next_run_at != None,
            MassEvaluationJob.next_run_at <= now
        )
        res = await db.execute(stmt)
        due_jobs = res.scalars().all()
        
        due_count = len(due_jobs)
        launched_count = 0
        
        for job in due_jobs:
            # Check if there is already an active running run
            stmt_lock = select(MassEvaluationRun).where(
                MassEvaluationRun.job_id == job.job_id,
                MassEvaluationRun.status == "running"
            )
            res_lock = await db.execute(stmt_lock)
            active_run = res_lock.scalars().first()
            if active_run:
                logger.info("Scheduler skipped due job ID %d ('%s') because it is already running.", job.job_id, job.job_name)
                continue
                
            try:
                # Trigger the run (which handles schedule update, run creation, background spawn and commits)
                await MassEvaluationService.run_job(db, job.job_id, trigger_type="scheduled")
                launched_count += 1
                logger.info("Scheduler successfully launched due job ID %d ('%s').", job.job_id, job.job_name)
            except Exception as e:
                logger.error("Scheduler failed to launch due job ID %d ('%s'): %s", job.job_id, job.job_name, e)
                
        return {"due_jobs_count": due_count, "launched_jobs_count": launched_count}

    @staticmethod
    async def cleanup_stale_runs(db: AsyncSession, threshold_minutes: int = 10) -> int:
        """
        Find runs in 'running' status that have not updated their heartbeat (or started) for
        longer than threshold_minutes, and mark them as failed.
        """
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
        
        # Select all running runs that are stale
        stmt = select(MassEvaluationRun).where(
            MassEvaluationRun.status == "running",
            or_(
                MassEvaluationRun.heartbeat_at == None,
                MassEvaluationRun.heartbeat_at <= cutoff
            ),
            MassEvaluationRun.started_at <= cutoff
        )
        
        res = await db.execute(stmt)
        stale_runs = res.scalars().all()
        
        cleaned_count = 0
        for run in stale_runs:
            # Query existing results for this run to avoid false failure on runs that finished processing
            stmt_res = select(
                func.count(MassEvaluationResult.mass_analysis_id).label("total"),
                func.count(case((MassEvaluationResult.status == "completed", 1))).label("completed"),
                func.count(case((MassEvaluationResult.status == "failed", 1))).label("failed"),
                func.count(case((MassEvaluationResult.status == "skipped", 1))).label("skipped"),
            ).where(MassEvaluationResult.run_id == run.run_id)
            res_counts = (await db.execute(stmt_res)).first()

            total_res = res_counts.total if res_counts else 0
            completed_res = res_counts.completed if res_counts else 0
            failed_res = res_counts.failed if res_counts else 0
            skipped_res = res_counts.skipped if res_counts else 0

            # Resolve status defensibly based on actual persisted call results
            if completed_res > 0 and failed_res == 0:
                resolved_status = "completed"
                resolved_err = None
            elif completed_res > 0 and failed_res > 0:
                resolved_status = "completed_with_errors"
                resolved_err = f"Completed with {failed_res} error(s) before heartbeat timeout."
            else:
                resolved_status = "failed"
                resolved_err = f"Execution abandoned (no heartbeat updated for more than {threshold_minutes} minutes)."

            logger.warning(
                "Mass evaluation run %d (job %d) is stale (results: total=%d, completed=%d, failed=%d). Resolving status to '%s'.",
                run.run_id, run.job_id, total_res, completed_res, failed_res, resolved_status
            )
            run.status = resolved_status
            run.error_message = resolved_err
            if not run.finished_at:
                run.finished_at = datetime.now(timezone.utc)
            if total_res > 0:
                run.calls_analyzed = completed_res + failed_res
                run.calls_failed = failed_res
                run.calls_skipped = skipped_res

            # If it is an automation run, sync the associated automation run
            try:
                auto_stmt = select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.run_id == run.run_id)
                auto_res = await db.execute(auto_stmt)
                auto_run = auto_res.scalars().first()
                if auto_run:
                    auto_run.status = resolved_status
                    if not auto_run.finished_at:
                        auto_run.finished_at = run.finished_at
                    auto_run.error_message = resolved_err
                    if total_res > 0:
                        auto_run.calls_skipped = run.calls_skipped

                    aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == auto_run.automation_id)
                    aut_res = await db.execute(aut_stmt)
                    aut_obj = aut_res.scalars().first()
                    if aut_obj:
                        if resolved_status in ["completed", "completed_with_errors"]:
                            aut_obj.last_success_at = datetime.now(timezone.utc)
                        else:
                            aut_obj.last_error_at = datetime.now(timezone.utc)
                            aut_obj.last_error_message = resolved_err
            except Exception as e_auto:
                logger.error("Failed to cleanup associated automation run for run %d: %s", run.run_id, e_auto)

            cleaned_count += 1

        if cleaned_count > 0:
            await db.commit()

        return cleaned_count

    @staticmethod
    async def backfill_mass_criterion_typologies(db: AsyncSession, payload: Any) -> dict[str, Any]:
        from app.models.mass_evaluations import MassEvaluationCriterionResult, MassEvaluationResult
        from app.models.typologies import Typology
        from collections import defaultdict

        # 1. Distinct mass_analysis_id with null typology_key
        stmt_nulls = select(MassEvaluationCriterionResult.mass_analysis_id).where(
            MassEvaluationCriterionResult.typology_key.is_(None)
        ).distinct()
        res_nulls = await db.execute(stmt_nulls)
        null_analysis_ids = list(res_nulls.scalars().all())

        if not null_analysis_ids:
            if payload.mode == "dry_run":
                return {
                    "analyses_with_null_typology": 0,
                    "analyses_resolvable": 0,
                    "analyses_unresolved": 0,
                    "rows_to_update": 0,
                    "examples": []
                }
            else:
                return {
                    "updated_rows": 0,
                    "updated_analyses": 0,
                    "unresolved_analyses": 0,
                    "warnings": ["No analyses with null typologies found."]
                }

        # 2. Get all typology_key rows to extract 'tipo_llamada'
        stmt_tipo = select(MassEvaluationCriterionResult).where(
            MassEvaluationCriterionResult.mass_analysis_id.in_(null_analysis_ids),
            MassEvaluationCriterionResult.criterion_key == 'tipo_llamada'
        )
        res_tipo = await db.execute(stmt_tipo)
        tipo_rows = res_tipo.scalars().all()

        tipo_map_by_analysis = {r.mass_analysis_id: r for r in tipo_rows}

        # 3. Load all typologies
        stmt_typs = select(Typology)
        res_typs = await db.execute(stmt_typs)
        all_typologies = res_typs.scalars().all()

        typology_map = {}
        for t in all_typologies:
            # Map (service_id, typology_key.lower().strip()) -> Typology
            key = (t.service_id, t.typology_key.lower().strip())
            typology_map[key] = t

        # 4. Get all criterion rows where typology_key is NULL for grouping
        stmt_null_rows = select(MassEvaluationCriterionResult).where(
            MassEvaluationCriterionResult.mass_analysis_id.in_(null_analysis_ids),
            MassEvaluationCriterionResult.typology_key.is_(None)
        )
        res_null_rows = await db.execute(stmt_null_rows)
        null_rows = res_null_rows.scalars().all()

        rows_by_analysis = defaultdict(list)
        for r in null_rows:
            rows_by_analysis[r.mass_analysis_id].append(r)

        # 5. Process
        analyses_with_null_typology = len(null_analysis_ids)
        analyses_resolvable = 0
        analyses_unresolved = 0
        rows_to_update = 0
        examples = []

        resolvable_data = [] # List of tuples: (mass_analysis_id, Typology object, list of affected rows, tipo_row)

        for mass_analysis_id in null_analysis_ids:
            tipo_row = tipo_map_by_analysis.get(mass_analysis_id)
            if not tipo_row:
                analyses_unresolved += 1
                continue

            detected_value = tipo_row.category_value or tipo_row.text_value
            service_id = tipo_row.service_id

            if not detected_value or not service_id:
                analyses_unresolved += 1
                continue

            norm_val = str(detected_value).lower().strip()
            t_obj = typology_map.get((service_id, norm_val))

            if not t_obj:
                analyses_unresolved += 1
                continue

            analyses_resolvable += 1
            affected_rows = rows_by_analysis.get(mass_analysis_id, [])
            rows_to_update += len(affected_rows)

            resolvable_data.append((mass_analysis_id, t_obj, affected_rows, tipo_row))

            if len(examples) < 10:
                examples.append({
                    "mass_analysis_id": mass_analysis_id,
                    "call_id": tipo_row.call_id,
                    "detected_value": detected_value,
                    "resolved_typology_key": t_obj.typology_key,
                    "resolved_typology_name": t_obj.typology_name,
                    "rows_affected": len(affected_rows)
                })

        if payload.mode == "dry_run":
            return {
                "analyses_with_null_typology": analyses_with_null_typology,
                "analyses_resolvable": analyses_resolvable,
                "analyses_unresolved": analyses_unresolved,
                "rows_to_update": rows_to_update,
                "examples": examples
            }

        # payload.mode == "execute"
        updated_rows = 0
        updated_analyses = 0
        unresolved_analyses = analyses_unresolved
        warnings = []

        for mass_analysis_id, t_obj, affected_rows, tipo_row in resolvable_data:
            try:
                # Update criterion results
                for r in affected_rows:
                    r.typology_id = t_obj.typology_id
                    r.typology_key = t_obj.typology_key
                    r.typology_name = t_obj.typology_name

                # Update parent result if present
                stmt_parent = select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == mass_analysis_id)
                res_parent = await db.execute(stmt_parent)
                parent = res_parent.scalars().first()
                if parent:
                    parent.typology_id = t_obj.typology_id
                    parent.typology_key = t_obj.typology_key
                    parent.typology_name = t_obj.typology_name

                updated_rows += len(affected_rows)
                updated_analyses += 1
            except Exception as ex:
                warnings.append(f"Error updating analysis ID {mass_analysis_id}: {str(ex)}")
                unresolved_analyses += 1

        if updated_analyses > 0:
            await db.commit()

        return {
            "updated_rows": updated_rows,
            "updated_analyses": updated_analyses,
            "unresolved_analyses": unresolved_analyses,
            "warnings": warnings
        }

    # ── Automation Management ──────────────────────────────────────────────────

    @staticmethod
    async def create_automation(db: AsyncSession, payload: MassAnalysisAutomationCreate) -> MassAnalysisAutomation:
        """Create a new automation configuration and its corresponding background job."""
        # 1. First validate if Prompt exists
        stmt_p = select(Prompt).where(Prompt.prompt_id == payload.prompt_id)
        res_p = await db.execute(stmt_p)
        prompt = res_p.scalars().first()
        if not prompt:
            raise ValueError(f"El prompt ID {payload.prompt_id} no existe.")

        # 2. Create the underlying background job with execution_source = 'automation'
        job = MassEvaluationJob(
            job_name=f"[Auto] {payload.name}",
            description=payload.description,
            is_active=payload.is_active,
            prompt_id=payload.prompt_id,
            prompt_version_id=payload.prompt_version_id,
            agent_owner_ids=payload.agent_owner_ids,
            duration_min_seconds=payload.min_duration_seconds,
            direction=payload.direction_filter,
            only_with_recording=True,
            max_calls=500,
            execution_source="automation",
            date_mode="relative",
            relative_days=1,
            timezone="Europe/Madrid",
            schedule_enabled=False
        )
        await enrich_job_prompt_info(db, job)
        db.add(job)
        await db.flush()

        # 3. Create the Automation Configuration
        automation = MassAnalysisAutomation(
            name=payload.name,
            description=payload.description,
            is_active=payload.is_active,
            interval_minutes=payload.interval_minutes,
            lookback_minutes=payload.lookback_minutes,
            delay_minutes=payload.delay_minutes,
            service_id=payload.service_id,
            prompt_id=payload.prompt_id,
            prompt_version_id=payload.prompt_version_id,
            min_duration_seconds=payload.min_duration_seconds,
            direction_filter=payload.direction_filter,
            agent_owner_ids=payload.agent_owner_ids,
            job_id=job.job_id
        )
        db.add(automation)
        await db.commit()
        await db.refresh(automation)
        return automation

    @staticmethod
    async def update_automation(
        db: AsyncSession, automation_id: int, payload: MassAnalysisAutomationUpdate
    ) -> MassAnalysisAutomation | None:
        """Update an automation configuration and synchronize changes to its background job."""
        stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == automation_id)
        res = await db.execute(stmt)
        automation = res.scalars().first()
        if not automation:
            return None

        update_data = payload.model_dump(exclude_unset=True)
        for k, v in update_data.items():
            setattr(automation, k, v)

        # Synchronize changes to underlying permanent job
        if automation.job_id:
            stmt_j = select(MassEvaluationJob).where(MassEvaluationJob.job_id == automation.job_id)
            res_j = await db.execute(stmt_j)
            job = res_j.scalars().first()
            if job:
                if "name" in update_data:
                    job.job_name = f"[Auto] {payload.name}"
                if "description" in update_data:
                    job.description = payload.description
                if "is_active" in update_data:
                    job.is_active = payload.is_active
                if "prompt_id" in update_data:
                    job.prompt_id = payload.prompt_id
                if "prompt_version_id" in update_data:
                    job.prompt_version_id = payload.prompt_version_id
                if "agent_owner_ids" in update_data:
                    job.agent_owner_ids = payload.agent_owner_ids
                if "min_duration_seconds" in update_data:
                    job.duration_min_seconds = payload.min_duration_seconds
                if "direction_filter" in update_data:
                    job.direction = payload.direction_filter

                await enrich_job_prompt_info(db, job)

        await db.commit()
        await db.refresh(automation)
        return automation

    @staticmethod
    async def get_automation(db: AsyncSession, automation_id: int) -> MassAnalysisAutomation | None:
        """Retrieve details of an automation configuration."""
        stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == automation_id)
        res = await db.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def list_automations(
        db: AsyncSession,
        limit: int = 100,
        active: str | None = None,
        include_inactive: bool | None = None,
        include_archived: bool = False,
        company_ids: list[int] | None = None,
        service_ids: list[int] | None = None,
    ) -> list[MassAnalysisAutomation]:
        """List automation configurations with flexible active/inactive status filtering."""
        from app.models.services import Service
        stmt = select(MassAnalysisAutomation)

        active_norm = str(active).strip().lower() if active is not None else None
        if active_norm in ("true", "1"):
            stmt = stmt.where(MassAnalysisAutomation.is_active == True)
        elif active_norm in ("false", "0"):
            stmt = stmt.where(MassAnalysisAutomation.is_active == False)
        elif include_inactive is False and active_norm is None:
            stmt = stmt.where(MassAnalysisAutomation.is_active == True)
        # Default (active=None or active='all'): return active and inactive automations
        
        if service_ids is not None:
            stmt = stmt.where(MassAnalysisAutomation.service_id.in_(service_ids))
        elif company_ids:
            stmt = stmt.join(Service, Service.service_id == MassAnalysisAutomation.service_id).where(Service.company_id.in_(company_ids))

        stmt = stmt.order_by(desc(MassAnalysisAutomation.automation_id)).limit(limit)
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def delete_automation(db: AsyncSession, automation_id: int, soft_delete: bool = True) -> bool:
        """Deactivate or delete an automation and its background job."""
        stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == automation_id)
        res = await db.execute(stmt)
        automation = res.scalars().first()
        if not automation:
            return False

        if soft_delete:
            automation.is_active = False
            if automation.job_id:
                stmt_j = select(MassEvaluationJob).where(MassEvaluationJob.job_id == automation.job_id)
                res_j = await db.execute(stmt_j)
                job = res_j.scalars().first()
                if job:
                    job.is_active = False
            await db.commit()
        else:
            if automation.job_id:
                stmt_j = select(MassEvaluationJob).where(MassEvaluationJob.job_id == automation.job_id)
                res_j = await db.execute(stmt_j)
                job = res_j.scalars().first()
                if job:
                    await db.delete(job)
            await db.delete(automation)
            await db.commit()
        return True

    @staticmethod
    async def cleanup_stale_automation_runs(
        db: AsyncSession,
        threshold_minutes: int | None = None,
        company_ids: list[int] | None = None,
        service_ids: list[int] | None = None,
    ) -> int:
        """
        Finds MassAnalysisAutomationRun records with status 'running' that have been running
        longer than threshold_minutes (defaulting to Settings.automation_running_stale_after_minutes),
        marks them as 'failed' with error_message='Marked as stale after exceeding AUTOMATION_RUNNING_STALE_AFTER_MINUTES',
        updates finished_at and updates the associated automation and underlying MassEvaluationRun if applicable.
        """
        from datetime import datetime, timedelta, timezone
        from app.config import get_settings
        settings = get_settings()

        if threshold_minutes is None:
            threshold_minutes = settings.automation_running_stale_after_minutes or 60

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=threshold_minutes)

        stmt = select(MassAnalysisAutomationRun).where(
            MassAnalysisAutomationRun.status == "running",
            MassAnalysisAutomationRun.started_at <= cutoff
        )
        if service_ids is not None or company_ids is not None:
            from app.models.services import Service
            stmt = stmt.join(MassAnalysisAutomation, MassAnalysisAutomation.automation_id == MassAnalysisAutomationRun.automation_id)
            if service_ids is not None:
                stmt = stmt.where(MassAnalysisAutomation.service_id.in_(service_ids))
            elif company_ids:
                stmt = stmt.join(Service, Service.service_id == MassAnalysisAutomation.service_id).where(Service.company_id.in_(company_ids))

        res = await db.execute(stmt)
        stale_auto_runs = res.scalars().all()

        cleaned_count = 0
        for auto_run in stale_auto_runs:
            started_at = auto_run.started_at
            if started_at and started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            age_minutes = int((now - started_at).total_seconds() / 60) if started_at else threshold_minutes

            logger.warning(
                "[automation_scheduler] stale_running_marked_failed automation_id=%d run_id=%d age_minutes=%d threshold_minutes=%d",
                auto_run.automation_id, auto_run.automation_run_id, age_minutes, threshold_minutes
            )

            err_msg = f"Marked as stale after exceeding AUTOMATION_RUNNING_STALE_AFTER_MINUTES ({threshold_minutes} minutes)"
            auto_run.status = "failed"
            auto_run.finished_at = now
            auto_run.error_message = err_msg

            # Update parent automation
            aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == auto_run.automation_id)
            aut_res = await db.execute(aut_stmt)
            aut_obj = aut_res.scalars().first()
            if aut_obj:
                aut_obj.last_error_at = now
                aut_obj.last_error_message = err_msg

            # Also update associated MassEvaluationRun if present
            if auto_run.run_id:
                try:
                    run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == auto_run.run_id)
                    run_res = await db.execute(run_stmt)
                    sub_run = run_res.scalars().first()
                    if sub_run and sub_run.status == "running":
                        sub_run.status = "failed"
                        sub_run.finished_at = now
                        sub_run.error_message = err_msg
                except Exception as e_sub:
                    logger.error("Failed to mark associated MassEvaluationRun %s as failed: %s", auto_run.run_id, e_sub)

            cleaned_count += 1

        if cleaned_count > 0:
            await db.commit()

        return cleaned_count

    @staticmethod
    async def mark_automation_run_stale_failed(
        db: AsyncSession,
        run_id: int,
        context: Any = None
    ) -> MassAnalysisAutomationRun | None:
        """
        Manually marks an automation run (matched by automation_run_id OR linked run_id)
        as failed if stuck in running or stale state.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        stmt = select(MassAnalysisAutomationRun).where(
            or_(
                MassAnalysisAutomationRun.automation_run_id == run_id,
                MassAnalysisAutomationRun.run_id == run_id
            )
        )
        res = await db.execute(stmt)
        auto_run = res.scalars().first()
        if not auto_run:
            return None

        # Scoping validation
        if context and not context.is_super_admin:
            aut_stmt = select(MassAnalysisAutomation.service_id).where(MassAnalysisAutomation.automation_id == auto_run.automation_id)
            aut_res = await db.execute(aut_stmt)
            service_id = aut_res.scalar()
            if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=403,
                    detail="Acceso denegado: esta automatización pertenece a un servicio no asignado."
                )

        err_msg = "Manually marked as stale/failed by administrator"
        auto_run.status = "failed"
        auto_run.finished_at = now
        auto_run.error_message = err_msg

        # Update parent automation
        aut_stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == auto_run.automation_id)
        aut_res = await db.execute(aut_stmt)
        aut_obj = aut_res.scalars().first()
        if aut_obj:
            aut_obj.last_error_at = now
            aut_obj.last_error_message = err_msg

        # Update linked MassEvaluationRun
        if auto_run.run_id:
            try:
                run_stmt = select(MassEvaluationRun).where(MassEvaluationRun.run_id == auto_run.run_id)
                run_res = await db.execute(run_stmt)
                sub_run = run_res.scalars().first()
                if sub_run:
                    sub_run.status = "failed"
                    sub_run.finished_at = now
                    sub_run.error_message = err_msg
            except Exception as e_sub:
                logger.error("Failed to mark associated MassEvaluationRun as failed: %s", e_sub)

        await db.commit()
        await db.refresh(auto_run)
        return auto_run

    @staticmethod
    async def get_automation_next_window(
        db: AsyncSession,
        automation: MassAnalysisAutomation | int,
        now: datetime | None = None,
    ) -> tuple[datetime, datetime, bool, str]:
        """
        Calculates continuous search window for an automation to eliminate coverage gaps.
        Rule 1: Anchor window_from on last_successful_window_to (from latest completed run).
                If none, fallback to created_at (safe max lookback) or now - lookback - delay.
        Rule 2: window_to = min(window_from + lookback, now - delay).
        Rule 3: If window_to <= window_from, is_ready=False (not_due_window_not_ready).
        Returns:
            (window_from, window_to, is_ready, source_desc)
        """
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        if isinstance(automation, int):
            target_id = automation
        else:
            try:
                from sqlalchemy import inspect as sa_inspect
                insp = sa_inspect(automation)
                if insp and insp.identity:
                    target_id = insp.identity[0]
                else:
                    target_id = automation.__dict__.get("automation_id")
            except Exception:
                target_id = getattr(automation, "automation_id", None)

        if target_id is not None:
            stmt_aut = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == target_id)
            res_aut = await db.execute(stmt_aut)
            aut_obj = res_aut.scalars().first()
            if not aut_obj:
                raise ValueError(f"La automatización ID {target_id} no existe.")
        else:
            aut_obj = automation

        automation_id = aut_obj.automation_id
        lookback_min = aut_obj.lookback_minutes or 30
        delay_min = aut_obj.delay_minutes or 5

        # 1. Query latest completed automation run with non-null window_to
        stmt_last = (
            select(MassAnalysisAutomationRun.window_to)
            .where(
                MassAnalysisAutomationRun.automation_id == automation_id,
                MassAnalysisAutomationRun.status.in_(["completed", "completed_empty"]),
                MassAnalysisAutomationRun.window_to.isnot(None),
            )
            .order_by(
                desc(MassAnalysisAutomationRun.window_to),
                desc(MassAnalysisAutomationRun.automation_run_id),
            )
            .limit(1)
        )
        res_last = await db.execute(stmt_last)
        last_window_to = res_last.scalar()

        if last_window_to is not None:
            if last_window_to.tzinfo is None:
                last_window_to = last_window_to.replace(tzinfo=timezone.utc)
            window_from = last_window_to
            source_desc = "continuous"
        else:
            # First run: start from (created_at or now) - lookback - delay
            base_time = aut_obj.created_at or now
            if base_time.tzinfo is None:
                base_time = base_time.replace(tzinfo=timezone.utc)
            initial_start = base_time - timedelta(minutes=lookback_min + delay_min)
            min_safe_start = now - timedelta(hours=24)
            window_from = max(initial_start, min_safe_start)
            source_desc = "initial_lookback"

        max_window_to = now - timedelta(minutes=delay_min)
        target_window_to = window_from + timedelta(minutes=lookback_min)
        window_to = min(target_window_to, max_window_to)

        is_ready = window_to > window_from
        return window_from, window_to, is_ready, source_desc

    @staticmethod
    async def run_automation_run(
        db: AsyncSession, automation: MassAnalysisAutomation | int, trigger_type: str = "scheduled"
    ) -> MassAnalysisAutomationRun:
        """Computes continuous call search window, generates an automation execution run, and spawns the job."""
        if isinstance(automation, int):
            stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == automation)
            res = await db.execute(stmt)
            automation = res.scalars().first()
            if not automation:
                raise ValueError(f"La automatización ID {automation} no existe.")
        else:
            db.add(automation)

        now = datetime.now(timezone.utc)
        automation_id = automation.automation_id

        # Calculate continuous window
        window_from, window_to, is_ready, source_desc = await MassEvaluationService.get_automation_next_window(
            db, automation, now=now
        )

        if not is_ready:
            logger.info(
                "[automation_window] automation_id=%d not_due_window_not_ready window_from=%s window_to=%s (source=%s)",
                automation_id, window_from.isoformat(), window_to.isoformat(), source_desc
            )
            auto_run = MassAnalysisAutomationRun(
                automation_id=automation_id,
                status="skipped",
                started_at=now,
                finished_at=now,
                window_from=window_from,
                window_to=window_to,
                calls_found=0,
                calls_selected=0,
                calls_skipped=0,
                error_message="not_due_window_not_ready",
            )
            db.add(auto_run)
            await db.commit()
            await db.refresh(auto_run)
            return auto_run

        # Check lock: verify if this automation already has a running automation execution
        stmt_lock = select(MassAnalysisAutomationRun).where(
            MassAnalysisAutomationRun.automation_id == automation_id,
            MassAnalysisAutomationRun.status == "running"
        )
        res_lock = await db.execute(stmt_lock)
        active_auto_run = res_lock.scalars().first()
        if active_auto_run:
            from app.config import get_settings
            threshold_minutes = get_settings().automation_running_stale_after_minutes or 60
            st_at = active_auto_run.started_at
            if st_at and st_at.tzinfo is None:
                st_at = st_at.replace(tzinfo=timezone.utc)
            age_minutes = int((now - st_at).total_seconds() / 60) if st_at else 0

            if age_minutes >= threshold_minutes:
                logger.warning(
                    "[automation_scheduler] stale_running_marked_failed automation_id=%d run_id=%d age_minutes=%d threshold_minutes=%d",
                    automation_id, active_auto_run.automation_run_id, age_minutes, threshold_minutes
                )
                err_msg = f"Marked as stale after exceeding AUTOMATION_RUNNING_STALE_AFTER_MINUTES ({threshold_minutes} minutes)"
                active_auto_run.status = "failed"
                active_auto_run.finished_at = now
                active_auto_run.error_message = err_msg
                automation.last_error_at = now
                automation.last_error_message = err_msg
                await db.flush()
            else:
                raise ValueError(f"La automatización {automation_id} ya tiene una ejecución activa (Run ID {active_auto_run.automation_run_id})")

        logger.info(
            "[automation_window] automation_id=%d window_from=%s window_to=%s source=%s gap_closed=true",
            automation_id, window_from.isoformat(), window_to.isoformat(), source_desc
        )

        # Create Run record and COMMIT it immediately so it is persisted even if run_job fails.
        # This avoids the greenlet_spawn error that occurs when the except block tries to commit
        # after run_job raised an exception that left the session in an inconsistent state.
        auto_run = MassAnalysisAutomationRun(
            automation_id=automation_id,
            status="running",
            started_at=now,
            window_from=window_from,
            window_to=window_to,
            calls_found=0,
            calls_selected=0,
            calls_skipped=0
        )
        db.add(auto_run)
        await db.flush()

        # Snapshot scalar fields from automation BEFORE committing — after commit, the ORM object
        # is expired and lazy-loading outside an async greenlet causes greenlet_spawn errors.
        job_id_snapshot = automation.job_id

        await db.commit()       # Persist auto_run BEFORE calling run_job, so we have a valid ID
        await db.refresh(auto_run)
        auto_run_id_snapshot = auto_run.automation_run_id

        from app.db import get_engine as _get_engine
        engine = _get_engine()

        try:
            # Trigger underlying mass evaluation run (which launches background execution task)
            sub_run = await MassEvaluationService.run_job(
                db,
                job_id=job_id_snapshot,
                trigger_type=trigger_type,
                override_date_from=window_from,
                override_date_to=window_to
            )

            # Link run and job ids (use snapshot to avoid expired ORM access)
            auto_run.job_id = job_id_snapshot
            auto_run.run_id = sub_run.run_id
            # Re-fetch automation to update last_run_at safely
            aut_refresh_stmt = select(MassAnalysisAutomation).where(
                MassAnalysisAutomation.automation_id == automation_id
            )
            aut_refresh_res = await db.execute(aut_refresh_stmt)
            aut_fresh = aut_refresh_res.scalars().first()
            if aut_fresh:
                aut_fresh.last_run_at = now
            await db.commit()

        except Exception as e:
            err_str = str(e)
            logger.error("Failed to launch mass evaluation run for automation %d: %s", automation_id, err_str)
            # Determine final status: 'skipped' for lock conflicts, 'failed' for real errors
            is_already_running = "already running" in err_str.lower() or "already_running" in err_str.lower()
            final_err_status = "skipped" if is_already_running else "failed"

            # Use a FRESH session to update auto_run — the current session may be in a bad state
            # after run_job raised (greenlet/rollback issues). A fresh connection is always safe.
            try:
                async with AsyncSession(engine) as fail_db:
                    try:
                        fail_stmt = select(MassAnalysisAutomationRun).where(
                            MassAnalysisAutomationRun.automation_run_id == auto_run_id_snapshot
                        )
                        fail_res = await fail_db.execute(fail_stmt)
                        fail_auto_run = fail_res.scalars().first()
                        if fail_auto_run and fail_auto_run.status == "running":
                            fail_auto_run.status = final_err_status
                            fail_auto_run.finished_at = datetime.now(timezone.utc)
                            fail_auto_run.error_message = err_str

                        # Update parent automation metadata
                        aut_stmt2 = select(MassAnalysisAutomation).where(
                            MassAnalysisAutomation.automation_id == automation_id
                        )
                        aut_res2 = await fail_db.execute(aut_stmt2)
                        aut_obj2 = aut_res2.scalars().first()
                        if aut_obj2:
                            aut_obj2.last_run_at = now
                            if not is_already_running:
                                aut_obj2.last_error_at = datetime.now(timezone.utc)
                                aut_obj2.last_error_message = err_str

                        await fail_db.commit()

                        # Refresh auto_run from fresh session for the return value
                        fail_stmt2 = select(MassAnalysisAutomationRun).where(
                            MassAnalysisAutomationRun.automation_run_id == auto_run_id_snapshot
                        )
                        fail_res2 = await fail_db.execute(fail_stmt2)
                        updated_auto_run = fail_res2.scalars().first()
                        if updated_auto_run:
                            return updated_auto_run
                    except Exception as e_fail_inner:
                        await fail_db.rollback()
                        raise e_fail_inner
                    finally:
                        await fail_db.close()
            except Exception as e_inner:
                logger.error("Failed to update auto_run status in fallback session: %s", e_inner)

            # Return the in-memory auto_run (may not reflect DB state if fail_db also failed)
            auto_run.status = final_err_status
            auto_run.finished_at = datetime.now(timezone.utc)
            auto_run.error_message = err_str
            return auto_run

        # Re-fetch to return consistent state
        try:
            await db.refresh(auto_run)
        except Exception:
            pass
        return auto_run

    @staticmethod
    async def sync_automation_runs_with_mass_runs(
        db: AsyncSession, automation_id: int
    ) -> int:
        """
        For each MassAnalysisAutomationRun in status='running' linked to a finished MassEvaluationRun,
        syncs the automation run state to match. Also closes orphan runs (run_id=None) older than 10 min.
        Returns the number of runs updated.
        """
        stmt = select(MassAnalysisAutomationRun).where(
            MassAnalysisAutomationRun.automation_id == automation_id,
            MassAnalysisAutomationRun.status == "running"
        )
        res = await db.execute(stmt)
        running_auto_runs = res.scalars().all()

        now = datetime.now(timezone.utc)
        changed = 0
        for auto_run in running_auto_runs:
            if auto_run.run_id is not None:
                # Sync with the underlying MassEvaluationRun
                run_stmt = select(MassEvaluationRun).where(
                    MassEvaluationRun.run_id == auto_run.run_id
                )
                run_res = await db.execute(run_stmt)
                mass_run = run_res.scalars().first()
                if mass_run and mass_run.status not in ("running", "pending"):
                    # Mass run has finished — reflect its outcome in the automation run
                    if mass_run.status in ("completed", "completed_with_errors"):
                        auto_run.status = "completed"
                        auto_run.error_message = None
                    else:
                        auto_run.status = "failed"
                        auto_run.error_message = mass_run.error_message
                    auto_run.finished_at = mass_run.finished_at or now
                    auto_run.calls_found = mass_run.calls_found or 0
                    auto_run.calls_selected = mass_run.calls_selected or 0
                    auto_run.calls_skipped = mass_run.calls_skipped or 0
                    changed += 1
                    logger.info(
                        "[automation_sync] synced auto_run_id=%d automation_id=%d mass_run_id=%d "
                        "new_status=%s calls_found=%d",
                        auto_run.automation_run_id, auto_run.automation_id,
                        auto_run.run_id, auto_run.status, auto_run.calls_found
                    )
            else:
                # Orphan run: no run_id linked — close it if older than 10 minutes
                st_at = auto_run.started_at
                if st_at and st_at.tzinfo is None:
                    st_at = st_at.replace(tzinfo=timezone.utc)
                age_minutes = int((now - st_at).total_seconds() / 60) if st_at else 999
                if age_minutes >= 10:
                    auto_run.status = "skipped"
                    auto_run.finished_at = now
                    auto_run.error_message = (
                        auto_run.error_message or
                        "Closed automatically: no mass evaluation run was linked within timeout"
                    )
                    changed += 1
                    logger.warning(
                        "[automation_sync] closed orphan auto_run_id=%d automation_id=%d age_minutes=%d",
                        auto_run.automation_run_id, auto_run.automation_id, age_minutes
                    )

        if changed > 0:
            await db.commit()
        return changed

    @staticmethod
    async def run_due_automations(
        db: AsyncSession,
        company_ids: list[int] | None = None,
        service_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Finds all active automations that are due to execute and triggers them with scoping support.
        Uses PostgreSQL pg_try_advisory_xact_lock in a dedicated transaction-scoped connection so that
        the lock is automatically released upon transaction completion (commit or rollback), preventing
        connection-pool lock leakage and indefinite blockage across ticks.
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncEngine
        import threading

        # Detect dialect: PostgreSQL vs SQLite / tests
        is_postgresql = False
        try:
            bind = db.get_bind()
            if bind:
                dialect_name = getattr(bind.dialect, "name", "")
                is_postgresql = (dialect_name == "postgresql")
        except Exception:
            pass

        # Obtain the real AsyncEngine (do NOT use db.get_bind() as connection engine)
        async_engine: AsyncEngine | None = None
        try:
            from app.db import get_async_engine
            eng = get_async_engine()
            if isinstance(eng, AsyncEngine):
                async_engine = eng
                if not is_postgresql:
                    is_postgresql = (getattr(eng.dialect, "name", "") == "postgresql")
        except Exception as e_eng:
            logger.warning("[automation_scheduler] could not obtain AsyncEngine: %s", e_eng)

        if is_postgresql:
            if not async_engine:
                logger.error("[automation_scheduler] unable to acquire global lock: valid AsyncEngine not found. Skipping tick.")
                return {
                    "due_automations_count": 0,
                    "launched_automations_count": 0,
                    "skipped_automations_count": 0,
                    "stale_runs_closed": 0,
                    "skip_reason": "no_async_engine",
                    "executions": [],
                }

            async with async_engine.connect() as lock_conn:
                async with lock_conn.begin():
                    try:
                        res = await lock_conn.execute(
                            text("SELECT pg_try_advisory_xact_lock(:key)"),
                            {"key": MassEvaluationService._SCHEDULER_LOCK_KEY}
                        )
                        acquired = bool(res.scalar())
                    except Exception as e_lock:
                        logger.error("[automation_scheduler] error acquiring advisory xact lock: %s", e_lock)
                        acquired = False

                    if not acquired:
                        logger.info("[automation_scheduler] skipped global lock held by another worker")
                        return {
                            "due_automations_count": 0,
                            "launched_automations_count": 0,
                            "skipped_automations_count": 0,
                            "stale_runs_closed": 0,
                            "skip_reason": "global_lock_held",
                            "executions": [],
                        }

                    worker_pid = "unknown"
                    try:
                        pid_res = await lock_conn.execute(text("SELECT pg_backend_pid()"))
                        worker_pid = str(pid_res.scalar() or "unknown")
                    except Exception:
                        pass

                    logger.info("[automation_scheduler] acquired global xact lock worker_pid=%s", worker_pid)

                    result = await MassEvaluationService._run_due_automations_inner(
                        db, company_ids=company_ids, service_ids=service_ids
                    )

                    launched_cnt = result.get("launched_automations_count", 0)
                    if launched_cnt > 0:
                        log_process_memory(f"automations_launched_{launched_cnt}")

                    logger.info(
                        "[automation_scheduler] tick finished due=%d launched=%d skipped=%d",
                        result.get("due_automations_count", 0),
                        launched_cnt,
                        result.get("skipped_automations_count", 0),
                    )
                    return result
                # When exiting async with lock_conn.begin():
                # Transaction ends, and PostgreSQL automatically and unconditionally releases the advisory xact lock!

        # Non-PG / SQLite fallback: use in-process threading.Lock
        lock = MassEvaluationService._threading_scheduler_lock
        acquired = True
        if lock is not None:
            acquired = lock.acquire(blocking=False)

        if not acquired:
            logger.info("[automation_scheduler] skipped global lock held by another worker")
            return {
                "due_automations_count": 0,
                "launched_automations_count": 0,
                "skipped_automations_count": 0,
                "stale_runs_closed": 0,
                "skip_reason": "global_lock_held",
                "executions": [],
            }

        try:
            logger.info("[automation_scheduler] acquired global xact lock worker_pid=sqlite_thread_%s", threading.get_ident())
            result = await MassEvaluationService._run_due_automations_inner(
                db, company_ids=company_ids, service_ids=service_ids
            )
            logger.info(
                "[automation_scheduler] tick finished due=%d launched=%d skipped=%d",
                result.get("due_automations_count", 0),
                result.get("launched_automations_count", 0),
                result.get("skipped_automations_count", 0),
            )
            return result
        finally:
            if lock is not None:
                try:
                    lock.release()
                except RuntimeError:
                    pass

    @staticmethod
    async def _run_due_automations_inner(
        db: AsyncSession,
        company_ids: list[int] | None = None,
        service_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Inner body of run_due_automations, executed only when global lock is held."""
        from app.config import get_settings
        settings = get_settings()

        # 1. Automatic stale running cleanup before evaluating locks
        stale_closed_count = await MassEvaluationService.cleanup_stale_automation_runs(
            db,
            company_ids=company_ids,
            service_ids=service_ids
        )

        now = datetime.now(timezone.utc)
        from app.models.services import Service

        stmt = select(MassAnalysisAutomation).where(MassAnalysisAutomation.is_active == True)
        if service_ids is not None:
            stmt = stmt.where(MassAnalysisAutomation.service_id.in_(service_ids))
        elif company_ids:
            stmt = stmt.join(Service, Service.service_id == MassAnalysisAutomation.service_id).where(Service.company_id.in_(company_ids))

        res = await db.execute(stmt)
        automations_raw = res.scalars().all()

        automations_list = []
        for aut in automations_raw:
            last_at = aut.last_run_at
            if last_at and last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
            automations_list.append((
                aut.automation_id,
                aut.name,
                aut.interval_minutes or 30,
                aut.lookback_minutes or 30,
                aut.delay_minutes or 5,
                last_at
            ))

        due_count = 0
        launched_count = 0
        skipped_count = 0
        executions_detail = []

        threshold_minutes = settings.automation_running_stale_after_minutes or 60

        for automation_id, automation_name, interval_minutes, lookback_min, delay_min, last_run_at in automations_list:
            # Check next continuous window readiness
            window_from, window_to, is_ready, source_desc = await MassEvaluationService.get_automation_next_window(
                db, automation_id, now=now
            )

            if not is_ready:
                continue

            # Determine whether automation is due (standard cadence vs catch-up backlog)
            is_due = False
            if last_run_at is None:
                is_due = True
            else:
                elapsed = now - last_run_at
                if elapsed >= timedelta(minutes=interval_minutes):
                    is_due = True
                elif source_desc == "continuous" and (window_from + timedelta(minutes=lookback_min) <= now - timedelta(minutes=delay_min)):
                    # Catch-up mode: previous run was completed and backlog is ready
                    is_due = True

            if not is_due:
                continue

            due_count += 1

            # Check lock: verify if this automation already has an active run
            stmt_lock = select(MassAnalysisAutomationRun).where(
                MassAnalysisAutomationRun.automation_id == automation_id,
                MassAnalysisAutomationRun.status == "running"
            )
            res_lock = await db.execute(stmt_lock)
            active_run = res_lock.scalars().first()
            if active_run:
                st_at = active_run.started_at
                if st_at and st_at.tzinfo is None:
                    st_at = st_at.replace(tzinfo=timezone.utc)
                age_minutes = int((now - st_at).total_seconds() / 60) if st_at else 0
                is_stale = age_minutes >= threshold_minutes

                skipped_count += 1
                logger.info(
                    "[automation_scheduler] skipped automation_id=%d ('%s') reason='already_running' run_id=%d age_minutes=%d is_stale=%s",
                    automation_id, automation_name, active_run.automation_run_id, age_minutes, is_stale
                )
                executions_detail.append({
                    "automation_id": automation_id,
                    "automation_name": automation_name,
                    "status": "skipped",
                    "reason_skipped": "already_running",
                    "blocking_run_id": active_run.automation_run_id,
                    "blocking_run_started_at": active_run.started_at.isoformat() if active_run.started_at else None,
                    "blocking_run_age_minutes": age_minutes,
                    "is_stale": is_stale,
                })
                continue

            try:
                auto_run = await MassEvaluationService.run_automation_run(db, automation_id, trigger_type="scheduled")
                if auto_run.status in ("failed", "skipped"):
                    skipped_count += 1
                    if auto_run.status == "skipped" and auto_run.error_message == "not_due_window_not_ready":
                        logger.info(
                            "[automation_scheduler] skipped automation_id=%d ('%s') reason='not_due_window_not_ready'",
                            automation_id, automation_name
                        )
                        executions_detail.append({
                            "automation_id": automation_id,
                            "automation_name": automation_name,
                            "status": "skipped",
                            "reason_skipped": "not_due_window_not_ready",
                        })
                    else:
                        logger.warning(
                            "[automation_scheduler] failed automation_id=%d ('%s') auto_run_id=%d error='%s'",
                            automation_id, automation_name, auto_run.automation_run_id, auto_run.error_message
                        )
                        executions_detail.append({
                            "automation_id": automation_id,
                            "automation_name": automation_name,
                            "status": auto_run.status,
                            "error_message": auto_run.error_message,
                        })
                else:
                    launched_count += 1
                    logger.info(
                        "[automation_scheduler] launched automation_id=%d ('%s') auto_run_id=%d job_id=%s run_id=%s",
                        automation_id, automation_name, auto_run.automation_run_id, auto_run.job_id, auto_run.run_id
                    )
                    executions_detail.append({
                        "automation_id": automation_id,
                        "automation_name": automation_name,
                        "status": "launched",
                        "automation_run_id": auto_run.automation_run_id,
                        "job_id": auto_run.job_id,
                        "run_id": auto_run.run_id,
                    })
            except Exception as e:
                skipped_count += 1
                logger.error("[automation_scheduler] failed automation_id=%d ('%s'): %s", automation_id, automation_name, e)
                executions_detail.append({
                    "automation_id": automation_id,
                    "automation_name": automation_name,
                    "status": "failed",
                    "error_message": str(e),
                })

        return {
            "due_automations_count": due_count,
            "launched_automations_count": launched_count,
            "skipped_automations_count": skipped_count,
            "stale_runs_closed": stale_closed_count,
            "executions": executions_detail,
        }

    @staticmethod
    async def list_automation_runs(db: AsyncSession, automation_id: int, limit: int = 100) -> list[MassAnalysisAutomationRun]:
        """List all execution logs / runs for a given automation configuration.
        Performs an on-read sync so any 'running' rows whose mass run has already
        completed are updated to their final status before returning.
        """
        # On-read sync: fix running rows that should be completed/failed/skipped
        try:
            await MassEvaluationService.sync_automation_runs_with_mass_runs(db, automation_id)
        except Exception as e_sync:
            logger.warning("Failed to sync automation runs on read for automation_id=%d: %s", automation_id, e_sync)

        stmt = select(MassAnalysisAutomationRun).where(
            MassAnalysisAutomationRun.automation_id == automation_id
        ).order_by(desc(MassAnalysisAutomationRun.automation_run_id)).limit(limit)
        res = await db.execute(stmt)
        return list(res.scalars().all())
