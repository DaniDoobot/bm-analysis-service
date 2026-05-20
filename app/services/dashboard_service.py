"""Dashboard service for calculating real metrics."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyses import Analysis
from app.utils.hubspot_owners import resolve_agent_display, resolve_owner_name, OWNER_TO_NAME

logger = logging.getLogger(__name__)

CRITERIA_NAMES = {
    "sentiment": "Sentimiento",
    "evaluacion_global": "Evaluación Global",
    "empatia": "Empatía",
    "simpatia": "Simpatía",
    "claridad": "Claridad",
    "procedimiento": "Procedimiento",
    "saludo_inicio": "Saludo de Inicio",
    "n3_preguntas": "N3 Preguntas",
    "despedida_con_refuerzo": "Despedida con Refuerzo",
    "gestion_objeciones": "Gestión de Objeciones",
    "uso_nombre_paciente": "Uso del Nombre del Paciente",
    "uso_preguntas": "Uso de Preguntas",
    "explicaciones_medicas": "Explicaciones Médicas",
    "claridad_explicacion_economica": "Claridad Explicación Económica"
}

CATEGORIES = [
    {"key": "precio/coste", "label": "Precio / coste", "keywords": ["precio", "coste", "caro", "dinero", "pagar", "consulta", "presupuesto", "financiar", "financiación", "pago"]},
    {"key": "disponibilidad/agenda", "label": "Disponibilidad / agenda", "keywords": ["horario", "disponibilidad", "fecha", "cita", "agenda", "mañana", "tarde", "hora", "calendario", "sábado", "sabado"]},
    {"key": "pareja/familia", "label": "Pareja / familia", "keywords": ["mujer", "pareja", "esposa", "marido", "familia", "hijo", "esposo", "consultar"]},
    {"key": "miedo/duda clínica", "label": "Miedo / duda clínica", "keywords": ["miedo", "duda", "tratamiento", "médico", "doctor", "problema", "enfermedad", "dolor", "operación", "riesgo", "efectos"]},
    {"key": "no interesado", "label": "No interesado", "keywords": ["no interesado", "no quiere", "no desea", "no le interesa", "desinterés", "desinteres"]}
]


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


def extract_score(result: Any, key: str) -> float | None:
    if not result or not isinstance(result, dict):
        return None
    val = result.get(key)
    if val is None:
        if key == "sentiment":
            val = result.get("sentiment") or result.get("sentimiento")
        elif key == "procedimiento":
            val = result.get("procedimiento") or result.get("adherencia_procedimiento")
            
    if val is None:
        return None
        
    try:
        if isinstance(val, dict):
            for skey in ["score", "valor", "value", "puntuacion"]:
                if val.get(skey) is not None:
                    return float(val[skey])
        return float(val)
    except (ValueError, TypeError):
        if isinstance(val, str):
            cleaned = val.strip().lower()
            if cleaned in ["si", "sí"]:
                return 10.0
            if cleaned == "no":
                return 0.0
        return None


def get_avg_score(analyses: list[Analysis], key: str) -> float | None:
    scores = []
    for a in analyses:
        if key == "evaluacion_global" and a.evaluacion_global is not None:
            scores.append(float(a.evaluacion_global))
        else:
            s = extract_score(a.result, key)
            if s is not None:
                scores.append(s)
    return round(sum(scores) / len(scores), 1) if scores else None


def extract_objection_items(result: Any) -> list[str]:
    items = []
    if not result or not isinstance(result, dict):
        return items
        
    for k in ["objecion_1", "objecion_2", "objecion_3"]:
        val = result.get(k)
        if val and isinstance(val, str) and val.strip().lower() not in ["", "null", "none"]:
            items.append(val.strip())
            
    objs = result.get("objeciones")
    if objs:
        if isinstance(objs, list):
            for o in objs:
                if isinstance(o, str) and o.strip():
                    items.append(o.strip())
                elif isinstance(o, dict) and o.get("texto"):
                    items.append(o["texto"].strip())
        elif isinstance(objs, str) and objs.strip():
            if not items:
                items.append(objs.strip())
                
    return items


def categorize_text(text: str) -> str:
    if not text:
        return "Otros"
    txt_lower = text.lower()
    for cat in CATEGORIES:
        for kw in cat["keywords"]:
            if kw in txt_lower:
                return cat["label"]
    return "Otros"


def _get_objection_metrics(rows: list[Any]) -> tuple[int, int]:
    calls = 0
    items = 0
    for r in rows:
        if _has_objections(r.result):
            calls += 1
            texts = extract_objection_items(r.result)
            items += len(texts) if texts else 1
    return calls, items


# ── Existing dashboard summary ────────────────────────────────────────────────
async def get_dashboard_summary(
    db: AsyncSession,
    analysis_type: str = "audio",
    period: str = "24h"
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    
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

    stmt = select(
        Analysis.analysis_id,
        Analysis.call_id,
        Analysis.agente_telefonico,
        Analysis.hubspot_owner_id,
        Analysis.tipo_llamada,
        Analysis.evaluacion_global,
        Analysis.fecha_eval,
        Analysis.call_timestamp,
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

    total_analyses = len(actual_rows)
    evals = [r.evaluacion_global for r in actual_rows if r.evaluacion_global is not None]
    avg_eval = round(sum(evals) / len(evals), 1) if evals else 0.0
    
    citas = sum(1 for r in actual_rows if r.tipo_llamada == "cita")
    total_tipo = sum(1 for r in actual_rows if r.tipo_llamada is not None)
    cita_rate = round((citas / total_tipo) * 100) if total_tipo > 0 else 0

    durs = [_get_duration_sec(r.payload) for r in actual_rows]
    durs = [d for d in durs if d is not None]
    avg_dur = round(sum(durs) / len(durs)) if durs else None

    total_objection_calls, total_objection_items = _get_objection_metrics(actual_rows)

    total_analyses_ant = len(anterior_rows)
    evals_ant = [r.evaluacion_global for r in anterior_rows if r.evaluacion_global is not None]
    avg_eval_ant = sum(evals_ant) / len(evals_ant) if evals_ant else 0.0
    
    citas_ant = sum(1 for r in anterior_rows if r.tipo_llamada == "cita")
    total_tipo_ant = sum(1 for r in anterior_rows if r.tipo_llamada is not None)
    cita_rate_ant = (citas_ant / total_tipo_ant) * 100 if total_tipo_ant > 0 else 0.0

    durs_ant = [_get_duration_sec(r.payload) for r in anterior_rows]
    durs_ant = [d for d in durs_ant if d is not None]
    avg_dur_ant = sum(durs_ant) / len(durs_ant) if durs_ant else 0.0

    total_objection_calls_ant, total_objection_items_ant = _get_objection_metrics(anterior_rows)

    comparisons = {
        "total_analyses_delta_pct": _calc_delta(total_analyses, total_analyses_ant),
        "pending_delta_pct": 0.0,
        "avg_evaluacion_global_delta_pct": _calc_delta(avg_eval, avg_eval_ant),
        "cita_rate_delta_pct": _calc_delta(cita_rate, cita_rate_ant),
        "avg_duration_delta_pct": _calc_delta(avg_dur or 0.0, avg_dur_ant),
        "total_objeciones_delta_pct": _calc_delta(total_objection_items, total_objection_items_ant),
        "total_objection_calls_delta_pct": _calc_delta(total_objection_calls, total_objection_calls_ant),
        "total_objection_items_delta_pct": _calc_delta(total_objection_items, total_objection_items_ant)
    }

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

    dist = {}
    for r in actual_rows:
        t = r.tipo_llamada or "desconocido"
        dist[t] = dist.get(t, 0) + 1
    type_distribution = [{"tipo_llamada": k, "count": v} for k, v in dist.items()]

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
            "call_timestamp": r.call_timestamp.isoformat() if r.call_timestamp else None,
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
            "total_objeciones": total_objection_items,  # compatibility fallback (points to items)
            "total_objection_calls": total_objection_calls,
            "total_objection_items": total_objection_items
        },
        "comparisons": comparisons,
        "calls_evolution": calls_evolution,
        "type_distribution": type_distribution,
        "sentiment_evolution": sentiment_evolution,
        "agent_ranking": agent_ranking,
        "latest_analyses": latest_analyses
    }


# ── A) GET /bm/agents ──────────────────────────────────────────────────────────
async def get_agents_list(db: AsyncSession) -> list[dict[str, Any]]:
    # Get statistics from db
    stmt = select(
        Analysis.hubspot_owner_id,
        func.count(Analysis.analysis_id).label("total_analyses"),
        func.max(Analysis.fecha_eval).label("last_analysis_at"),
        func.avg(Analysis.evaluacion_global).label("avg_evaluacion_global")
    ).where(
        Analysis.status == "completed"
    ).group_by(
        Analysis.hubspot_owner_id
    )
    
    result = await db.execute(stmt)
    db_rows = result.fetchall()
    
    db_stats = {r.hubspot_owner_id: r for r in db_rows if r.hubspot_owner_id}
    
    results = []
    
    # 1. Process static mapping first to ensure they are returned
    for oid, name in OWNER_TO_NAME.items():
        stats = db_stats.get(oid)
        results.append({
            "hubspot_owner_id": oid,
            "agent_name": name,
            "total_analyses": stats.total_analyses if stats else 0,
            "last_analysis_at": stats.last_analysis_at.isoformat() if stats and stats.last_analysis_at else None,
            "avg_evaluacion_global": round(float(stats.avg_evaluacion_global), 1) if stats and stats.avg_evaluacion_global is not None else 0.0
        })
        
    # 2. Add extra agents found in DB not in mapping
    extra_ids = [oid for oid in db_stats.keys() if oid not in OWNER_TO_NAME]
    if extra_ids:
        # Resolve their names dynamically from the DB history
        extra_names = {}
        for oid in extra_ids:
            qr = await db.execute(
                select(Analysis.agente_telefonico)
                .where(Analysis.hubspot_owner_id == oid)
                .where(Analysis.agente_telefonico.is_not(None))
                .where(~Analysis.agente_telefonico.op('~')('^[0-9]+$'))
                .order_by(Analysis.analysis_id.desc())
                .limit(1)
            )
            val = qr.scalar()
            extra_names[oid] = val or oid
            
        for oid in extra_ids:
            stats = db_stats[oid]
            results.append({
                "hubspot_owner_id": oid,
                "agent_name": extra_names.get(oid, oid),
                "total_analyses": stats.total_analyses,
                "last_analysis_at": stats.last_analysis_at.isoformat() if stats.last_analysis_at else None,
                "avg_evaluacion_global": round(float(stats.avg_evaluacion_global), 1) if stats.avg_evaluacion_global is not None else 0.0
            })
            
    return results


# ── B) GET /bm/agents/{hubspot_owner_id}/evolution ─────────────────────────────
async def get_agent_evolution(
    db: AsyncSession,
    hubspot_owner_id: str,
    analysis_type: str = "audio",
    period: str = "30d",
    bucket_param: str | None = None,
    prompt_version_id: int | None = None
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    
    # 1. Resolve timeframe
    if period == "7d":
        delta = timedelta(days=7)
        start_date = now - delta
    elif period == "30d":
        delta = timedelta(days=30)
        start_date = now - delta
    elif period == "90d":
        delta = timedelta(days=90)
        start_date = now - delta
    else:
        period = "all"
        start_date = None
        
    stmt = select(Analysis).where(
        Analysis.hubspot_owner_id == hubspot_owner_id,
        Analysis.analysis_type == analysis_type,
        Analysis.status == "completed"
    )
    if start_date:
        stmt = stmt.where(Analysis.fecha_eval >= start_date)
    if prompt_version_id is not None:
        stmt = stmt.where(Analysis.prompt_version_id == prompt_version_id)
        
    stmt = stmt.order_by(Analysis.fecha_eval.asc())
    
    result = await db.execute(stmt)
    analyses = list(result.scalars().all())
    
    agent_name = resolve_owner_name(hubspot_owner_id)
    if not agent_name and analyses:
        # Fallback to DB history
        for a in reversed(analyses):
            if a.agente_telefonico and not a.agente_telefonico.isdigit():
                agent_name = a.agente_telefonico
                break
    if not agent_name:
        agent_name = hubspot_owner_id
        
    # ── Summary Calculations ──────────────────────────────────────────────────
    total_analyses = len(analyses)
    first_analysis_at = analyses[0].fecha_eval.isoformat() if analyses and analyses[0].fecha_eval else None
    last_analysis_at = analyses[-1].fecha_eval.isoformat() if analyses and analyses[-1].fecha_eval else None
    
    avg_eval = get_avg_score(analyses, "evaluacion_global") or 0.0
    avg_sent = get_avg_score(analyses, "sentiment") or 0.0
    avg_emp = get_avg_score(analyses, "empatia") or 0.0
    avg_cla = get_avg_score(analyses, "claridad") or 0.0
    avg_sim = get_avg_score(analyses, "simpatia") or 0.0
    avg_pro = get_avg_score(analyses, "procedimiento") or 0.0
    
    citas = sum(1 for a in analyses if a.tipo_llamada == "cita")
    total_tipo = sum(1 for a in analyses if a.tipo_llamada is not None)
    cita_rate = round((citas / total_tipo) * 100) if total_tipo > 0 else 0
    total_objs = sum(1 for a in analyses if _has_objections(a.result))
    
    # ── Trend Calculation ─────────────────────────────────────────────────────
    delta_val = 0.0
    delta_pct = 0.0
    direction = "stable"
    interpretation = "Sin datos suficientes para calcular tendencia."
    
    if total_analyses >= 2:
        mid = total_analyses // 2
        first_half = analyses[:mid]
        second_half = analyses[mid:]
        
        first_avg = get_avg_score(first_half, "evaluacion_global") or 0.0
        last_avg = get_avg_score(second_half, "evaluacion_global") or 0.0
        
        delta_val = round(last_avg - first_avg, 1)
        delta_pct = round(((last_avg - first_avg) / first_avg) * 100, 1) if first_avg > 0 else 0.0
        
        if delta_val > 0.3:
            direction = "up"
            interpretation = "El agente muestra una mejoría en la evaluación global en el periodo seleccionado."
        elif delta_val < -0.3:
            direction = "down"
            interpretation = "El agente muestra una caída en la evaluación global en el periodo seleccionado."
        else:
            direction = "stable"
            interpretation = "El desempeño del agente se mantiene estable en la evaluación global."
            
    # ── Timeline Grouping ─────────────────────────────────────────────────────
    bucket_interval = bucket_param if bucket_param in ["day", "week"] else ("week" if period in ["90d", "all"] else "day")
    
    buckets_map = {}
    for a in analyses:
        fe = a.fecha_eval
        if not fe:
            continue
        if fe.tzinfo is None:
            fe = fe.replace(tzinfo=timezone.utc)
            
        if bucket_interval == "day":
            b_key = fe.strftime("%Y-%m-%d")
        else:
            start_of_week = fe - timedelta(days=fe.weekday())
            b_key = start_of_week.strftime("%Y-%m-%d")
            
        if b_key not in buckets_map:
            buckets_map[b_key] = []
        buckets_map[b_key].append(a)
        
    timeline = []
    for b_key in sorted(buckets_map.keys()):
        bucket_analyses = buckets_map[b_key]
        b_total = len(bucket_analyses)
        b_avg_eval = get_avg_score(bucket_analyses, "evaluacion_global")
        b_avg_sent = get_avg_score(bucket_analyses, "sentiment")
        b_avg_emp = get_avg_score(bucket_analyses, "empatia")
        b_avg_cla = get_avg_score(bucket_analyses, "claridad")
        b_avg_sim = get_avg_score(bucket_analyses, "simpatia")
        b_avg_pro = get_avg_score(bucket_analyses, "procedimiento")
        
        b_citas = sum(1 for r in bucket_analyses if r.tipo_llamada == "cita")
        b_total_tipo = sum(1 for r in bucket_analyses if r.tipo_llamada is not None)
        b_cita_rate = round((b_citas / b_total_tipo) * 100) if b_total_tipo > 0 else 0
        b_total_objs = sum(1 for r in bucket_analyses if _has_objections(r.result))
        
        timeline.append({
            "bucket": b_key,
            "total_analyses": b_total,
            "avg_evaluacion_global": b_avg_eval,
            "avg_sentiment": b_avg_sent,
            "avg_empatia": b_avg_emp,
            "avg_claridad": b_avg_cla,
            "avg_simpatia": b_avg_sim,
            "avg_procedimiento": b_avg_pro,
            "cita_rate": b_cita_rate,
            "total_objeciones": b_total_objs
        })
        
    # ── Criteria Evolution ────────────────────────────────────────────────────
    criteria_evolution = []
    if total_analyses >= 2:
        mid = total_analyses // 2
        first_half = analyses[:mid]
        second_half = analyses[mid:]
        
        for key, name in CRITERIA_NAMES.items():
            first_avg = get_avg_score(first_half, key)
            last_avg = get_avg_score(second_half, key)
            if first_avg is not None and last_avg is not None:
                c_delta = round(last_avg - first_avg, 1)
                c_dir = "up" if c_delta > 0.1 else ("down" if c_delta < -0.1 else "stable")
                criteria_evolution.append({
                    "criterion_key": key,
                    "criterion_name": name,
                    "first_avg": first_avg,
                    "last_avg": last_avg,
                    "delta": c_delta,
                    "direction": c_dir
                })
                
    # ── Strengths / Weaknesses ────────────────────────────────────────────────
    criteria_scores = []
    for key, name in CRITERIA_NAMES.items():
        if key in ["evaluacion_global", "sentiment"]:
            continue
        avg_val = get_avg_score(analyses, key)
        if avg_val is not None:
            criteria_scores.append({
                "criterion_key": key,
                "criterion_name": name,
                "avg_score": avg_val
            })
            
    strengths_sorted = sorted(criteria_scores, key=lambda x: x["avg_score"], reverse=True)
    strengths = strengths_sorted[:5]
    
    weaknesses_sorted = sorted(criteria_scores, key=lambda x: x["avg_score"])
    weaknesses = weaknesses_sorted[:5]
    
    # ── Latest 10 Analyses ────────────────────────────────────────────────────
    sorted_desc = sorted(analyses, key=lambda x: x.analysis_id, reverse=True)
    latest_analyses = []
    for a in sorted_desc[:10]:
        objection_text = None
        if a.result and isinstance(a.result, dict):
            objection_text = a.result.get("objeciones") or a.result.get("objecion_1")
            if isinstance(objection_text, list) and objection_text:
                objection_text = str(objection_text[0])
        latest_analyses.append({
            "analysis_id": a.analysis_id,
            "call_id": a.call_id,
            "fecha_eval": a.fecha_eval.isoformat() if a.fecha_eval else None,
            "call_timestamp": a.call_timestamp.isoformat() if a.call_timestamp else None,
            "tipo_llamada": a.tipo_llamada,
            "evaluacion_global": float(a.evaluacion_global) if a.evaluacion_global is not None else None,
            "objeciones": objection_text
        })
        
    return {
        "agent": {
            "hubspot_owner_id": hubspot_owner_id,
            "agent_name": agent_name
        },
        "period": period,
        "analysis_type": analysis_type,
        "generated_at": now.isoformat(),
        "summary": {
            "total_analyses": total_analyses,
            "first_analysis_at": first_analysis_at,
            "last_analysis_at": last_analysis_at,
            "avg_evaluacion_global": avg_eval,
            "avg_sentiment": avg_sent,
            "cita_rate": cita_rate,
            "avg_empatia": avg_emp,
            "avg_claridad": avg_cla,
            "avg_simpatia": avg_sim,
            "avg_procedimiento": avg_pro,
            "total_objeciones": total_objs
        },
        "trend": {
            "evaluacion_global_slope": delta_val, # Simple delta serving as slope representation
            "evaluacion_global_direction": direction,
            "evaluacion_global_delta_first_last": delta_val,
            "evaluacion_global_delta_pct": delta_pct,
            "interpretation": interpretation
        },
        "timeline": timeline,
        "criteria_evolution": criteria_evolution,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "latest_analyses": latest_analyses
    }


# ── C) GET /bm/dashboard/objections ────────────────────────────────────────────
async def get_objections_breakdown(
    db: AsyncSession,
    analysis_type: str = "audio",
    period: str = "7d",
    agent_id: str | None = None,
    tipo_llamada: str | None = None
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    
    if period == "24h":
        delta = timedelta(hours=24)
    elif period == "7d":
        delta = timedelta(days=7)
    elif period == "30d":
        delta = timedelta(days=30)
    elif period == "90d":
        delta = timedelta(days=90)
    else:
        period = "all"
        delta = None
        
    stmt = select(Analysis).where(
        Analysis.analysis_type == analysis_type,
        Analysis.status == "completed"
    )
    if delta:
        stmt = stmt.where(Analysis.fecha_eval >= now - delta)
    if agent_id:
        stmt = stmt.where(Analysis.hubspot_owner_id == agent_id)
    if tipo_llamada:
        stmt = stmt.where(Analysis.tipo_llamada == tipo_llamada)
        
    result = await db.execute(stmt)
    analyses = result.scalars().all()
    
    objection_analyses = [a for a in analyses if _has_objections(a.result)]
    
    category_groups = {}
    total_objection_items = 0
    
    for a in objection_analyses:
        resolved_agent = resolve_agent_display(a.agente_telefonico, a.hubspot_owner_id)
        objection_texts = extract_objection_items(a.result)
        
        if not objection_texts:
            objection_texts = [a.result.get("objeciones") or "Objeción genérica"]
            
        for text in objection_texts:
            if not text or not isinstance(text, str):
                continue
            total_objection_items += 1
            cat_label = categorize_text(text)
            if cat_label not in category_groups:
                category_groups[cat_label] = {
                    "label": cat_label,
                    "count": 0,
                    "call_count": 0,
                    "calls": set(),
                    "examples": []
                }
            
            category_groups[cat_label]["count"] += 1
            if a.call_id not in category_groups[cat_label]["calls"]:
                category_groups[cat_label]["calls"].add(a.call_id)
                category_groups[cat_label]["call_count"] += 1
                
            if len(category_groups[cat_label]["examples"]) < 5:
                category_groups[cat_label]["examples"].append({
                    "analysis_id": a.analysis_id,
                    "call_id": a.call_id,
                    "agent": resolved_agent,
                    "fecha_eval": a.fecha_eval.isoformat() if a.fecha_eval else None,
                    "call_timestamp": a.call_timestamp.isoformat() if a.call_timestamp else None,
                    "text": text
                })
                
    top_objections = []
    for cat_label, g in category_groups.items():
        top_objections.append({
            "label": g["label"],
            "count": g["count"],
            "call_count": g["call_count"],
            "examples": g["examples"]
        })
    top_objections.sort(key=lambda x: x["count"], reverse=True)
    
    # ── By Agent Grouping ─────────────────────────────────────────────────────
    agent_groups = {}
    for a in objection_analyses:
        resolved_agent = resolve_agent_display(a.agente_telefonico, a.hubspot_owner_id)
        oid = a.hubspot_owner_id or "desconocido"
        
        if oid not in agent_groups:
            agent_groups[oid] = {
                "agent": resolved_agent or oid,
                "hubspot_owner_id": oid,
                "total_objections": 0,
                "total_calls_with_objections": 0,
                "calls": set()
            }
            
        objection_texts = extract_objection_items(a.result)
        num_objs = len(objection_texts) if objection_texts else 1
        
        agent_groups[oid]["total_objections"] += num_objs
        if a.call_id not in agent_groups[oid]["calls"]:
            agent_groups[oid]["calls"].add(a.call_id)
            agent_groups[oid]["total_calls_with_objections"] += 1
            
    by_agent = []
    for oid, g in agent_groups.items():
        by_agent.append({
            "agent": g["agent"],
            "hubspot_owner_id": g["hubspot_owner_id"],
            "total_objections": g["total_objections"],
            "total_calls_with_objections": g["total_calls_with_objections"]
        })
    by_agent.sort(key=lambda x: x["total_objections"], reverse=True)
    
    # ── Items Formatting ──────────────────────────────────────────────────────
    items = []
    for a in objection_analyses:
        resolved_agent = resolve_agent_display(a.agente_telefonico, a.hubspot_owner_id)
        objection_texts = extract_objection_items(a.result)
        objection_summary_text = ", ".join(objection_texts) if objection_texts else (a.result.get("objeciones") if a.result else None)
        
        items.append({
            "analysis_id": a.analysis_id,
            "call_id": a.call_id,
            "fecha_eval": a.fecha_eval.isoformat() if a.fecha_eval else None,
            "call_timestamp": a.call_timestamp.isoformat() if a.call_timestamp else None,
            "agent": resolved_agent,
            "tipo_llamada": a.tipo_llamada,
            "objeciones": objection_summary_text,
            "objecion_1": a.result.get("objecion_1") if a.result else None,
            "objecion_2": a.result.get("objecion_2") if a.result else None,
            "objecion_3": a.result.get("objecion_3") if a.result else None,
            "evaluacion_global": float(a.evaluacion_global) if a.evaluacion_global is not None else None
        })
    items.sort(key=lambda x: x["analysis_id"], reverse=True)
    
    return {
        "period": period,
        "analysis_type": analysis_type,
        "total_objection_calls": len(objection_analyses),
        "total_objection_items": total_objection_items,
        "top_objections": top_objections,
        "by_agent": by_agent,
        "items": items
    }
