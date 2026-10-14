"""
FinOps AI Usage Reporting Service (Phase 4).
Provides high-performance SQL-aggregated analytics for token consumption,
cost breakdowns, modality attribution, and timeseries evolution over bm_ai_usage_events.
"""
import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, List, Optional, Tuple

from fastapi import HTTPException, status as http_status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.roles import InternalRole
from app.core.tenant_context import TenantContext
from app.models.ai_usage import AIUsageEvent
from app.models.companies import Company
from app.models.services import Service
from app.schemas.usage import (
    CompanyUsageBreakdown,
    CompanyUsageResponse,
    ModelUsageBreakdown,
    ModelUsageResponse,
    ModuleUsageBreakdown,
    ModuleUsageResponse,
    ServiceUsageBreakdown,
    ServiceUsageResponse,
    UsageSummaryResponse,
    UsageTimeseriesPoint,
    UsageTimeseriesResponse,
)
from app.utils.dates import MADRID_TZ, resolve_granularity, safe_parse_datetime


def resolve_functional_area(module: Optional[str]) -> str:
    """Maps technical backend module to high-level FinOps functional area."""
    if not module:
        return "OTROS"
    m = module.strip().lower()
    if m == "mass_evaluations":
        return "MASIVAS"
    if m == "test_analysis":
        return "LABORATORIO"
    if m == "cycles":
        return "CICLOS"
    if m in (
        "trainer_prompt",
        "trainer_post_eval",
        "trainer_voice_session",
        "training_voice_session",
        "hub_media_stream",
    ):
        return "TRAINER"
    if m == "tutor_chat":
        return "TUTOR_IA"
    if m in ("prompt_builder", "criteria_generator"):
        return "CONFIGURACION_IA"
    return "OTROS"


def get_functional_area_expr():
    """SQLAlchemy CASE expression mapping module to functional area at the SQL level."""
    return case(
        (AIUsageEvent.module == "mass_evaluations", "MASIVAS"),
        (AIUsageEvent.module == "test_analysis", "LABORATORIO"),
        (AIUsageEvent.module == "cycles", "CICLOS"),
        (
            AIUsageEvent.module.in_([
                "trainer_prompt",
                "trainer_post_eval",
                "trainer_voice_session",
                "training_voice_session",
                "hub_media_stream",
            ]),
            "TRAINER",
        ),
        (AIUsageEvent.module == "tutor_chat", "TUTOR_IA"),
        (
            AIUsageEvent.module.in_(["prompt_builder", "criteria_generator"]),
            "CONFIGURACION_IA",
        ),
        else_="OTROS",
    )


