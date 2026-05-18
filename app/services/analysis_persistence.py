"""
Centralised analysis persistence service.

save_analysis() is the single entry point for writing any analysis to DB:
  1. Inserts a row in bm_analyses.
  2. Upserts bm_call_analysis_current.
  3. Inserts per-criterion rows in bm_analysis_results.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyses import Analysis, AnalysisResult, CallAnalysisCurrent
from app.models.criteria import PromptCriterion
from app.services.analysis_results_mapper import map_criterion_value
from app.services.criteria_service import get_active_criteria
from app.utils.dates import safe_parse_datetime

logger = logging.getLogger(__name__)


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
    
    call_timestamp = safe_parse_datetime(call_metadata.get("call_timestamp"))
    
    fecha_eval_raw = call_metadata.get("fecha_eval")
    if fecha_eval_raw:
        fecha_eval = safe_parse_datetime(fecha_eval_raw) or now
    else:
        fecha_eval = now

    # ── 1. Insert bm_analyses ──────────────────────────────────────────────
    analysis = Analysis(
        analysis_type=analysis_type,
        call_id=call_id,
        hubspot_url=call_metadata.get("hubspot_url"),
        call_direction=call_metadata.get("call_direction"),
        call_timestamp=call_timestamp,
        source=call_metadata.get("source", "api"),
        run_ts=now,
        fecha_eval=fecha_eval,
        agente_telefonico=call_metadata.get("agente_telefonico"),
        hubspot_owner_id=call_metadata.get("hubspot_owner_id"),
        prompt_id=prompt_metadata.get("prompt_id"),
        prompt_version_id=prompt_metadata.get("prompt_version_id"),
        transcription=transcription,
        transcription_provider=(transcription_metadata or {}).get("provider"),
        transcription_model=(transcription_metadata or {}).get("model"),
        model_provider=model_metadata.get("model_provider"),
        model_name=model_metadata.get("model_name"),
        status="completed",
        tipo_llamada=result_json.get("tipo_llamada"),
        evaluacion_global=str(result_json.get("evaluacion_global", "")) or None,
        result=result_json,
        payload=payload,
    )
    db.add(analysis)
    await db.flush()  # get analysis_id without committing

    # ── 2. Upsert bm_call_analysis_current ────────────────────────────────
    await _upsert_current(db, analysis, call_metadata)

    # ── 3. Insert bm_analysis_results ─────────────────────────────────────
    prompt_id = prompt_metadata.get("prompt_id")
    if prompt_id:
        await _insert_results(db, analysis, result_json, prompt_id)

    await db.commit()
    await db.refresh(analysis)
    logger.info("Saved analysis analysis_id=%s call_id=%s type=%s", analysis.analysis_id, call_id, analysis_type)
    return analysis


async def _upsert_current(
    db: AsyncSession,
    analysis: Analysis,
    call_metadata: dict[str, Any],
) -> None:
    """Upsert bm_call_analysis_current for call_id + analysis_type."""
    # Use raw SQL for upsert (PostgreSQL ON CONFLICT DO UPDATE)
    stmt = text(
        """
        INSERT INTO bm_call_analysis_current (
            call_id, analysis_type, latest_analysis_id,
            hubspot_url, call_direction, call_timestamp, source,
            fecha_eval, updated_at, agente_telefonico, hubspot_owner_id,
            prompt_id, prompt_version_id, status,
            tipo_llamada, evaluacion_global, result, payload
        ) VALUES (
            :call_id, :analysis_type, :latest_analysis_id,
            :hubspot_url, :call_direction, :call_timestamp, :source,
            :fecha_eval, NOW(), :agente_telefonico, :hubspot_owner_id,
            :prompt_id, :prompt_version_id, :status,
            :tipo_llamada, :evaluacion_global, :result::jsonb, :payload::jsonb
        )
        ON CONFLICT (call_id, analysis_type) DO UPDATE SET
            latest_analysis_id = EXCLUDED.latest_analysis_id,
            hubspot_url = EXCLUDED.hubspot_url,
            call_direction = EXCLUDED.call_direction,
            call_timestamp = EXCLUDED.call_timestamp,
            source = EXCLUDED.source,
            fecha_eval = EXCLUDED.fecha_eval,
            updated_at = NOW(),
            agente_telefonico = EXCLUDED.agente_telefonico,
            hubspot_owner_id = EXCLUDED.hubspot_owner_id,
            prompt_id = EXCLUDED.prompt_id,
            prompt_version_id = EXCLUDED.prompt_version_id,
            status = EXCLUDED.status,
            tipo_llamada = EXCLUDED.tipo_llamada,
            evaluacion_global = EXCLUDED.evaluacion_global,
            result = EXCLUDED.result,
            payload = EXCLUDED.payload
        """
    )

    import json

    await db.execute(
        stmt,
        {
            "call_id": analysis.call_id,
            "analysis_type": analysis.analysis_type,
            "latest_analysis_id": analysis.analysis_id,
            "hubspot_url": analysis.hubspot_url,
            "call_direction": analysis.call_direction,
            "call_timestamp": analysis.call_timestamp,
            "source": analysis.source,
            "fecha_eval": analysis.fecha_eval,
            "agente_telefonico": analysis.agente_telefonico,
            "hubspot_owner_id": analysis.hubspot_owner_id,
            "prompt_id": analysis.prompt_id,
            "prompt_version_id": analysis.prompt_version_id,
            "status": analysis.status,
            "tipo_llamada": analysis.tipo_llamada,
            "evaluacion_global": analysis.evaluacion_global,
            "result": json.dumps(analysis.result) if analysis.result else None,
            "payload": json.dumps(analysis.payload) if analysis.payload else None,
        },
    )


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
