"""
Centralised analysis persistence service.

save_analysis() is the single entry point for writing any analysis to DB:
  1. Inserts a row in bm_analyses.
  2. Upserts bm_call_analysis_current.
  3. Inserts per-criterion rows in bm_analysis_results.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyses import Analysis, AnalysisResult, CallAnalysisCurrent
from app.models.criteria import PromptCriterion
from app.services.analysis_results_mapper import map_criterion_value
from app.services.criteria_service import get_active_criteria
from app.utils.dates import safe_parse_datetime

logger = logging.getLogger(__name__)

# Claves legacy explícitamente descatalogadas que deben eliminarse antes de persistir
_LEGACY_KEYS: frozenset[str] = frozenset({
    "campo_1", "campo_1_feed",
    "campo_2", "campo_2_feed",
    "campo_3", "campo_3_feed",
    "campo_4", "campo_4_feed",
    "campo_5", "campo_5_feed",
})


def _strip_legacy_keys(result_json: dict[str, Any]) -> dict[str, Any]:
    """
    Remove explicitly deprecated legacy keys from the AI result dict.
    Valid feed_keys (e.g. sentiment_feed, empatia_feed) are NOT removed.
    Returns a new dict; does not mutate the original.
    """
    removed = [k for k in result_json if k in _LEGACY_KEYS]
    if removed:
        logger.warning("Stripped legacy keys from result_json: %s", removed)
    return {k: v for k, v in result_json.items() if k not in _LEGACY_KEYS}


def _ensure_aware(dt: datetime | None, fallback: datetime) -> datetime:
    """
    Return a timezone-aware datetime.
    - If dt is None → return fallback (which must already be aware).
    - If dt is naive → assign UTC.
    - If dt is aware → return as-is.
    """
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def safe_parse_number(value: Any) -> Decimal | None:
    """
    Coerce a value to Decimal for numeric DB columns.

    Handles:
      - None / "" / whitespace-only string → None
      - int, float, Decimal               → Decimal(value)
      - str with digits (e.g. "9", "8.5") → Decimal(value)

    Returns None if conversion fails.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


async def save_analysis(
    db: AsyncSession,
    *,
    analysis_type: str,                      # "audio" | "text"
    call_metadata: dict[str, Any],           # call_id, hubspot_url, direction, timestamp, etc.
    prompt_metadata: dict[str, Any],         # prompt_id, prompt_version_id
    model_metadata: dict[str, Any],          # model_provider, model_name, etc.
    result_json: dict[str, Any],             # raw AI output (parsed JSON)
    payload: dict[str, Any],                 # full payload for audit
    transcription: str | None = None,
    transcription_metadata: dict[str, Any] | None = None,
) -> Analysis:
    """
    Persist a complete analysis run:
      1. bm_analyses (new row)
      2. bm_call_analysis_current (upsert)
      3. bm_analysis_results (per criterion)
    """
    now = datetime.now(timezone.utc)
    call_id = call_metadata.get("call_id", "")

    # ── Normalise all datetime fields defensively ──────────────────────────
    run_ts = _ensure_aware(
        safe_parse_datetime(call_metadata.get("run_ts")),
        fallback=now,
    )
    fecha_eval = _ensure_aware(
        safe_parse_datetime(call_metadata.get("fecha_eval")),
        fallback=now,
    )
    call_timestamp: datetime | None = safe_parse_datetime(call_metadata.get("call_timestamp"))
    if call_timestamp is not None and call_timestamp.tzinfo is None:
        call_timestamp = call_timestamp.replace(tzinfo=timezone.utc)

    # ── Resolve agent name defensively ────────────────────────────────────
    from app.utils.hubspot_owners import resolve_agent_display
    raw_agent = call_metadata.get("agente_telefonico")
    owner_id = call_metadata.get("hubspot_owner_id")
    resolved_agent = resolve_agent_display(raw_agent, owner_id)

    # ── Strip legacy keys from result ─────────────────────────────────────
    clean_result = _strip_legacy_keys(result_json)

    try:
        # ── 1. Insert bm_analyses ──────────────────────────────────────────
        analysis = Analysis(
            analysis_type=analysis_type,
            call_id=call_id,
            hubspot_url=call_metadata.get("hubspot_url"),
            call_direction=call_metadata.get("call_direction"),
            call_timestamp=call_timestamp,
            source=call_metadata.get("source", "api"),
            run_ts=run_ts,
            fecha_eval=fecha_eval,
            agente_telefonico=resolved_agent,
            hubspot_owner_id=owner_id,
            prompt_id=prompt_metadata.get("prompt_id"),
            prompt_version_id=prompt_metadata.get("prompt_version_id"),
            transcription=transcription,
            transcription_provider=(transcription_metadata or {}).get("provider"),
            transcription_model=(transcription_metadata or {}).get("model"),
            model_provider=model_metadata.get("model_provider"),
            model_name=model_metadata.get("model_name"),
            status="completed",
            tipo_llamada=clean_result.get("tipo_llamada"),
            evaluacion_global=safe_parse_number(clean_result.get("evaluacion_global")),
            result=clean_result,
            payload=payload,
        )
        db.add(analysis)
        await db.flush()  # get analysis_id without committing

        # ── 2. Upsert bm_call_analysis_current ────────────────────────────
        await _upsert_current(db, analysis, call_metadata)

        # ── 3. Insert bm_analysis_results ─────────────────────────────────
        prompt_id = prompt_metadata.get("prompt_id")
        if prompt_id:
            await _insert_results(db, analysis, clean_result, prompt_id)

        await db.commit()
        await db.refresh(analysis)
        logger.info(
            "Saved analysis analysis_id=%s call_id=%s type=%s",
            analysis.analysis_id, call_id, analysis_type,
        )
        return analysis

    except Exception:
        # Roll back so the session is left in a clean state.
        # The caller's except block will format the error response.
        await db.rollback()
        raise