def resolve_period_bounds(
    period: Optional[str] = None,
    date_from: Any = None,
    date_to: Any = None,
    ref_dt: Optional[datetime] = None,
) -> Tuple[str, datetime, datetime]:
    """
    Computes Europe/Madrid local time bounds for the requested period,
    converting to timezone-aware UTC for database queries.

    Supported periods:
      - 'day': Natural day in Madrid (00:00:00 to 23:59:59.999999).
      - 'week': Natural week in Madrid (Monday 00:00:00 to Sunday 23:59:59.999999).
      - 'month': Natural month in Madrid (day 1 to last day of month 23:59:59.999999).
      - 'quarter': Natural quarter in Madrid (Q1 Jan-Mar, Q2 Apr-Jun, Q3 Jul-Sep, Q4 Oct-Dec).
      - 'custom': Custom range specified by date_from and date_to in Europe/Madrid.
    """
    now_m = (ref_dt or datetime.now(timezone.utc)).astimezone(MADRID_TZ)

    # 1. Custom bounds if date_from or date_to are explicitly provided
    if date_from is not None or date_to is not None:
        effective_period = "custom"
        dt_from_parsed = safe_parse_datetime(date_from) if date_from else None
        dt_to_parsed = safe_parse_datetime(date_to) if date_to else None

        if dt_from_parsed:
            raw_from = str(date_from).strip()
            if "+" in raw_from or "Z" in raw_from or (len(raw_from) > 10 and "T" in raw_from and "00:00:00" not in raw_from):
                dt_from_utc = dt_from_parsed.astimezone(timezone.utc)
            else:
                dt_from_m = datetime(dt_from_parsed.year, dt_from_parsed.month, dt_from_parsed.day, 0, 0, 0, tzinfo=MADRID_TZ)
                dt_from_utc = dt_from_m.astimezone(timezone.utc)
        else:
            # Default to 30 days before date_to or start of current month
            if dt_to_parsed:
                dt_from_utc = (dt_to_parsed - timedelta(days=30)).astimezone(timezone.utc)
            else:
                dt_from_m = datetime(now_m.year, now_m.month, 1, 0, 0, 0, tzinfo=MADRID_TZ)
                dt_from_utc = dt_from_m.astimezone(timezone.utc)

        if dt_to_parsed:
            raw_to = str(date_to).strip()
            if "+" in raw_to or "Z" in raw_to or (len(raw_to) > 10 and "T" in raw_to and "00:00:00" not in raw_to):
                dt_to_utc = dt_to_parsed.astimezone(timezone.utc)
            else:
                dt_to_m = datetime(dt_to_parsed.year, dt_to_parsed.month, dt_to_parsed.day, 23, 59, 59, 999999, tzinfo=MADRID_TZ)
                dt_to_utc = dt_to_m.astimezone(timezone.utc)
        else:
            dt_to_m = datetime(now_m.year, now_m.month, now_m.day, 23, 59, 59, 999999, tzinfo=MADRID_TZ)
            dt_to_utc = dt_to_m.astimezone(timezone.utc)

        return effective_period, dt_from_utc, dt_to_utc

    # 2. Preset natural periods
    norm_period = (period or "month").strip().lower()

    if norm_period == "day":
        start_m = datetime(now_m.year, now_m.month, now_m.day, 0, 0, 0, 0, tzinfo=MADRID_TZ)
        end_m = datetime(now_m.year, now_m.month, now_m.day, 23, 59, 59, 999999, tzinfo=MADRID_TZ)
        return "day", start_m.astimezone(timezone.utc), end_m.astimezone(timezone.utc)

    elif norm_period == "week":
        weekday = now_m.weekday()  # Monday = 0, Sunday = 6
        monday_m = datetime(now_m.year, now_m.month, now_m.day, 0, 0, 0, 0, tzinfo=MADRID_TZ) - timedelta(days=weekday)
        start_m = monday_m
        end_m = monday_m + timedelta(days=6, hours=23, minutes=59, seconds=59, microseconds=999999)
        return "week", start_m.astimezone(timezone.utc), end_m.astimezone(timezone.utc)

    elif norm_period == "quarter":
        q_start_month = ((now_m.month - 1) // 3) * 3 + 1
        q_end_month = q_start_month + 2
        _, last_day = calendar.monthrange(now_m.year, q_end_month)
        start_m = datetime(now_m.year, q_start_month, 1, 0, 0, 0, 0, tzinfo=MADRID_TZ)
        end_m = datetime(now_m.year, q_end_month, last_day, 23, 59, 59, 999999, tzinfo=MADRID_TZ)
        return "quarter", start_m.astimezone(timezone.utc), end_m.astimezone(timezone.utc)

    else:  # default to month
        _, last_day = calendar.monthrange(now_m.year, now_m.month)
        start_m = datetime(now_m.year, now_m.month, 1, 0, 0, 0, 0, tzinfo=MADRID_TZ)
        end_m = datetime(now_m.year, now_m.month, last_day, 23, 59, 59, 999999, tzinfo=MADRID_TZ)
        return "month", start_m.astimezone(timezone.utc), end_m.astimezone(timezone.utc)


def apply_usage_filters(
    stmt,
    context: TenantContext,
    dt_from_utc: datetime,
    dt_to_utc: datetime,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    model: Optional[str] = None,
    pricing_status: Optional[str] = None,
):
    """
    Applies multi-tenant role boundary enforcement and user query filters to a SQLAlchemy select.
    Raises HTTP 403 if unauthorized access to companies or services is attempted.
    """
    # 1. Multi-tenant access validation: strictly Super Admin
    if not context.is_super_admin:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Super Administrador para consultar datos FinOps.",
        )

    # 2. Date bounds filter
    stmt = stmt.where(AIUsageEvent.created_at >= dt_from_utc, AIUsageEvent.created_at <= dt_to_utc)

    # 3. Dimensional filters
    if company_id is not None:
        stmt = stmt.where(AIUsageEvent.company_id == company_id)
    if service_id is not None:
        stmt = stmt.where(AIUsageEvent.service_id == service_id)
    if module:
        stmt = stmt.where(AIUsageEvent.module == module.strip())
    if model:
        stmt = stmt.where(AIUsageEvent.model == model.strip())
    if pricing_status:
        stmt = stmt.where(AIUsageEvent.pricing_status == pricing_status.strip().lower())

    return stmt


