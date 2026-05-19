"""Dashboard service for calculating real metrics."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyses import Analysis
from app.utils.hubspot_owners import resolve_agent_display, resolve_owner_name

logger = logging.getLogger(__name__)


def _get_duration_sec(payload: Any) -> float | None:
    if not payload or not isinstance(payload, dict):
        return None
    hs_data = payload.get("hubspot_data")
    if not hs_data or not isinstance(hs_data, dict):
        return None
    dur = hs_data.get("call_duration")
    if dur is None:
        return None
    try:
        dur_ms = float(dur)
        return dur_ms / 1000.0
    except ValueError:
        return None


def _has_objections(result: Any) -> bool:
    if not result or not isinstance(result, dict):
        return False
    
    # 1. Check direct 'objeciones' list/dict/string
    objs = result.get("objeciones")
    if objs:
        if isinstance(objs, (list, dict)) and len(objs) > 0:
            return True
        if isinstance(objs, str) and objs.strip():
            return True
            
    # 2. Check legacy object fields (objecion_1, objecion_2, objecion_3)
    for k in ["objecion_1", "objecion_2", "objecion_3"]:
        val = result.get(k)
        if val is not None:
            if isinstance(val, str) and val.strip().lower() not in ["", "null", "none"]:
                return True
            elif not isinstance(val, str):
                return True
    return False


def _get_sentiment(result: Any) -> float | None:
    if not result or not isinstance(result, dict):
        return None
    val = result.get("sentiment") or result.get("sentimiento") or result.get("evaluacion_sentimiento")
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _round_dt(dt: datetime, interval: str) -> datetime:
    if interval == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    else:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _calc_delta(actual: float, anterior: float) -> float | None:
    if anterior is None or anterior == 0:
        return None
    return round(((actual - anterior) / anterior) * 100, 1)


async def get_dashboard_summary(
    db: AsyncSession,
    analysis_type: str = "audio",
    period: str = "24h"
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    
    # Resolve timeframe
    if period == "7d":
        delta = timedelta(days=7)
        bucket_interval = "day"
    elif period == "30d":
        delta = timedelta(days=30)
        bucket_interval = "day"
    else:
        period = "24h"
        delta = timedelta(hours=24)
        bucket_interval = "hour"

    start_actual = now - delta
    end_actual = now
    start_anterior = now - (delta * 2)
    end_anterior = now - delta

    # Single robust query fetching all records in both current and previous period
    stmt = select(
        Analysis.analysis_id,
        Analysis.call_id,
        Analysis.agente_telefonico,
        Analysis.hubspot_owner_id,
        Analysis.tipo_llamada,
        Analysis.evaluacion_global,
        Analysis.fecha_eval,
        Analysis.status,
        Analysis.result,
        Analysis.payload
    ).where(
        Analysis.analysis_type == analysis_type,
        Analysis.fecha_eval >= start_anterior,
        Analysis.fecha_eval <= end_actual,
        Analysis.status == "completed"
    )

    result = await db.execute(stmt)
    rows = result.fetchall()

    actual_rows = []
    anterior_rows = []

    for r in rows:
        fe = r.fecha_eval
        if not fe:
            continue
        if fe.tzinfo is None:
            fe = fe.replace(tzinfo=timezone.utc)
        
        if start_actual <= fe <= end_actual:
            actual_rows.append(r)
        elif start_anterior <= fe < start_actual:
            anterior_rows.append(r)

    # ── Calculate metrics for Actual ──────────────────────────────────────────
    total_analyses = len(actual_rows)
    
    # Global score
    evals = [r.evaluacion_global for r in actual_rows if r.evaluacion_global is not None]
    avg_eval = round(sum(evals) / len(evals), 1) if evals else 0.0
    
    # Cita Rate
    citas = sum(1 for r in actual_rows if r.tipo_llamada == "cita")
    total_tipo = sum(1 for r in actual_rows if r.tipo_llamada is not None)
    cita_rate = round((citas / total_tipo) * 100) if total_tipo > 0 else 0

    # Call Duration
    durs = [_get_duration_sec(r.payload) for r in actual_rows]
    durs = [d for d in durs if d is not None]
    avg_dur = round(sum(durs) / len(durs)) if durs else None

    # Objections
    total_objeciones = sum(1 for r in actual_rows if _has_objections(r.result))

    # ── Calculate metrics for Anterior ────────────────────────────────────────
    total_analyses_ant = len(anterior_rows)
    
    evals_ant = [r.evaluacion_global for r in anterior_rows if r.evaluacion_global is not None]
    avg_eval_ant = sum(evals_ant) / len(evals_ant) if evals_ant else 0.0
    
    citas_ant = sum(1 for r in anterior_rows if r.tipo_llamada == "cita")
    total_tipo_ant = sum(1 for r in anterior_rows if r.tipo_llamada is not None)
    cita_rate_ant = (citas_ant / total_tipo_ant) * 100 if total_tipo_ant > 0 else 0.0

    durs_ant = [_get_duration_sec(r.payload) for r in anterior_rows]
    durs_ant = [d for d in durs_ant if d is not None]
    avg_dur_ant = sum(durs_ant) / len(durs_ant) if durs_ant else 0.0

    total_objeciones_ant = sum(1 for r in anterior_rows if _has_objections(r.result))

    # Comparisons
    comparisons = {
        "total_analyses_delta_pct": _calc_delta(total_analyses, total_analyses_ant),
        "pending_delta_pct": 0.0,
        "avg_evaluacion_global_delta_pct": _calc_delta(avg_eval, avg_eval_ant),
        "cita_rate_delta_pct": _calc_delta(cita_rate, cita_rate_ant),
        "avg_duration_delta_pct": _calc_delta(avg_dur or 0.0, avg_dur_ant),
        "total_objeciones_delta_pct": _calc_delta(total_objeciones, total_objeciones_ant)
    }

    # ── Calls Evolution ───────────────────────────────────────────────────────
    # Generate all buckets to prevent holes
    buckets = []
    if bucket_interval == "hour":
        curr = _round_dt(start_actual, "hour")
        while curr <= end_actual:
            buckets.append(curr)
            curr += timedelta(hours=1)
    else:
        curr = _round_dt(start_actual, "day")
        while curr <= end_actual:
            buckets.append(curr)
            curr += timedelta(days=1)

    grouped_evolution = {}
    for r in actual_rows:
        fe = r.fecha_eval
        if fe.tzinfo is None:
            fe = fe.replace(tzinfo=timezone.utc)
        b = _round_dt(fe, bucket_interval)
        if b not in grouped_evolution:
            grouped_evolution[b] = {"total": 0, "citas": 0, "sin_cita": 0}
        grouped_evolution[b]["total"] += 1
        if r.tipo_llamada == "cita":
            grouped_evolution[b]["citas"] += 1
        else:
            grouped_evolution[b]["sin_cita"] += 1

    calls_evolution = []
    for b in buckets:
        data = grouped_evolution.get(b, {"total": 0, "citas": 0, "sin_cita": 0})
        calls_evolution.append({
            "bucket": b.isoformat(),
            "total": data["total"],
            "citas": data["citas"],
            "sin_cita": data["sin_cita"]
        })

    # ── Type Distribution ─────────────────────────────────────────────────────
    dist = {}
    for r in actual_rows:
        t = r.tipo_llamada or "desconocido"
        dist[t] = dist.get(t, 0) + 1
    type_distribution = [{"tipo_llamada": k, "count": v} for k, v in dist.items()]

    # ── Sentiment Evolution ───────────────────────────────────────────────────
    sentiment_grouped = {}
    for r in actual_rows:
        fe = r.fecha_eval
        if fe.tzinfo is None:
            fe = fe.replace(tzinfo=timezone.utc)
        b = _round_dt(fe, bucket_interval)
        sent = _get_sentiment(r.result)
        if sent is not None:
            if b not in sentiment_grouped:
                sentiment_grouped[b] = []
            sentiment_grouped[b].append(sent)

    sentiment_evolution = []
    for b in buckets:
        vals = sentiment_grouped.get(b, [])
        avg_sent = round(sum(vals) / len(vals), 1) if vals else None
        sentiment_evolution.append({
            "bucket": b.isoformat(),
            "avg_sentiment": avg_sent
        })

    # ── Agent Ranking ─────────────────────────────────────────────────────────
    agent_data = {}
    for r in actual_rows:
        resolved_name = resolve_agent_display(r.agente_telefonico, r.hubspot_owner_id)
        if not resolved_name:
            resolved_name = "Desconocido"
        if resolved_name not in agent_data:
            agent_data[resolved_name] = {
                "evals": [],
                "citas": 0,
                "total_tipo": 0,
                "total_analyses": 0
            }
        agent_data[resolved_name]["total_analyses"] += 1
        if r.evaluacion_global is not None:
            agent_data[resolved_name]["evals"].append(r.evaluacion_global)
        if r.tipo_llamada is not None:
            agent_data[resolved_name]["total_tipo"] += 1
            if r.tipo_llamada == "cita":
                agent_data[resolved_name]["citas"] += 1

    ranking = []
    for name, data in agent_data.items():
        avg_eval_score = round(sum(data["evals"]) / len(data["evals"]), 1) if data["evals"] else 0.0
        cita_rate_score = round((data["citas"] / data["total_tipo"]) * 100) if data["total_tipo"] > 0 else 0
        ranking.append({
            "agente_telefonico": name,
            "total_analyses": data["total_analyses"],
            "avg_evaluacion_global": avg_eval_score,
            "cita_rate": cita_rate_score
        })

    ranking.sort(key=lambda x: (x["total_analyses"], x["avg_evaluacion_global"]), reverse=True)
    agent_ranking = ranking[:5]

    # ── Latest Analyses ───────────────────────────────────────────────────────
    sorted_actual = sorted(actual_rows, key=lambda x: x.fecha_eval or datetime.min, reverse=True)
    latest_analyses = []
    for r in sorted_actual[:8]:
        resolved_agent = resolve_agent_display(r.agente_telefonico, r.hubspot_owner_id)
        latest_analyses.append({
            "analysis_id": r.analysis_id,
            "call_id": r.call_id,
            "agente_telefonico": resolved_agent,
            "tipo_llamada": r.tipo_llamada,
            "evaluacion_global": float(r.evaluacion_global) if r.evaluacion_global is not None else None,
            "fecha_eval": r.fecha_eval.isoformat() if r.fecha_eval else None,
            "status": r.status
        })

    return {
        "period": period,
        "analysis_type": analysis_type,
        "generated_at": now.isoformat(),
        "kpis": {
            "total_analyses": total_analyses,
            "pending": 0,
            "pending_available": False,
            "avg_evaluacion_global": avg_eval,
            "cita_rate": cita_rate,
            "avg_duration_seconds": avg_dur,
            "total_objeciones": total_objeciones
        },
        "comparisons": comparisons,
        "calls_evolution": calls_evolution,
        "type_distribution": type_distribution,
        "sentiment_evolution": sentiment_evolution,
        "agent_ranking": agent_ranking,
        "latest_analyses": latest_analyses
    }