async def _upsert_current(
    db: AsyncSession,
    analysis: Analysis,
    call_metadata: dict[str, Any],
) -> None:
    """
    Upsert bm_call_analysis_current for call_id + analysis_type.

    Uses SQLAlchemy's PostgreSQL dialect insert().on_conflict_do_update() so
    that all values — including JSONB, timestamptz and numeric — are bound
    through SQLAlchemy's type system.  This avoids the asyncpg syntax error
    that results from mixing $N positional placeholders with :param::cast
    expressions in a raw text() query.
    """
    now = datetime.now(timezone.utc)

    stmt = pg_insert(CallAnalysisCurrent).values(
        call_id=analysis.call_id,
        analysis_type=analysis.analysis_type,
        latest_analysis_id=analysis.analysis_id,
        hubspot_url=analysis.hubspot_url,
        call_direction=analysis.call_direction,
        call_timestamp=analysis.call_timestamp,
        source=analysis.source,
        fecha_eval=analysis.fecha_eval,
        updated_at=now,
        agente_telefonico=analysis.agente_telefonico,
        hubspot_owner_id=analysis.hubspot_owner_id,
        prompt_id=analysis.prompt_id,
        prompt_version_id=analysis.prompt_version_id,
        status=analysis.status,
        tipo_llamada=analysis.tipo_llamada,
        evaluacion_global=analysis.evaluacion_global,
        result=analysis.result,
        payload=analysis.payload,
    )

    stmt = stmt.on_conflict_do_update(
        index_elements=["call_id", "analysis_type"],
        set_={
            "latest_analysis_id": stmt.excluded.latest_analysis_id,
            "hubspot_url": stmt.excluded.hubspot_url,
            "call_direction": stmt.excluded.call_direction,
            "call_timestamp": stmt.excluded.call_timestamp,
            "source": stmt.excluded.source,
            "fecha_eval": stmt.excluded.fecha_eval,
            "updated_at": now,
            "agente_telefonico": stmt.excluded.agente_telefonico,
            "hubspot_owner_id": stmt.excluded.hubspot_owner_id,
            "prompt_id": stmt.excluded.prompt_id,
            "prompt_version_id": stmt.excluded.prompt_version_id,
            "status": stmt.excluded.status,
            "tipo_llamada": stmt.excluded.tipo_llamada,
            "evaluacion_global": stmt.excluded.evaluacion_global,
            "result": stmt.excluded.result,
            "payload": stmt.excluded.payload,
        },
    )

    await db.execute(stmt)


async def _insert_results(
    db: AsyncSession,
    analysis: Analysis,
    result_json: dict[str, Any],
    prompt_id: int,
) -> None:
    """Insert per-criterion rows in bm_analysis_results."""
    criteria: list[PromptCriterion] = await get_active_criteria(db, prompt_id)

    for criterion in criteria:
        output_key = criterion.output_key
        feed_key = criterion.feed_key

        raw_value = result_json.get(output_key) if output_key else None
        feed_value = result_json.get(feed_key) if feed_key else None

        typed = map_criterion_value(raw_value, criterion.criterion_type or "text")

        row = AnalysisResult(
            analysis_id=analysis.analysis_id,
            criterion_id=criterion.criterion_id,
            criterion_key=criterion.criterion_key,
            criterion_name=criterion.criterion_name,
            criterion_type=criterion.criterion_type,
            value_number=typed["value_number"],
            value_text=typed["value_text"],
            value_boolean=typed["value_boolean"],
            value_category=typed["value_category"],
            feed=str(feed_value) if feed_value is not None else None,
            description=criterion.criterion_description,
            raw_value=typed["raw_value"],
        )
        db.add(row)