def get_metrics_columns():
    """SQLAlchemy aggregation columns for AI usage and FinOps metrics."""
    return [
        func.count(AIUsageEvent.id).label("total_events"),
        func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0).label("total_cost_usd"),
        func.coalesce(func.sum(AIUsageEvent.total_token_count), 0).label("total_tokens"),
        func.coalesce(func.sum(AIUsageEvent.prompt_token_count), 0).label("prompt_tokens"),
        func.coalesce(func.sum(AIUsageEvent.candidates_token_count), 0).label("candidates_tokens"),
        func.coalesce(func.sum(AIUsageEvent.cached_content_token_count), 0).label("cached_tokens"),
        func.coalesce(func.sum(AIUsageEvent.thoughts_token_count), 0).label("thoughts_tokens"),
        func.coalesce(func.sum(AIUsageEvent.input_text_tokens), 0).label("input_text_tokens"),
        func.coalesce(func.sum(AIUsageEvent.input_audio_tokens), 0).label("input_audio_tokens"),
        func.coalesce(func.sum(AIUsageEvent.output_text_tokens), 0).label("output_text_tokens"),
        func.coalesce(func.sum(AIUsageEvent.output_audio_tokens), 0).label("output_audio_tokens"),
        func.coalesce(func.sum(AIUsageEvent.audio_duration_seconds), 0).label("audio_seconds"),
        func.coalesce(
            func.sum(
                case(
                    (AIUsageEvent.audio_duration_seconds > 0, func.coalesce(AIUsageEvent.estimated_cost_usd, 0)),
                    else_=0,
                )
            ),
            0,
        ).label("audio_related_cost_usd"),
        func.avg(AIUsageEvent.execution_latency_ms).label("avg_latency_ms"),
        func.coalesce(func.sum(case((AIUsageEvent.pricing_status == "priced", 1), else_=0)), 0).label("priced_events"),
        func.coalesce(func.sum(case((AIUsageEvent.pricing_status == "partial", 1), else_=0)), 0).label("partial_events"),
        func.coalesce(func.sum(case((AIUsageEvent.pricing_status == "unpriced", 1), else_=0)), 0).label("unpriced_events"),
    ]


def compute_derived_metrics(
    total_events: int,
    total_cost: float,
    audio_seconds: float,
    audio_related_cost: float,
    priced_events: int,
    partial_events: int,
) -> Tuple[float, Optional[float], Optional[float], float]:
    """Calculates audio minutes, cost per audio minute, cost per event, and coverage percentage."""
    audio_mins = round(audio_seconds / 60.0, 2)
    cost_per_audio_min = None
    if audio_mins > 0:
        cost_per_audio_min = round(audio_related_cost / audio_mins, 6)

    cost_per_event = None
    if total_events > 0:
        cost_per_event = round(total_cost / total_events, 6)

    coverage = 100.0
    if total_events > 0:
        coverage = round(((priced_events + partial_events) / total_events) * 100.0, 2)

    return audio_mins, cost_per_audio_min, cost_per_event, coverage


def get_time_bucket_expr(dialect_name: str, granularity: str):
    """Generates dialect-appropriate time bucket label expression (SQLite or PostgreSQL)."""
    if dialect_name == "sqlite":
        if granularity == "hour":
            return func.strftime("%Y-%m-%d %H:00", AIUsageEvent.created_at)
        elif granularity == "week":
            return func.date(AIUsageEvent.created_at, "weekday 0", "-6 days")
        elif granularity == "month":
            return func.strftime("%Y-%m-01", AIUsageEvent.created_at)
        elif granularity == "quarter":
            return case(
                (func.strftime("%m", AIUsageEvent.created_at).in_(["01", "02", "03"]), func.strftime("%Y-Q1", AIUsageEvent.created_at)),
                (func.strftime("%m", AIUsageEvent.created_at).in_(["04", "05", "06"]), func.strftime("%Y-Q2", AIUsageEvent.created_at)),
                (func.strftime("%m", AIUsageEvent.created_at).in_(["07", "08", "09"]), func.strftime("%Y-Q3", AIUsageEvent.created_at)),
                else_=func.strftime("%Y-Q4", AIUsageEvent.created_at),
            )
        else:  # day
            return func.strftime("%Y-%m-%d", AIUsageEvent.created_at)
    else:
        # PostgreSQL
        if granularity == "hour":
            return func.to_char(func.date_trunc("hour", func.timezone("Europe/Madrid", AIUsageEvent.created_at)), "YYYY-MM-DD HH24:00")
        elif granularity == "week":
            return func.to_char(func.date_trunc("week", func.timezone("Europe/Madrid", AIUsageEvent.created_at)), "YYYY-MM-DD")
        elif granularity == "month":
            return func.to_char(func.date_trunc("month", func.timezone("Europe/Madrid", AIUsageEvent.created_at)), "YYYY-MM-01")
        elif granularity == "quarter":
            return func.to_char(func.date_trunc("quarter", func.timezone("Europe/Madrid", AIUsageEvent.created_at)), 'YYYY-"Q"Q')
        else:  # day
            return func.to_char(func.date_trunc("day", func.timezone("Europe/Madrid", AIUsageEvent.created_at)), "YYYY-MM-DD")


# ── SERVICE METHODS ───────────────────────────────────────────────────────────

async def get_usage_summary(
    session: AsyncSession,
    context: TenantContext,
    period: Optional[str] = None,
    date_from: Any = None,
    date_to: Any = None,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    model: Optional[str] = None,
    pricing_status: Optional[str] = None,
    ref_dt: Optional[datetime] = None,
) -> UsageSummaryResponse:
    """Calculates overall AI usage metrics and cost attribution for the specified period."""
    eff_period, dt_from_utc, dt_to_utc = resolve_period_bounds(period, date_from, date_to, ref_dt=ref_dt)

    stmt = select(*get_metrics_columns())
    stmt = apply_usage_filters(
        stmt,
        context=context,
        dt_from_utc=dt_from_utc,
        dt_to_utc=dt_to_utc,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )

    result = await session.execute(stmt)
    row = result.first()

    if not row or (row.total_events or 0) == 0:
        return UsageSummaryResponse(
            period=eff_period,
            date_from=dt_from_utc,
            date_to=dt_to_utc,
            currency="USD",
            total_events=0,
            total_cost_usd=0.0,
            total_tokens=0,
            audio_seconds=0.0,
            audio_minutes=0.0,
            audio_related_cost_usd=0.0,
            cost_per_audio_minute_usd=None,
            cost_per_event_usd=None,
            avg_latency_ms=None,
            priced_events=0,
            partial_events=0,
            unpriced_events=0,
            cost_coverage_percentage=100.0,
        )

    total_events = int(row.total_events or 0)
    total_cost = float(row.total_cost_usd or 0.0)
    audio_seconds = float(row.audio_seconds or 0.0)
    audio_related_cost = float(row.audio_related_cost_usd or 0.0)
    priced_events = int(row.priced_events or 0)
    partial_events = int(row.partial_events or 0)
    unpriced_events = int(row.unpriced_events or 0)

    audio_mins, cost_per_audio_min, cost_per_event, coverage = compute_derived_metrics(
        total_events, total_cost, audio_seconds, audio_related_cost, priced_events, partial_events
    )

    avg_latency = round(float(row.avg_latency_ms), 1) if row.avg_latency_ms is not None else None

    return UsageSummaryResponse(
        period=eff_period,
        date_from=dt_from_utc,
        date_to=dt_to_utc,
        currency="USD",
        total_events=total_events,
        total_cost_usd=round(total_cost, 6),
        total_tokens=int(row.total_tokens or 0),
        prompt_tokens=int(row.prompt_tokens or 0),
        candidates_tokens=int(row.candidates_tokens or 0),
        cached_tokens=int(row.cached_tokens or 0),
        thoughts_tokens=int(row.thoughts_tokens or 0),
        input_text_tokens=int(row.input_text_tokens or 0),
        input_audio_tokens=int(row.input_audio_tokens or 0),
        output_text_tokens=int(row.output_text_tokens or 0),
        output_audio_tokens=int(row.output_audio_tokens or 0),
        audio_seconds=round(audio_seconds, 2),
        audio_minutes=audio_mins,
        audio_related_cost_usd=round(audio_related_cost, 6),
        cost_per_audio_minute_usd=cost_per_audio_min,
        cost_per_event_usd=cost_per_event,
        avg_latency_ms=avg_latency,
        priced_events=priced_events,
        partial_events=partial_events,
        unpriced_events=unpriced_events,
        cost_coverage_percentage=coverage,
    )


async def get_usage_by_company(
    session: AsyncSession,
    context: TenantContext,
    period: Optional[str] = None,
    date_from: Any = None,
    date_to: Any = None,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    model: Optional[str] = None,
    pricing_status: Optional[str] = None,
    ref_dt: Optional[datetime] = None,
) -> CompanyUsageResponse:
    """Returns AI usage and costs broken down by company."""
    eff_period, dt_from_utc, dt_to_utc = resolve_period_bounds(period, date_from, date_to, ref_dt=ref_dt)

    stmt = select(
        AIUsageEvent.company_id,
        func.coalesce(Company.company_name, "Sin Empresa").label("company_name"),
        *get_metrics_columns(),
    ).outerjoin(Company, AIUsageEvent.company_id == Company.company_id)

    stmt = apply_usage_filters(
        stmt,
        context=context,
        dt_from_utc=dt_from_utc,
        dt_to_utc=dt_to_utc,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )

    stmt = stmt.group_by(AIUsageEvent.company_id, Company.company_name).order_by(
        func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0).desc()
    )

    result = await session.execute(stmt)
    rows = result.all()

    total_cost_all = sum(float(r.total_cost_usd or 0.0) for r in rows)
    items: List[CompanyUsageBreakdown] = []

    for r in rows:
        events = int(r.total_events or 0)
        cost = float(r.total_cost_usd or 0.0)
        audio_sec = float(r.audio_seconds or 0.0)
        audio_rel_cost = float(r.audio_related_cost_usd or 0.0)
        priced = int(r.priced_events or 0)
        partial = int(r.partial_events or 0)

        audio_mins, cost_per_audio_min, cost_per_event, _ = compute_derived_metrics(
            events, cost, audio_sec, audio_rel_cost, priced, partial
        )

        pct = round((cost / total_cost_all * 100.0), 2) if total_cost_all > 0 else 0.0
        avg_latency = round(float(r.avg_latency_ms), 1) if r.avg_latency_ms is not None else None

        items.append(
            CompanyUsageBreakdown(
                company_id=r.company_id,
                company_name=r.company_name,
                total_events=events,
                total_cost_usd=round(cost, 6),
                percentage_of_total_cost=pct,
                total_tokens=int(r.total_tokens or 0),
                prompt_tokens=int(r.prompt_tokens or 0),
                candidates_tokens=int(r.candidates_tokens or 0),
                cached_tokens=int(r.cached_tokens or 0),
                thoughts_tokens=int(r.thoughts_tokens or 0),
                audio_seconds=round(audio_sec, 2),
                audio_minutes=audio_mins,
                audio_related_cost_usd=round(audio_rel_cost, 6),
                cost_per_audio_minute_usd=cost_per_audio_min,
                cost_per_event_usd=cost_per_event,
                avg_latency_ms=avg_latency,
                priced_events=priced,
                partial_events=partial,
                unpriced_events=int(r.unpriced_events or 0),
            )
        )

    return CompanyUsageResponse(
        period=eff_period,
        date_from=dt_from_utc,
        date_to=dt_to_utc,
        currency="USD",
        total_cost_usd=round(total_cost_all, 6),
        items=items,
    )


async def get_usage_by_service(
    session: AsyncSession,
    context: TenantContext,
    period: Optional[str] = None,
    date_from: Any = None,
    date_to: Any = None,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    model: Optional[str] = None,
    pricing_status: Optional[str] = None,
    ref_dt: Optional[datetime] = None,
) -> ServiceUsageResponse:
    """Returns AI usage and costs broken down by service."""
    eff_period, dt_from_utc, dt_to_utc = resolve_period_bounds(period, date_from, date_to, ref_dt=ref_dt)

    stmt = select(
        AIUsageEvent.service_id,
        func.coalesce(Service.service_name, "Sin Servicio").label("service_name"),
        AIUsageEvent.company_id,
        func.coalesce(Company.company_name, "Sin Empresa").label("company_name"),
        *get_metrics_columns(),
    ).outerjoin(Service, AIUsageEvent.service_id == Service.service_id) \
     .outerjoin(Company, AIUsageEvent.company_id == Company.company_id)

    stmt = apply_usage_filters(
        stmt,
        context=context,
        dt_from_utc=dt_from_utc,
        dt_to_utc=dt_to_utc,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )

    stmt = stmt.group_by(
        AIUsageEvent.service_id,
        Service.service_name,
        AIUsageEvent.company_id,
        Company.company_name,
    ).order_by(func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0).desc())

    result = await session.execute(stmt)
    rows = result.all()

    total_cost_all = sum(float(r.total_cost_usd or 0.0) for r in rows)
    items: List[ServiceUsageBreakdown] = []

    for r in rows:
        events = int(r.total_events or 0)
        cost = float(r.total_cost_usd or 0.0)
        audio_sec = float(r.audio_seconds or 0.0)
        audio_rel_cost = float(r.audio_related_cost_usd or 0.0)
        priced = int(r.priced_events or 0)
        partial = int(r.partial_events or 0)

        audio_mins, cost_per_audio_min, cost_per_event, _ = compute_derived_metrics(
            events, cost, audio_sec, audio_rel_cost, priced, partial
        )

        pct = round((cost / total_cost_all * 100.0), 2) if total_cost_all > 0 else 0.0
        avg_latency = round(float(r.avg_latency_ms), 1) if r.avg_latency_ms is not None else None

        items.append(
            ServiceUsageBreakdown(
                service_id=r.service_id,
                service_name=r.service_name,
                company_id=r.company_id,
                company_name=r.company_name,
                total_events=events,
                total_cost_usd=round(cost, 6),
                percentage_of_total_cost=pct,
                total_tokens=int(r.total_tokens or 0),
                prompt_tokens=int(r.prompt_tokens or 0),
                candidates_tokens=int(r.candidates_tokens or 0),
                cached_tokens=int(r.cached_tokens or 0),
                thoughts_tokens=int(r.thoughts_tokens or 0),
                audio_seconds=round(audio_sec, 2),
                audio_minutes=audio_mins,
                audio_related_cost_usd=round(audio_rel_cost, 6),
                cost_per_audio_minute_usd=cost_per_audio_min,
                cost_per_event_usd=cost_per_event,
                avg_latency_ms=avg_latency,
                priced_events=priced,
                partial_events=partial,
                unpriced_events=int(r.unpriced_events or 0),
            )
        )

    return ServiceUsageResponse(
        period=eff_period,
        date_from=dt_from_utc,
        date_to=dt_to_utc,
        currency="USD",
        total_cost_usd=round(total_cost_all, 6),
        items=items,
    )


async def get_usage_by_module(
    session: AsyncSession,
    context: TenantContext,
    period: Optional[str] = None,
    date_from: Any = None,
    date_to: Any = None,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    model: Optional[str] = None,
    pricing_status: Optional[str] = None,
    ref_dt: Optional[datetime] = None,
) -> ModuleUsageResponse:
    """Returns AI usage and costs broken down by technical module and functional area."""
    eff_period, dt_from_utc, dt_to_utc = resolve_period_bounds(period, date_from, date_to, ref_dt=ref_dt)

    func_area_expr = get_functional_area_expr()
    stmt = select(
        func.coalesce(AIUsageEvent.module, "desconocido").label("module"),
        func_area_expr.label("functional_area"),
        *get_metrics_columns(),
    )

    stmt = apply_usage_filters(
        stmt,
        context=context,
        dt_from_utc=dt_from_utc,
        dt_to_utc=dt_to_utc,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )

    stmt = stmt.group_by(AIUsageEvent.module, func_area_expr).order_by(
        func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0).desc()
    )

    result = await session.execute(stmt)
    rows = result.all()

    total_cost_all = sum(float(r.total_cost_usd or 0.0) for r in rows)
    items: List[ModuleUsageBreakdown] = []

    for r in rows:
        events = int(r.total_events or 0)
        cost = float(r.total_cost_usd or 0.0)
        audio_sec = float(r.audio_seconds or 0.0)
        audio_rel_cost = float(r.audio_related_cost_usd or 0.0)
        priced = int(r.priced_events or 0)
        partial = int(r.partial_events or 0)

        audio_mins, cost_per_audio_min, cost_per_event, _ = compute_derived_metrics(
            events, cost, audio_sec, audio_rel_cost, priced, partial
        )

        pct = round((cost / total_cost_all * 100.0), 2) if total_cost_all > 0 else 0.0
        avg_latency = round(float(r.avg_latency_ms), 1) if r.avg_latency_ms is not None else None

        items.append(
            ModuleUsageBreakdown(
                module=r.module,
                functional_area=r.functional_area,
                total_events=events,
                total_cost_usd=round(cost, 6),
                percentage_of_total_cost=pct,
                total_tokens=int(r.total_tokens or 0),
                prompt_tokens=int(r.prompt_tokens or 0),
                candidates_tokens=int(r.candidates_tokens or 0),
                cached_tokens=int(r.cached_tokens or 0),
                thoughts_tokens=int(r.thoughts_tokens or 0),
                audio_seconds=round(audio_sec, 2),
                audio_minutes=audio_mins,
                audio_related_cost_usd=round(audio_rel_cost, 6),
                cost_per_audio_minute_usd=cost_per_audio_min,
                cost_per_event_usd=cost_per_event,
                avg_latency_ms=avg_latency,
                priced_events=priced,
                partial_events=partial,
                unpriced_events=int(r.unpriced_events or 0),
            )
        )

    return ModuleUsageResponse(
        period=eff_period,
        date_from=dt_from_utc,
        date_to=dt_to_utc,
        currency="USD",
        total_cost_usd=round(total_cost_all, 6),
        items=items,
    )


async def get_usage_by_model(
    session: AsyncSession,
    context: TenantContext,
    period: Optional[str] = None,
    date_from: Any = None,
    date_to: Any = None,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    model: Optional[str] = None,
    pricing_status: Optional[str] = None,
    ref_dt: Optional[datetime] = None,
) -> ModelUsageResponse:
    """Returns AI usage and costs broken down by model."""
    eff_period, dt_from_utc, dt_to_utc = resolve_period_bounds(period, date_from, date_to, ref_dt=ref_dt)

    stmt = select(
        func.coalesce(AIUsageEvent.model, "desconocido").label("model"),
        func.coalesce(AIUsageEvent.provider, "gemini").label("provider"),
        *get_metrics_columns(),
    )

    stmt = apply_usage_filters(
        stmt,
        context=context,
        dt_from_utc=dt_from_utc,
        dt_to_utc=dt_to_utc,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )

    stmt = stmt.group_by(AIUsageEvent.model, AIUsageEvent.provider).order_by(
        func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0).desc()
    )

    result = await session.execute(stmt)
    rows = result.all()

    total_cost_all = sum(float(r.total_cost_usd or 0.0) for r in rows)
    items: List[ModelUsageBreakdown] = []

    for r in rows:
        events = int(r.total_events or 0)
        cost = float(r.total_cost_usd or 0.0)
        audio_sec = float(r.audio_seconds or 0.0)
        audio_rel_cost = float(r.audio_related_cost_usd or 0.0)
        priced = int(r.priced_events or 0)
        partial = int(r.partial_events or 0)

        audio_mins, cost_per_audio_min, cost_per_event, _ = compute_derived_metrics(
            events, cost, audio_sec, audio_rel_cost, priced, partial
        )

        pct = round((cost / total_cost_all * 100.0), 2) if total_cost_all > 0 else 0.0
        avg_latency = round(float(r.avg_latency_ms), 1) if r.avg_latency_ms is not None else None

        items.append(
            ModelUsageBreakdown(
                model=r.model,
                provider=r.provider,
                total_events=events,
                total_cost_usd=round(cost, 6),
                percentage_of_total_cost=pct,
                total_tokens=int(r.total_tokens or 0),
                prompt_tokens=int(r.prompt_tokens or 0),
                candidates_tokens=int(r.candidates_tokens or 0),
                cached_tokens=int(r.cached_tokens or 0),
                thoughts_tokens=int(r.thoughts_tokens or 0),
                audio_seconds=round(audio_sec, 2),
                audio_minutes=audio_mins,
                audio_related_cost_usd=round(audio_rel_cost, 6),
                cost_per_audio_minute_usd=cost_per_audio_min,
                cost_per_event_usd=cost_per_event,
                avg_latency_ms=avg_latency,
                priced_events=priced,
                partial_events=partial,
                unpriced_events=int(r.unpriced_events or 0),
            )
        )

    return ModelUsageResponse(
        period=eff_period,
        date_from=dt_from_utc,
        date_to=dt_to_utc,
        currency="USD",
        total_cost_usd=round(total_cost_all, 6),
        items=items,
    )


async def get_usage_timeseries(
    session: AsyncSession,
    context: TenantContext,
    period: Optional[str] = None,
    granularity: Optional[str] = "auto",
    group_by: Optional[str] = "none",
    date_from: Any = None,
    date_to: Any = None,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    model: Optional[str] = None,
    pricing_status: Optional[str] = None,
    ref_dt: Optional[datetime] = None,
) -> UsageTimeseriesResponse:
    """Returns time-bucketed AI usage metrics and cost trends, optionally grouped by dimension."""
    eff_period, dt_from_utc, dt_to_utc = resolve_period_bounds(period, date_from, date_to, ref_dt=ref_dt)

    # 1. Resolve effective granularity
    span = dt_to_utc - dt_from_utc
    req_gran = (granularity or "auto").strip().lower()
    if req_gran in ("hour", "day", "week", "month", "quarter"):
        eff_granularity = req_gran
    else:
        eff_granularity = resolve_granularity(span, "auto")

    # 2. Database dialect detection
    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind else "postgresql"

    # 3. Setup grouping expressions
    bucket_expr = get_time_bucket_expr(dialect_name, eff_granularity).label("bucket_label")
    min_ts_expr = func.min(AIUsageEvent.created_at).label("timestamp")

    norm_group_by = (group_by or "none").strip().lower()

    select_cols = [
        min_ts_expr,
        bucket_expr,
    ]
    group_by_cols = [bucket_expr]

    if norm_group_by == "company":
        select_cols.append(AIUsageEvent.company_id.label("dim_id"))
        select_cols.append(func.coalesce(Company.company_name, "Sin Empresa").label("dim_name"))
        group_by_cols.extend([AIUsageEvent.company_id, Company.company_name])
    elif norm_group_by == "service":
        select_cols.append(AIUsageEvent.service_id.label("dim_id"))
        select_cols.append(func.coalesce(Service.service_name, "Sin Servicio").label("dim_name"))
        group_by_cols.extend([AIUsageEvent.service_id, Service.service_name])
    elif norm_group_by == "module":
        select_cols.append(func.coalesce(AIUsageEvent.module, "desconocido").label("dim_name"))
        group_by_cols.append(AIUsageEvent.module)
    elif norm_group_by == "functional_area":
        fa_expr = get_functional_area_expr()
        select_cols.append(fa_expr.label("dim_name"))
        group_by_cols.append(fa_expr)
    elif norm_group_by == "model":
        select_cols.append(func.coalesce(AIUsageEvent.model, "desconocido").label("dim_name"))
        group_by_cols.append(AIUsageEvent.model)

    # Metrics
    select_cols.extend([
        func.count(AIUsageEvent.id).label("total_events"),
        func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0).label("total_cost_usd"),
        func.coalesce(func.sum(AIUsageEvent.total_token_count), 0).label("total_tokens"),
        func.coalesce(func.sum(AIUsageEvent.prompt_token_count), 0).label("prompt_tokens"),
        func.coalesce(func.sum(AIUsageEvent.candidates_token_count), 0).label("candidates_tokens"),
        func.coalesce(func.sum(AIUsageEvent.cached_content_token_count), 0).label("cached_tokens"),
        func.coalesce(func.sum(AIUsageEvent.thoughts_token_count), 0).label("thoughts_tokens"),
        func.coalesce(func.sum(AIUsageEvent.audio_duration_seconds), 0).label("audio_seconds"),
        func.coalesce(
            func.sum(
                case(
                    (AIUsageEvent.audio_duration_seconds > 0, func.coalesce(AIUsageEvent.estimated_cost_usd, 0)),
                    else_=0,
                )
            ),
            0,
        ).label("audio_related_cost_usd"),
    ])

    stmt = select(*select_cols)

    if norm_group_by == "company":
        stmt = stmt.outerjoin(Company, AIUsageEvent.company_id == Company.company_id)
    elif norm_group_by == "service":
        stmt = stmt.outerjoin(Service, AIUsageEvent.service_id == Service.service_id)

    stmt = apply_usage_filters(
        stmt,
        context=context,
        dt_from_utc=dt_from_utc,
        dt_to_utc=dt_to_utc,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )

    stmt = stmt.group_by(*group_by_cols).order_by(bucket_expr.asc())

    result = await session.execute(stmt)
    rows = result.all()

    total_cost_all = sum(float(r.total_cost_usd or 0.0) for r in rows)
    points: List[UsageTimeseriesPoint] = []

    for r in rows:
        events = int(r.total_events or 0)
        cost = float(r.total_cost_usd or 0.0)
        audio_sec = float(r.audio_seconds or 0.0)
        audio_rel_cost = float(r.audio_related_cost_usd or 0.0)

        audio_mins = round(audio_sec / 60.0, 2)
        cost_per_audio_min = round(audio_rel_cost / audio_mins, 6) if audio_mins > 0 else None
        cost_per_event = round(cost / events, 6) if events > 0 else None

        group_key = getattr(r, "dim_name", None)
        group_id = str(getattr(r, "dim_id", "")) if hasattr(r, "dim_id") and getattr(r, "dim_id") is not None else None

        points.append(
            UsageTimeseriesPoint(
                timestamp=r.timestamp,
                bucket_label=str(r.bucket_label),
                group_key=group_key,
                group_id=group_id,
                total_events=events,
                total_cost_usd=round(cost, 6),
                total_tokens=int(r.total_tokens or 0),
                prompt_tokens=int(r.prompt_tokens or 0),
                candidates_tokens=int(r.candidates_tokens or 0),
                cached_tokens=int(r.cached_tokens or 0),
                thoughts_tokens=int(r.thoughts_tokens or 0),
                audio_seconds=round(audio_sec, 2),
                audio_minutes=audio_mins,
                audio_related_cost_usd=round(audio_rel_cost, 6),
                cost_per_audio_minute_usd=cost_per_audio_min,
                cost_per_event_usd=cost_per_event,
            )
        )

    return UsageTimeseriesResponse(
        period=eff_period,
        granularity=eff_granularity,
        group_by=norm_group_by,
        date_from=dt_from_utc,
        date_to=dt_to_utc,
        currency="USD",
        total_cost_usd=round(total_cost_all, 6),
        points=points,
    )
