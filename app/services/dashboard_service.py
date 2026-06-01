"""Dashboard service for calculating real metrics."""

import logging
import decimal
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyses import Analysis
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.models.services import Service
from app.models.typologies import Typology
from app.utils.hubspot_owners import resolve_agent_display, resolve_owner_name, OWNER_TO_NAME

logger = logging.getLogger(__name__)


def parse_date(date_str: str | None) -> datetime | None:
    """Safely parse timezone-aware datetimes or YYYY-MM-DD strings."""
    if not date_str:
        return None
    try:
        # Try ISO format (e.g. 2026-05-27T09:36:56Z)
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        # Try as plain YYYY-MM-DD
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def resolve_date_range(
    date_from: str | None,
    date_to: str | None,
    period: str | None,
    default_period: str = "24h"
) -> tuple[datetime | None, datetime | None, str]:
    """
    Resolve start and end dates based on custom range or period shortcut.
    Returns (start_date, end_date, recommended_bucket_interval).
    """
    now = datetime.now(timezone.utc)
    
    dt_from = parse_date(date_from)
    dt_to = parse_date(date_to)
    
    if dt_from and dt_to:
        if len(date_from) <= 10:
            dt_from = dt_from.replace(hour=0, minute=0, second=0, microsecond=0)
        if len(date_to) <= 10:
            dt_to = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999)
            
        span = dt_to - dt_from
        if span <= timedelta(hours=24):
            bucket_interval = "hour"
        else:
            bucket_interval = "day"
        return dt_from, dt_to, bucket_interval
        
    p = period or default_period
    if p == "7d":
        delta = timedelta(days=7)
        bucket_interval = "day"
    elif p == "30d":
        delta = timedelta(days=30)
        bucket_interval = "day"
    elif p == "90d":
        delta = timedelta(days=90)
        bucket_interval = "day"
    elif p == "24h":
        delta = timedelta(hours=24)
        bucket_interval = "hour"
    elif p == "all":
        # No start constraint for 'all' in SQL, but for comparison and bucket we fall back to 365 days
        return None, now, "week"
    else:
        return resolve_date_range(None, None, default_period)
        
    start_actual = now - delta
    end_actual = now
    return start_actual, end_actual, bucket_interval


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


def to_float(value: Any, default: float = 0.0) -> float:
    """Helper to convert any numerical/decimal/string value to standard float safely."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def extract_criterion_float_value(cr: Any) -> float | None:
    """Safely extract float score or percentage or boolean from a MassEvaluationCriterionResult."""
    if not cr or not getattr(cr, "is_applicable", True):
        return None
    if getattr(cr, "numeric_value", None) is not None:
        return to_float(cr.numeric_value)
    if getattr(cr, "percentage_value", None) is not None:
        return to_float(cr.percentage_value)
    if getattr(cr, "boolean_value", None) is not None:
        return 10.0 if cr.boolean_value else 0.0
    return None


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
        dur_ms = to_float(dur)
        return dur_ms / 1000.0
    except ValueError:
        return None


def _get_duration_sec_mass(r: Any) -> float | None:
    if r.call_duration_seconds is not None:
        return to_float(r.call_duration_seconds)
    if r.hubspot_metadata and isinstance(r.hubspot_metadata, dict):
        dur = r.hubspot_metadata.get("call_duration")
        if dur is not None:
            try:
                return to_float(dur) / 1000.0
            except:
                pass
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
        return to_float(val)
    except (ValueError, TypeError):
        return None


def _round_dt(dt: datetime, interval: str) -> datetime:
    if interval == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    else:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _calc_delta(actual: Any, anterior: Any) -> float:
    act = to_float(actual)
    ant = to_float(anterior)
    if ant == 0.0:
        return 0.0
    return to_float(round(((act - ant) / ant) * 100, 1))


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
                    return to_float(val[skey])
        return to_float(val)
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
            scores.append(to_float(a.evaluacion_global))
        else:
            s = extract_score(a.result, key)
            if s is not None:
                scores.append(to_float(s))
    return to_float(round(sum(scores) / len(scores), 1)) if scores else None



# ── Mass Evaluation Helpers ───────────────────────────────────────────────────

def _effective_ts(row: Any) -> "datetime | None":
    """Returns call_timestamp if set, otherwise analysis_timestamp."""
    ts = getattr(row, "call_timestamp", None) or getattr(row, "analysis_timestamp", None)
    if ts and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def extract_score_from_mass(result_json: Any, items_json: Any, key: str) -> "float | None":
    """Extract numeric score from mass result_json, falling back to items_json."""
    EVALUATIVE_SCORES = {
        "evaluacion_global", "sentiment", "sentimiento", "evaluacion_sentimiento",
        "empatia", "simpatia", "claridad", "procedimiento", "adherencia_procedimiento",
        "saludo_inicio", "n3_preguntas", "despedida_con_refuerzo", 
        "gestion_objeciones", "uso_nombre_paciente", "uso_preguntas", 
        "explicaciones_medicas", "claridad_explicacion_economica",
        "trato_usted", "propension", "siguiente_paso"
    }

    if key not in EVALUATIVE_SCORES:
        return None

    if result_json and isinstance(result_json, dict):
        val = result_json.get(key)
        if val is None and key == "sentiment":
            val = result_json.get("sentimiento") or result_json.get("evaluacion_sentimiento")
        if val is None and key == "procedimiento":
            val = result_json.get("adherencia_procedimiento")
            
        if val is not None:
            if isinstance(val, bool):
                return None
                
            try:
                if isinstance(val, dict):
                    for sk in ["score", "valor", "value", "puntuacion"]:
                        v_sub = val.get(sk)
                        if v_sub is not None and not isinstance(v_sub, bool):
                            if isinstance(v_sub, (int, float, decimal.Decimal)):
                                return to_float(v_sub)
                            elif isinstance(v_sub, str):
                                return float(v_sub)
                else:
                    if isinstance(val, (int, float, decimal.Decimal)):
                        return to_float(val)
                    elif isinstance(val, str):
                        return float(val)
            except (ValueError, TypeError):
                return None
                        
    if items_json:
        items = items_json if isinstance(items_json, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_key = item.get("key") or item.get("criterion_key") or item.get("output_key")
            if item_key == key:
                v = item.get("value") or item.get("score") or item.get("valor")
                if v is not None and not isinstance(v, bool):
                    try:
                        if isinstance(v, (int, float, decimal.Decimal)):
                            return to_float(v)
                        elif isinstance(v, str):
                            return float(v)
                    except (ValueError, TypeError):
                        pass
    return None


def get_avg_score_mass(rows: list, key: str) -> "float | None":
    """Compute average score for a key across MassEvaluationResult rows."""
    scores = [s for r in rows if (s := extract_score_from_mass(r.result_json, r.items_json, key)) is not None]
    return to_float(round(sum(scores) / len(scores), 1)) if scores else None


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


def _get_objection_metrics_mass(rows: list[Any]) -> tuple[int, int]:
    calls = 0
    items = 0
    for r in rows:
        res = getattr(r, "result_json", None) or getattr(r, "result", None)
        if _has_objections(res):
            calls += 1
            texts = extract_objection_items(res)
            items += len(texts) if texts else 1
    return calls, items


# ── Existing dashboard summary ────────────────────────────────────────────────
async def get_dashboard_summary(
    db: AsyncSession,
    analysis_type: str = "audio",
    period: str = "24h",
    service_id: int | None = None,
    service_key: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    
    # Resolve custom range or period
    dt_from = parse_date(date_from)
    dt_to = parse_date(date_to)
    
    if dt_from and dt_to:
        if len(date_from) <= 10:
            start_actual = dt_from.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start_actual = dt_from
        if len(date_to) <= 10:
            end_actual = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            end_actual = dt_to
            
        span = end_actual - start_actual
        start_anterior = start_actual - span
        end_anterior = start_actual
        if span <= timedelta(hours=24):
            bucket_interval = "hour"
        else:
            bucket_interval = "day"
    else:
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

    # Query from MassEvaluationResult exclusively
    stmt = select(MassEvaluationResult).where(
        MassEvaluationResult.status == "completed"
    )
    if start_anterior:
        stmt = stmt.where(
            func.coalesce(
                MassEvaluationResult.call_timestamp,
                MassEvaluationResult.analysis_timestamp,
            ) >= start_anterior
        )
    if end_actual:
        stmt = stmt.where(
            func.coalesce(
                MassEvaluationResult.call_timestamp,
                MassEvaluationResult.analysis_timestamp,
            ) <= end_actual
        )
    if service_id is not None:
        stmt = stmt.where(MassEvaluationResult.service_id == service_id)
    elif service_key is not None:
        stmt = stmt.where(MassEvaluationResult.service_key == service_key)

    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    actual_rows = []
    anterior_rows = []

    for r in rows:
        fe = _effective_ts(r)
        if not fe:
            continue
        
        if start_actual <= fe <= end_actual:
            actual_rows.append(r)
        elif start_anterior <= fe < start_actual:
            anterior_rows.append(r)

    total_analyses = to_float(len(actual_rows))
    evals = []
    for r in actual_rows:
        v = extract_score_from_mass(r.result_json, r.items_json, "evaluacion_global")
        if v is not None:
            evals.append(to_float(v))
    avg_eval = to_float(round(sum(evals) / len(evals), 1)) if evals else 0.0
    
    citas = sum(1 for r in actual_rows if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") == "cita")
    total_tipo = sum(1 for r in actual_rows if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") is not None)
    cita_rate = to_float(round((citas / total_tipo) * 100)) if total_tipo > 0 else 0.0

    durs = [_get_duration_sec_mass(r) for r in actual_rows]
    durs = [d for d in durs if d is not None]
    avg_dur = to_float(round(sum(durs) / len(durs))) if durs else 0.0

    total_objection_calls_raw, total_objection_items_raw = _get_objection_metrics_mass(actual_rows)
    total_objection_calls = to_float(total_objection_calls_raw)
    total_objection_items = to_float(total_objection_items_raw)

    # Anterior period
    total_analyses_ant = to_float(len(anterior_rows))
    evals_ant = []
    for r in anterior_rows:
        v = extract_score_from_mass(r.result_json, r.items_json, "evaluacion_global")
        if v is not None:
            evals_ant.append(to_float(v))
    avg_eval_ant = to_float(sum(evals_ant) / len(evals_ant)) if evals_ant else 0.0
    
    citas_ant = sum(1 for r in anterior_rows if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") == "cita")
    total_tipo_ant = sum(1 for r in anterior_rows if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") is not None)
    cita_rate_ant = to_float((citas_ant / total_tipo_ant) * 100) if total_tipo_ant > 0 else 0.0

    durs_ant = [_get_duration_sec_mass(r) for r in anterior_rows]
    durs_ant = [d for d in durs_ant if d is not None]
    avg_dur_ant = to_float(sum(durs_ant) / len(durs_ant)) if durs_ant else 0.0

    total_objection_calls_ant_raw, total_objection_items_ant_raw = _get_objection_metrics_mass(anterior_rows)
    total_objection_calls_ant = to_float(total_objection_calls_ant_raw)
    total_objection_items_ant = to_float(total_objection_items_ant_raw)

    comparisons = {
        "total_analyses_delta_pct": _calc_delta(total_analyses, total_analyses_ant),
        "pending_delta_pct": 0.0,
        "avg_evaluacion_global_delta_pct": _calc_delta(avg_eval, avg_eval_ant),
        "cita_rate_delta_pct": _calc_delta(cita_rate, cita_rate_ant),
        "avg_duration_delta_pct": _calc_delta(avg_dur, avg_dur_ant),
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
        fe = _effective_ts(r)
        if not fe:
            continue
        b = _round_dt(fe, bucket_interval)
        if b not in grouped_evolution:
            grouped_evolution[b] = {"total": 0, "citas": 0, "sin_cita": 0}
        grouped_evolution[b]["total"] += 1
        tipo = r.result_json.get("tipo_llamada") if r.result_json else None
        if tipo == "cita":
            grouped_evolution[b]["citas"] += 1
        else:
            grouped_evolution[b]["sin_cita"] += 1

    calls_evolution = []
    for b in buckets:
        data = grouped_evolution.get(b, {"total": 0, "citas": 0, "sin_cita": 0})
        calls_evolution.append({
            "bucket": b.isoformat(),
            "total": to_float(data["total"]),
            "citas": to_float(data["citas"]),
            "sin_cita": to_float(data["sin_cita"])
        })

    # ── Dynamic Type Distribution using bm_typologies master catalog ──
    typo_stmt = select(Typology, Service).join(
        Service, Typology.service_id == Service.service_id
    ).where(
        Typology.is_active == True,
        Service.is_active == True
    )
    if service_id is not None:
        typo_stmt = typo_stmt.where(Typology.service_id == service_id)
    elif service_key is not None:
        typo_stmt = typo_stmt.where(Service.service_key == service_key)

    typo_stmt = typo_stmt.order_by(Typology.sort_order.asc(), Typology.typology_name.asc())
    typo_res = await db.execute(typo_stmt)
    typo_rows = typo_res.all()

    master_typologies = []
    for t, s in typo_rows:
        master_typologies.append({
            "typology_id": t.typology_id,
            "typology_key": t.typology_key,
            "typology_name": t.typology_name,
            "service_id": s.service_id,
            "service_key": s.service_key,
            "service_name": s.service_name,
            "sort_order": t.sort_order,
            "total_calls": 0.0,
            "count": 0.0,
            "percentage": 0.0,
            "tipo_llamada": t.typology_key
        })

    typology_counts = {}
    unclassified_count = 0

    for r in actual_rows:
        if r.typology_id is not None:
            typology_counts[r.typology_id] = typology_counts.get(r.typology_id, 0) + 1
        else:
            # Fallback to match by typology_key
            matched = False
            if r.typology_key:
                for mt in master_typologies:
                    if mt["typology_key"] == r.typology_key and (service_id is None or mt["service_id"] == r.service_id):
                        typology_counts[mt["typology_id"]] = typology_counts.get(mt["typology_id"], 0) + 1
                        matched = True
                        break
            if not matched:
                unclassified_count += 1

    total_typology_calls = sum(typology_counts.values()) + unclassified_count

    for mt in master_typologies:
        cnt = typology_counts.get(mt["typology_id"], 0)
        mt["count"] = to_float(cnt)
        mt["total_calls"] = to_float(cnt)
        if total_typology_calls > 0:
            mt["percentage"] = to_float(round((cnt / total_typology_calls) * 100, 1))
        else:
            mt["percentage"] = 0.0

    if unclassified_count > 0:
        pct = to_float(round((unclassified_count / total_typology_calls) * 100, 1)) if total_typology_calls > 0 else 0.0
        master_typologies.append({
            "typology_id": None,
            "typology_key": "unclassified",
            "typology_name": "Sin clasificar",
            "service_id": None,
            "service_key": None,
            "service_name": None,
            "sort_order": 999999,
            "total_calls": to_float(unclassified_count),
            "count": to_float(unclassified_count),
            "percentage": pct,
            "tipo_llamada": "unclassified"
        })

    type_distribution = master_typologies

    sentiment_grouped = {}
    for r in actual_rows:
        fe = _effective_ts(r)
        if not fe:
            continue
        b = _round_dt(fe, bucket_interval)
        sent = _get_sentiment(r.result_json)
        if sent is not None:
            if b not in sentiment_grouped:
                sentiment_grouped[b] = []
            sentiment_grouped[b].append(to_float(sent))

    sentiment_evolution = []
    for b in buckets:
        vals = sentiment_grouped.get(b, [])
        avg_sent = to_float(round(sum(vals) / len(vals), 1)) if vals else 0.0
        sentiment_evolution.append({
            "bucket": b.isoformat(),
            "avg_sentiment": avg_sent
        })

    agent_data = {}
    for r in actual_rows:
        resolved_name = resolve_agent_display(r.agent_name, r.hubspot_owner_id)
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
        v = extract_score_from_mass(r.result_json, r.items_json, "evaluacion_global")
        if v is not None:
            agent_data[resolved_name]["evals"].append(to_float(v))
        
        tipo = r.result_json.get("tipo_llamada") if r.result_json else None
        if tipo is not None:
            agent_data[resolved_name]["total_tipo"] += 1
            if tipo == "cita":
                agent_data[resolved_name]["citas"] += 1

    ranking = []
    for name, data in agent_data.items():
        avg_eval_score = to_float(round(sum(data["evals"]) / len(data["evals"]), 1)) if data["evals"] else 0.0
        cita_rate_score = to_float(round((data["citas"] / data["total_tipo"]) * 100)) if data["total_tipo"] > 0 else 0.0
        ranking.append({
            "agente_telefonico": name,
            "total_analyses": to_float(data["total_analyses"]),
            "avg_evaluacion_global": avg_eval_score,
            "cita_rate": cita_rate_score
        })

    ranking.sort(key=lambda x: (x["total_analyses"], x["avg_evaluacion_global"]), reverse=True)
    agent_ranking = ranking[:5]

    sorted_actual = sorted(actual_rows, key=lambda x: _effective_ts(x) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    latest_analyses = []
    for r in sorted_actual[:8]:
        resolved_agent = resolve_agent_display(r.agent_name, r.hubspot_owner_id)
        eg = extract_score_from_mass(r.result_json, r.items_json, "evaluacion_global")
        tipo = r.result_json.get("tipo_llamada") if r.result_json else None
        latest_analyses.append({
            "analysis_id": r.mass_analysis_id,
            "mass_result_id": r.mass_analysis_id,
            "call_id": r.call_id,
            "agente_telefonico": resolved_agent,
            "tipo_llamada": tipo,
            "evaluacion_global": to_float(eg) if eg is not None else None,
            "fecha_eval": _effective_ts(r).isoformat() if _effective_ts(r) else None,
            "call_timestamp": r.call_timestamp.isoformat() if r.call_timestamp else None,
            "status": r.status,
            "execution_source": r.execution_source
        })

    return {
        "period": period,
        "analysis_type": analysis_type,
        "generated_at": now.isoformat(),
        "kpis": {
            "total_analyses": to_float(total_analyses),
            "pending": 0.0,
            "pending_available": False,
            "avg_evaluacion_global": avg_eval,
            "cita_rate": cita_rate,
            "avg_duration_seconds": avg_dur,
            "total_objeciones": total_objection_items,
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
async def get_agents_list(
    db: AsyncSession,
    service_id: int | None = None,
    service_key: str | None = None
) -> list[dict[str, Any]]:
    """Return agents list with metrics calculated from bm_mass_evaluation_results only."""
    from app.models.mass_evaluations import MassEvaluationResult

    # 1. Per-agent aggregates (count + last timestamp)
    agg_stmt = select(
        MassEvaluationResult.hubspot_owner_id,
        MassEvaluationResult.agent_name,
        func.count(MassEvaluationResult.mass_analysis_id).label("total_analyses"),
        func.max(MassEvaluationResult.analysis_timestamp).label("last_analysis_at"),
    ).where(
        MassEvaluationResult.status == "completed",
        MassEvaluationResult.hubspot_owner_id.is_not(None),
    )
    if service_id is not None:
        agg_stmt = agg_stmt.where(MassEvaluationResult.service_id == service_id)
    elif service_key is not None:
        agg_stmt = agg_stmt.where(MassEvaluationResult.service_key == service_key)
        
    agg_stmt = agg_stmt.group_by(
        MassEvaluationResult.hubspot_owner_id,
        MassEvaluationResult.agent_name,
    )
    agg_res = await db.execute(agg_stmt)
    agg_rows = agg_res.fetchall()

    # Collapse multiple agent_name variants per owner → keep highest count
    db_stats: dict[str, Any] = {}
    for r in agg_rows:
        oid = r.hubspot_owner_id
        if oid not in db_stats or r.total_analyses > db_stats[oid].total_analyses:
            db_stats[oid] = r

    # 2. Fetch result_json to compute avg_evaluacion_global in Python
    rj_stmt = select(
        MassEvaluationResult.hubspot_owner_id,
        MassEvaluationResult.result_json,
    ).where(
        MassEvaluationResult.status == "completed",
        MassEvaluationResult.hubspot_owner_id.is_not(None),
    )
    if service_id is not None:
        rj_stmt = rj_stmt.where(MassEvaluationResult.service_id == service_id)
    elif service_key is not None:
        rj_stmt = rj_stmt.where(MassEvaluationResult.service_key == service_key)
        
    rj_res = await db.execute(rj_stmt)
    owner_evals: dict[str, list[float]] = {}
    for r in rj_res.fetchall():
        rj = r.result_json
        if rj and isinstance(rj, dict):
            v = rj.get("evaluacion_global")
            if v is not None:
                try:
                    owner_evals.setdefault(r.hubspot_owner_id, []).append(to_float(v))
                except (ValueError, TypeError):
                    pass

    def _fmt(stats: Any, oid: str, name: str) -> dict:
        evals = owner_evals.get(oid, [])
        avg_eval = to_float(round(sum(evals) / len(evals), 1)) if evals else 0.0
        last_at = None
        if stats and stats.last_analysis_at:
            raw = stats.last_analysis_at
            if raw.tzinfo is None:
                raw = raw.replace(tzinfo=timezone.utc)
            last_at = raw.isoformat()
        return {
            "hubspot_owner_id": oid,
            "agent_name": name,
            "total_analyses": to_float(stats.total_analyses) if stats else 0.0,
            "last_analysis_at": last_at,
            "avg_evaluacion_global": avg_eval,
        }

    results = []
    # 3. Known mapping first (always shown even with 0 evaluations)
    for oid, name in OWNER_TO_NAME.items():
        results.append(_fmt(db_stats.get(oid), oid, name))

    # 4. Extra agents found only in mass eval results
    for oid in db_stats:
        if oid not in OWNER_TO_NAME:
            row = db_stats[oid]
            disp_name = row.agent_name or oid
            # Exclude unidentified numeric agents from "Todos los agentes"
            if disp_name.startswith("Agente no identificado") or disp_name.isdigit():
                continue
            results.append(_fmt(row, oid, disp_name))

    return results



# ── B) GET /bm/agents/{hubspot_owner_id}/evolution ─────────────────────────────
async def get_agent_evolution(
    db: AsyncSession,
    hubspot_owner_id: str,
    analysis_type: str = "audio",   # kept for API compat; mass evals are always audio
    period: str = "30d",
    bucket_param: str | None = None,
    prompt_version_id: int | None = None,
    service_id: int | None = None,
    service_key: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Evolution metrics from bm_mass_evaluation_results only."""
    from app.models.mass_evaluations import MassEvaluationResult

    now = datetime.now(timezone.utc)

    # 1. Resolve timeframe
    dt_from, dt_to, recommended_bucket = resolve_date_range(date_from, date_to, period, default_period="30d")
    bucket_interval = bucket_param if bucket_param in ["hour", "day", "week"] else recommended_bucket

    stmt = select(MassEvaluationResult).where(
        MassEvaluationResult.hubspot_owner_id == hubspot_owner_id,
        MassEvaluationResult.status == "completed",
    )
    if dt_from:
        stmt = stmt.where(
            func.coalesce(
                MassEvaluationResult.call_timestamp,
                MassEvaluationResult.analysis_timestamp,
            ) >= dt_from
        )
    if dt_to:
        stmt = stmt.where(
            func.coalesce(
                MassEvaluationResult.call_timestamp,
                MassEvaluationResult.analysis_timestamp,
            ) <= dt_to
        )
    if service_id is not None:
        stmt = stmt.where(MassEvaluationResult.service_id == service_id)
    elif service_key is not None:
        stmt = stmt.where(MassEvaluationResult.service_key == service_key)
    if prompt_version_id is not None:
        stmt = stmt.where(MassEvaluationResult.prompt_version_id == prompt_version_id)

    stmt = stmt.order_by(
        func.coalesce(
            MassEvaluationResult.call_timestamp,
            MassEvaluationResult.analysis_timestamp,
        ).asc()
    )

    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    agent_name = resolve_owner_name(hubspot_owner_id)
    if not agent_name and rows:
        for r in reversed(rows):
            if r.agent_name and not r.agent_name.isdigit():
                agent_name = r.agent_name
                break
    if not agent_name:
        agent_name = hubspot_owner_id

    total_analyses = len(rows)
    first_ts = _effective_ts(rows[0]) if rows else None
    last_ts = _effective_ts(rows[-1]) if rows else None

    avg_eval = to_float(get_avg_score_mass(rows, "evaluacion_global"))
    avg_sent = to_float(get_avg_score_mass(rows, "sentiment"))
    avg_emp  = to_float(get_avg_score_mass(rows, "empatia"))
    avg_cla  = to_float(get_avg_score_mass(rows, "claridad"))
    avg_sim  = to_float(get_avg_score_mass(rows, "simpatia"))
    avg_pro  = to_float(get_avg_score_mass(rows, "procedimiento"))

    # tipo_llamada lives inside result_json
    tipo_counts: dict[str, int] = {}
    for r in rows:
        if r.result_json and isinstance(r.result_json, dict):
            t = r.result_json.get("tipo_llamada")
            if t:
                tipo_counts[t] = tipo_counts.get(t, 0) + 1
    total_tipo = sum(tipo_counts.values())
    cita_rate = to_float(round((tipo_counts.get("cita", 0) / total_tipo) * 100)) if total_tipo > 0 else 0.0
    total_objs = to_float(sum(1 for r in rows if _has_objections(r.result_json)))

    # ── Trend ────────────────────────────────────────────────────────────────
    delta_val = 0.0
    delta_pct = 0.0
    direction = "stable"
    interpretation = "Sin datos suficientes para calcular tendencia."

    if total_analyses >= 2:
        mid = total_analyses // 2
        first_avg = to_float(get_avg_score_mass(rows[:mid], "evaluacion_global"))
        last_avg  = to_float(get_avg_score_mass(rows[mid:], "evaluacion_global"))
        delta_val = to_float(round(last_avg - first_avg, 1))
        delta_pct = to_float(round(((last_avg - first_avg) / first_avg) * 100, 1)) if first_avg > 0 else 0.0
        if delta_val > 0.3:
            direction = "up"
            interpretation = "El agente muestra una mejoría en la evaluación global en el periodo seleccionado."
        elif delta_val < -0.3:
            direction = "down"
            interpretation = "El agente muestra una caída en la evaluación global en el periodo seleccionado."
        else:
            direction = "stable"
            interpretation = "El desempeño del agente se mantiene estable en la evaluación global."
    elif total_analyses == 0:
        direction = "no_data"
        interpretation = "Sin evaluaciones masivas disponibles para el periodo seleccionado."

    # ── Timeline ─────────────────────────────────────────────────────────────
    buckets_map: dict[str, list] = {}
    for r in rows:
        ts = _effective_ts(r)
        if not ts:
            continue
        if bucket_interval == "hour":
            b_key = ts.strftime("%Y-%m-%d %H:00")
        elif bucket_interval == "day":
            b_key = ts.strftime("%Y-%m-%d")
        else:
            b_key = (ts - timedelta(days=ts.weekday())).strftime("%Y-%m-%d")
        buckets_map.setdefault(b_key, []).append(r)


    timeline = []
    for b_key in sorted(buckets_map):
        br = buckets_map[b_key]
        b_tipo = sum(1 for r in br if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada"))
        b_citas = sum(1 for r in br if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") == "cita")
        timeline.append({
            "bucket": b_key,
            "total_analyses": to_float(len(br)),
            "avg_evaluacion_global": to_float(get_avg_score_mass(br, "evaluacion_global")),
            "avg_sentiment": to_float(get_avg_score_mass(br, "sentiment")),
            "avg_empatia": to_float(get_avg_score_mass(br, "empatia")),
            "avg_claridad": to_float(get_avg_score_mass(br, "claridad")),
            "avg_simpatia": to_float(get_avg_score_mass(br, "simpatia")),
            "avg_procedimiento": to_float(get_avg_score_mass(br, "procedimiento")),
            "cita_rate": to_float(round((b_citas / b_tipo) * 100)) if b_tipo > 0 else 0.0,
            "total_objeciones": to_float(sum(1 for r in br if _has_objections(r.result_json))),
        })

    # ── Criteria evolution ────────────────────────────────────────────────────
    criteria_evolution = []
    if total_analyses >= 2:
        mid = total_analyses // 2
        for key, name in CRITERIA_NAMES.items():
            fa = get_avg_score_mass(rows[:mid], key)
            la = get_avg_score_mass(rows[mid:], key)
            if fa is not None and la is not None:
                fa_val = to_float(fa)
                la_val = to_float(la)
                cd = to_float(round(la_val - fa_val, 1))
                criteria_evolution.append({
                    "criterion_key": key,
                    "criterion_name": name,
                    "first_avg": fa_val,
                    "last_avg": la_val,
                    "delta": cd,
                    "direction": "up" if cd > 0.1 else ("down" if cd < -0.1 else "stable"),
                })

    # ── Strengths / Weaknesses ────────────────────────────────────────────────
    criteria_scores = []
    for key, name in CRITERIA_NAMES.items():
        if key in ["evaluacion_global", "sentiment"]:
            continue
        av = get_avg_score_mass(rows, key)
        if av is not None:
            criteria_scores.append({"criterion_key": key, "criterion_name": name, "avg_score": to_float(av)})

    strengths  = sorted(criteria_scores, key=lambda x: x["avg_score"], reverse=True)[:5]
    weaknesses = sorted(criteria_scores, key=lambda x: x["avg_score"])[:5]

    # ── Latest 10 analyses ────────────────────────────────────────────────────
    sorted_desc = sorted(rows, key=lambda r: _effective_ts(r) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    latest_analyses = []
    for r in sorted_desc[:10]:
        rj = r.result_json or {}
        eg = rj.get("evaluacion_global")
        try:
            eg = to_float(eg) if eg is not None else None
        except (ValueError, TypeError):
            eg = None
        obj = rj.get("objeciones") or rj.get("objecion_1")
        if isinstance(obj, list) and obj:
            obj = str(obj[0])
        latest_analyses.append({
            "mass_analysis_id": r.mass_analysis_id,
            "run_id": r.run_id,
            "job_id": r.job_id,
            "call_id": r.call_id,
            "agent_name": r.agent_name or agent_name,
            "call_timestamp": r.call_timestamp.isoformat() if r.call_timestamp else None,
            "analysis_timestamp": r.analysis_timestamp.isoformat() if r.analysis_timestamp else None,
            "call_duration_seconds": to_float(r.call_duration_seconds),
            "direction": r.direction,
            "prompt_name": r.prompt_name,
            "prompt_version_name": r.prompt_version_name,
            "status": r.status,
            "tipo_llamada": rj.get("tipo_llamada"),
            "evaluacion_global": eg,
            "objeciones": obj,
            "execution_source": r.execution_source
        })

    return {
        "agent": {"hubspot_owner_id": hubspot_owner_id, "agent_name": agent_name},
        "period": period,
        "source": "mass_evaluations",
        "generated_at": now.isoformat(),
        "summary": {
            "total_analyses": to_float(total_analyses),
            "first_analysis_at": first_ts.isoformat() if first_ts else None,
            "last_analysis_at": last_ts.isoformat() if last_ts else None,
            "avg_evaluacion_global": avg_eval,
            "avg_sentiment": avg_sent,
            "cita_rate": cita_rate,
            "avg_empatia": avg_emp,
            "avg_claridad": avg_cla,
            "avg_simpatia": avg_sim,
            "avg_procedimiento": avg_pro,
            "total_objeciones": total_objs,
        },
        "trend": {
            "evaluacion_global_slope": delta_val,
            "evaluacion_global_direction": direction,
            "evaluacion_global_delta_first_last": delta_val,
            "evaluacion_global_delta_pct": delta_pct,
            "interpretation": interpretation,
        },
        "timeline": timeline,
        "criteria_evolution": criteria_evolution,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "latest_analyses": latest_analyses,
    }


# ── C) GET /bm/dashboard/objections ────────────────────────────────────────────
async def get_objections_breakdown(
    db: AsyncSession,
    analysis_type: str = "audio",
    period: str = "7d",
    agent_id: str | None = None,
    tipo_llamada: str | None = None,
    service_id: int | None = None,
    service_key: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    
    dt_from, dt_to, _ = resolve_date_range(date_from, date_to, period, default_period="7d")
        
    stmt = select(MassEvaluationResult).where(
        MassEvaluationResult.status == "completed"
    )
    if dt_from:
        stmt = stmt.where(
            func.coalesce(
                MassEvaluationResult.call_timestamp,
                MassEvaluationResult.analysis_timestamp,
            ) >= dt_from
        )
    if dt_to:
        stmt = stmt.where(
            func.coalesce(
                MassEvaluationResult.call_timestamp,
                MassEvaluationResult.analysis_timestamp,
            ) <= dt_to
        )
    if service_id is not None:
        stmt = stmt.where(MassEvaluationResult.service_id == service_id)
    elif service_key is not None:
        stmt = stmt.where(MassEvaluationResult.service_key == service_key)
    if agent_id:
        stmt = stmt.where(MassEvaluationResult.hubspot_owner_id == agent_id)
        
    result = await db.execute(stmt)
    analyses = list(result.scalars().all())

    if tipo_llamada:
        analyses = [
            a for a in analyses
            if a.result_json and isinstance(a.result_json, dict) and a.result_json.get("tipo_llamada") == tipo_llamada
        ]
    
    objection_analyses = [a for a in analyses if _has_objections(a.result_json)]
    
    category_groups = {}
    total_objection_items = 0
    
    for a in objection_analyses:
        resolved_agent = resolve_agent_display(a.agent_name, a.hubspot_owner_id)
        objection_texts = extract_objection_items(a.result_json)
        
        if not objection_texts:
            objection_texts = [((a.result_json.get("objeciones") if a.result_json else None) or "Objeción genérica")]
            
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
                    "analysis_id": a.mass_analysis_id,
                    "call_id": a.call_id,
                    "agent": resolved_agent,
                    "fecha_eval": _effective_ts(a).isoformat() if _effective_ts(a) else None,
                    "call_timestamp": a.call_timestamp.isoformat() if a.call_timestamp else None,
                    "text": text
                })
                
    top_objections = []
    for cat_label, g in category_groups.items():
        top_objections.append({
            "label": g["label"],
            "count": to_float(g["count"]),
            "call_count": to_float(g["call_count"]),
            "examples": g["examples"]
        })
    top_objections.sort(key=lambda x: x["count"], reverse=True)
    
    # ── By Agent Grouping ─────────────────────────────────────────────────────
    agent_groups = {}
    for a in objection_analyses:
        resolved_agent = resolve_agent_display(a.agent_name, a.hubspot_owner_id)
        oid = a.hubspot_owner_id or "desconocido"
        
        if oid not in agent_groups:
            agent_groups[oid] = {
                "agent": resolved_agent or oid,
                "hubspot_owner_id": oid,
                "total_objections": 0,
                "total_calls_with_objections": 0,
                "calls": set()
            }
            
        objection_texts = extract_objection_items(a.result_json)
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
            "total_objections": to_float(g["total_objections"]),
            "total_calls_with_objections": to_float(g["total_calls_with_objections"])
        })
    by_agent.sort(key=lambda x: x["total_objections"], reverse=True)
    
    # ── Items Formatting ──────────────────────────────────────────────────────
    items = []
    for a in objection_analyses:
        resolved_agent = resolve_agent_display(a.agent_name, a.hubspot_owner_id)
        objection_texts = extract_objection_items(a.result_json)
        objection_summary_text = ", ".join(objection_texts) if objection_texts else ((a.result_json.get("objeciones") if a.result_json else None) or "")
        
        eg = extract_score_from_mass(a.result_json, a.items_json, "evaluacion_global")

        items.append({
            "analysis_id": a.mass_analysis_id,
            "call_id": a.call_id,
            "fecha_eval": _effective_ts(a).isoformat() if _effective_ts(a) else None,
            "call_timestamp": a.call_timestamp.isoformat() if a.call_timestamp else None,
            "agent": resolved_agent,
            "tipo_llamada": a.result_json.get("tipo_llamada") if a.result_json else None,
            "objeciones": objection_summary_text,
            "objecion_1": a.result_json.get("objecion_1") if a.result_json else None,
            "objecion_2": a.result_json.get("objecion_2") if a.result_json else None,
            "objecion_3": a.result_json.get("objecion_3") if a.result_json else None,
            "evaluacion_global": to_float(eg) if eg is not None else None
        })
    items.sort(key=lambda x: x["analysis_id"], reverse=True)
    
    return {
        "period": period,
        "analysis_type": analysis_type,
        "total_objection_calls": to_float(len(objection_analyses)),
        "total_objection_items": to_float(total_objection_items),
        "top_objections": top_objections,
        "by_agent": by_agent,
        "items": items
    }


async def get_mass_result_detail(db: AsyncSession, identifier: str) -> dict[str, Any] | None:
    """Retrieve full detail of a single MassEvaluationResult by ID or call_id."""
    row = None
    try:
        id_val = int(identifier)
        stmt = select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == id_val)
        res = await db.execute(stmt)
        row = res.scalars().first()
    except ValueError:
        pass
        
    if not row:
        stmt = select(MassEvaluationResult).where(MassEvaluationResult.call_id == identifier).order_by(MassEvaluationResult.mass_analysis_id.desc())
        res = await db.execute(stmt)
        row = res.scalars().first()
        
    if not row:
        return None
        
    rj = row.result_json or {}
    
    def _norm(val: Any) -> Any:
        if isinstance(val, dict):
            return {k: _norm(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [_norm(v) for v in val]
        else:
            return to_float(val) if isinstance(val, decimal.Decimal) else val
            
    normalized_result_json = _norm(rj)
    normalized_items_json = _norm(row.items_json)
    
    eg = extract_score_from_mass(rj, row.items_json, "evaluacion_global")
    
    # 1. agent_name
    agent_name = resolve_agent_display(row.agent_name, row.hubspot_owner_id) or row.agent_name or None
    
    # 2. call_type
    call_type = rj.get("tipo_llamada") or rj.get("call_type") or None
    
    # 3. analysis_timestamp
    analysis_timestamp = row.analysis_timestamp.isoformat() if row.analysis_timestamp else None
    
    # 4. resumen
    resumen = rj.get("resumen") or rj.get("resumen_llamada") or rj.get("summary") or None
    
    # 5. objeciones
    objection_texts = extract_objection_items(rj)
    objeciones = objection_texts if objection_texts else None
    
    # 6. cita_resultado
    cita_resultado = rj.get("cierre_cita") or rj.get("cita_resultado") or rj.get("cita") or rj.get("cierre") or None
    
    # 7. individual_results_normalized
    individual_results_normalized = []
    if row.items_json:
        for item in row.items_json:
            criterio = item.get("name") or item.get("criterion_key") or "Desconocido"
            val = item.get("value")
            
            # score mapping: only if numeric
            score = None
            if val is not None and not isinstance(val, bool) and isinstance(val, (int, float, decimal.Decimal)):
                score = to_float(val)
                
            # resultado mapping
            resultado = None
            if val is not None:
                if isinstance(val, bool):
                    resultado = "Sí" if val else "No"
                else:
                    resultado = str(val)
                    
            comentario = item.get("feed") or item.get("comment") or None
            
            individual_results_normalized.append({
                "criterio": criterio,
                "score": score,
                "resultado": resultado,
                "comentario": comentario
            })
            
    transcript = rj.get("transcripción") or rj.get("transcripcion") or rj.get("transcript")
    if not transcript and row.hubspot_metadata:
        transcript = row.hubspot_metadata.get("transcript") or row.hubspot_metadata.get("transcription")
    
    return {
        "id": row.mass_analysis_id,
        "mass_result_id": row.mass_analysis_id,
        "call_id": row.call_id,
        "agent_name": agent_name,
        "hubspot_owner_id": row.hubspot_owner_id,
        "call_timestamp": row.call_timestamp.isoformat() if row.call_timestamp else None,
        "analysis_timestamp": analysis_timestamp,
        "status": row.status,
        "duration_seconds": to_float(row.call_duration_seconds),
        "call_type": call_type,
        "evaluacion_global": to_float(eg) if eg is not None else None,
        "resultado_json": normalized_result_json,
        "individual_results": normalized_items_json,
        "objeciones": objeciones,
        "resumen": resumen,
        "cita_resultado": cita_resultado,
        "individual_results_normalized": individual_results_normalized,
        "transcript": transcript,
        "batch_id": row.job_id,
        "mass_evaluation_id": row.job_id,
        "run_id": row.run_id,
        "recording_url": row.recording_url,
        "execution_source": row.execution_source
    }


async def get_agents_comparison(
    db: AsyncSession,
    hubspot_owner_ids: list[str] | None = None,
    service_id: int | None = None,
    service_key: str | None = None,
    typology_id: int | None = None,
    typology_key: str | None = None,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    bucket: str | None = None,
    metric_key: str | None = None,
) -> dict[str, Any]:
    """Retrieve multi-agent comparison analytics using MassEvaluationResult."""
    now = datetime.now(timezone.utc)
    
    if not metric_key:
        metric_key = "evaluacion_global"
        
    # 1. Resolve date range
    dt_from = parse_date(date_from)
    dt_to = parse_date(date_to)
    
    if dt_from and dt_to:
        if date_from and len(date_from) <= 10:
            start_actual = dt_from.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start_actual = dt_from
            
        if date_to and len(date_to) <= 10:
            end_actual = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            end_actual = dt_to
            
        span = end_actual - start_actual
        start_anterior = start_actual - span
        end_anterior = start_actual
        
        if not bucket:
            if span <= timedelta(hours=24):
                bucket_interval = "hour"
            elif span <= timedelta(days=7):
                bucket_interval = "day"
            elif span <= timedelta(days=30):
                bucket_interval = "day"
            else:
                bucket_interval = "week"
        else:
            bucket_interval = bucket
    else:
        p = period or "30d"
        if p == "24h":
            delta = timedelta(hours=24)
            default_bucket = "hour"
        elif p == "7d":
            delta = timedelta(days=7)
            default_bucket = "day"
        elif p == "30d":
            delta = timedelta(days=30)
            default_bucket = "day"
        elif p == "90d":
            delta = timedelta(days=90)
            default_bucket = "day"
        elif p == "all":
            delta = timedelta(days=365)
            default_bucket = "week"
        else:
            delta = timedelta(days=30)
            default_bucket = "day"
            
        start_actual = now - delta
        end_actual = now
        start_anterior = now - (delta * 2)
        end_anterior = now - delta
        bucket_interval = bucket or default_bucket

    # 2. Query completed mass evaluation results
    stmt = select(MassEvaluationResult).where(
        MassEvaluationResult.status == "completed"
    )
    if start_anterior:
        stmt = stmt.where(MassEvaluationResult.call_timestamp >= start_anterior)
    if end_actual:
        stmt = stmt.where(MassEvaluationResult.call_timestamp <= end_actual)
        
    if service_id is not None:
        stmt = stmt.where(MassEvaluationResult.service_id == service_id)
    elif service_key is not None:
        stmt = stmt.where(MassEvaluationResult.service_key == service_key)
        
    if typology_id is not None:
        stmt = stmt.where(MassEvaluationResult.typology_id == typology_id)
    elif typology_key is not None:
        stmt = stmt.where(MassEvaluationResult.typology_key == typology_key)
        
    if hubspot_owner_ids:
        stmt = stmt.where(MassEvaluationResult.hubspot_owner_id.in_(hubspot_owner_ids))
        
    # Order by call_timestamp ascending for chronological aggregations
    stmt = stmt.order_by(MassEvaluationResult.call_timestamp.asc())
    
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    
    # 3. Categorize rows
    actual_rows = []
    anterior_rows = []
    
    for r in rows:
        ts = r.call_timestamp
        if not ts:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
            
        if start_actual <= ts <= end_actual:
            actual_rows.append(r)
        elif start_anterior <= ts < start_actual:
            anterior_rows.append(r)

    # 4. Fetch available metrics catalog
    stmt_metrics = select(
        MassEvaluationCriterionResult.criterion_key,
        MassEvaluationCriterionResult.criterion_name,
        MassEvaluationCriterionResult.criterion_type
    ).where(
        MassEvaluationCriterionResult.criterion_type.in_(["score_1_10", "number", "percentage", "boolean", "score"])
    )
    metrics_res = await db.execute(stmt_metrics)
    raw_metrics = metrics_res.all()
    
    seen_keys = set()
    dynamic_metrics = []
    for r_met in raw_metrics:
        c_key, c_name, c_type = r_met
        if not c_key or c_key in seen_keys:
            continue
        seen_keys.add(c_key)
        if c_key in ["evaluacion_global", "total_calls", "cierre_cita_rate"]:
            continue
            
        val_type = "score"
        if c_type == "percentage":
            val_type = "percentage"
        elif c_type == "boolean":
            val_type = "boolean"
            
        dynamic_metrics.append({
            "metric_key": c_key,
            "label": c_name or c_key.replace("_", " ").capitalize(),
            "type": "criterion",
            "criterion_key": c_key,
            "output_key": c_key,
            "value_type": val_type
        })
        
    # Standard fallback criteria list to ensure UI population even with clean DB
    default_criteria = {
        "empatia": {"label": "Empatía", "type": "score"},
        "claridad": {"label": "Claridad", "type": "score"},
        "procedimiento": {"label": "Procedimiento", "type": "score"},
        "saludo_inicio": {"label": "Saludo de Inicio", "type": "score"},
        "n3_preguntas": {"label": "N3 Preguntas", "type": "score"},
        "despedida_con_refuerzo": {"label": "Despedida con Refuerzo", "type": "score"},
        "gestion_objeciones": {"label": "Gestión de Obeciones", "type": "score"},
        "uso_nombre_paciente": {"label": "Uso del Nombre del Paciente", "type": "score"},
        "uso_preguntas": {"label": "Uso de Preguntas", "type": "score"},
        "explicaciones_medicas": {"label": "Explicaciones Médicas", "type": "score"},
        "claridad_explicacion_economica": {"label": "Claridad Explicación Económica", "type": "score"}
    }
    for c_key, c_info in default_criteria.items():
        if c_key not in seen_keys:
            seen_keys.add(c_key)
            dynamic_metrics.append({
                "metric_key": c_key,
                "label": c_info["label"],
                "type": "criterion",
                "criterion_key": c_key,
                "output_key": c_key,
                "value_type": c_info["type"]
            })
            
    fixed_metrics = [
        {
            "metric_key": "evaluacion_global",
            "label": "Evaluación global",
            "type": "fixed",
            "value_type": "score"
        },
        {
            "metric_key": "total_calls",
            "label": "Volumen llamadas",
            "type": "fixed",
            "value_type": "count"
        },
        {
            "metric_key": "cierre_cita_rate",
            "label": "Cierre de cita",
            "type": "fixed",
            "value_type": "percentage"
        }
    ]
    available_metrics = fixed_metrics + dynamic_metrics
    
    # Resolve metric label
    metric_label = metric_key.replace("_", " ").capitalize()
    if metric_key == "evaluacion_global":
        metric_label = "Evaluación global"
    elif metric_key == "total_calls":
        metric_label = "Volumen llamadas"
    elif metric_key == "cierre_cita_rate":
        metric_label = "Cierre de cita"
    else:
        if metric_key in CRITERIA_NAMES:
            metric_label = CRITERIA_NAMES[metric_key]
        else:
            for dm in dynamic_metrics:
                if dm["metric_key"] == metric_key:
                    metric_label = dm["label"]
                    break
                    
    # 5. Query matching dynamic criterion results if necessary
    is_fixed = metric_key in ["evaluacion_global", "total_calls", "cierre_cita_rate"]
    criterion_rows = []
    
    if not is_fixed:
        stmt_crit = select(
            MassEvaluationCriterionResult,
            MassEvaluationResult.call_timestamp,
            MassEvaluationResult.hubspot_owner_id
        ).join(
            MassEvaluationResult,
            MassEvaluationCriterionResult.mass_analysis_id == MassEvaluationResult.mass_analysis_id
        ).where(
            MassEvaluationResult.status == "completed",
            MassEvaluationCriterionResult.criterion_key == metric_key,
            MassEvaluationCriterionResult.is_applicable == True
        )
        if start_anterior:
            stmt_crit = stmt_crit.where(MassEvaluationResult.call_timestamp >= start_anterior)
        if end_actual:
            stmt_crit = stmt_crit.where(MassEvaluationResult.call_timestamp <= end_actual)
            
        if service_id is not None:
            stmt_crit = stmt_crit.where(MassEvaluationResult.service_id == service_id)
        elif service_key is not None:
            stmt_crit = stmt_crit.where(MassEvaluationResult.service_key == service_key)
            
        if typology_id is not None:
            stmt_crit = stmt_crit.where(MassEvaluationResult.typology_id == typology_id)
        elif typology_key is not None:
            stmt_crit = stmt_crit.where(MassEvaluationResult.typology_key == typology_key)
            
        if hubspot_owner_ids:
            stmt_crit = stmt_crit.where(MassEvaluationResult.hubspot_owner_id.in_(hubspot_owner_ids))
            
        res_crit = await db.execute(stmt_crit)
        criterion_rows = list(res_crit.all())

    # 6. Determine agents to return
    # Find all agents with activity in current period
    active_owner_ids = set()
    for r in actual_rows:
        if r.hubspot_owner_id:
            active_owner_ids.add(r.hubspot_owner_id)
            
    # Include all selected owner IDs if provided
    target_owner_ids = set()
    if hubspot_owner_ids:
        target_owner_ids.update(hubspot_owner_ids)
    else:
        target_owner_ids.update(active_owner_ids)
        target_owner_ids.update(OWNER_TO_NAME.keys())
        
    # Map owner IDs to display names & initials
    agents_map = {}
    for oid in target_owner_ids:
        disp_name = resolve_owner_name(oid)
        if not disp_name:
            for r in rows:
                if r.hubspot_owner_id == oid and r.agent_name and not r.agent_name.isdigit():
                    disp_name = r.agent_name
                    break
        if not disp_name:
            disp_name = f"Agente ({oid})"
            
        parts = disp_name.strip().split()
        if len(parts) >= 2:
            initials = (parts[0][0] + parts[1][0]).upper()
        elif len(parts) == 1:
            initials = parts[0][:2].upper()
        else:
            initials = "??"
            
        agents_map[oid] = {
            "name": disp_name,
            "initials": initials
        }
        
    # 7. Master typologies for 0-call population if filtering by service
    typo_stmt = select(Typology, Service).join(
        Service, Typology.service_id == Service.service_id
    ).where(
        Typology.is_active == True,
        Service.is_active == True
    )
    if service_id is not None:
        typo_stmt = typo_stmt.where(Typology.service_id == service_id)
    elif service_key is not None:
        typo_stmt = typo_stmt.where(Service.service_key == service_key)
        
    typo_stmt = typo_stmt.order_by(Typology.sort_order.asc(), Typology.typology_name.asc())
    typo_res = await db.execute(typo_stmt)
    typo_rows = typo_res.all()
    
    master_typos = []
    for t, s in typo_rows:
        master_typos.append({
            "typology_key": t.typology_key,
            "typology_name": t.typology_name
        })

    # 8. Generate time buckets
    buckets = []
    if bucket_interval == "hour":
        curr = start_actual.replace(minute=0, second=0, microsecond=0)
        while curr <= end_actual:
            buckets.append(curr)
            curr += timedelta(hours=1)
    elif bucket_interval == "week":
        curr = start_actual.replace(hour=0, minute=0, second=0, microsecond=0)
        curr = curr - timedelta(days=curr.weekday())
        while curr <= end_actual:
            buckets.append(curr)
            curr += timedelta(days=7)
    else: # day
        curr = start_actual.replace(hour=0, minute=0, second=0, microsecond=0)
        while curr <= end_actual:
            buckets.append(curr)
            curr += timedelta(days=1)

    # 9. Aggregate per-agent metrics, series, typologies, and criteria summaries
    agents_list = []
    series_list = []
    typologies_list = []
    criteria_list = []
    
    for oid, info in agents_map.items():
        agent_actual_rows = [r for r in actual_rows if r.hubspot_owner_id == oid]
        agent_anterior_rows = [r for r in anterior_rows if r.hubspot_owner_id == oid]
        
        total_calls = len(agent_actual_rows)
        completed_calls = total_calls
        
        avg_eval = get_avg_score_mass(agent_actual_rows, "evaluacion_global")
        avg_cla = get_avg_score_mass(agent_actual_rows, "claridad")
        avg_emp = get_avg_score_mass(agent_actual_rows, "empatia")
        avg_pro = get_avg_score_mass(agent_actual_rows, "procedimiento")
        
        citas_count = sum(1 for r in agent_actual_rows if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") == "cita")
        cierre_cita_rate = round(citas_count / total_calls, 2) if total_calls > 0 else 0.0
        
        # Main typology & distribution
        agent_typo_counts = {}
        agent_typo_names = {}
        
        if service_id is not None or service_key is not None:
            for mt in master_typos:
                agent_typo_counts[mt["typology_key"]] = 0
                agent_typo_names[mt["typology_key"]] = mt["typology_name"]
                
        unclassified_count = 0
        for r in agent_actual_rows:
            tk = r.typology_key or (r.result_json.get("tipo_llamada") if r.result_json else None)
            tn = r.typology_name or (r.result_json.get("tipo_llamada").capitalize() if (r.result_json and r.result_json.get("tipo_llamada")) else None)
            
            if not tk:
                unclassified_count += 1
                continue
                
            agent_typo_counts[tk] = agent_typo_counts.get(tk, 0) + 1
            if tn:
                agent_typo_names[tk] = tn
            elif tk not in agent_typo_names:
                agent_typo_names[tk] = tk.capitalize()
                
        main_typology = None
        if agent_typo_counts:
            active_typos = {k: v for k, v in agent_typo_counts.items() if v > 0}
            if active_typos:
                best_tk = max(active_typos, key=active_typos.get)
                main_typology = agent_typo_names.get(best_tk, best_tk.capitalize())
                
        agent_total_typos = sum(agent_typo_counts.values()) + unclassified_count
        
        typos_dist = []
        for tk, count in agent_typo_counts.items():
            pct = round((count / agent_total_typos) * 100, 1) if agent_total_typos > 0 else 0.0
            typos_dist.append({
                "typology_key": tk,
                "typology_name": agent_typo_names.get(tk, tk.capitalize()),
                "total_calls": count,
                "percentage": pct
            })
            
        if unclassified_count > 0:
            pct = round((unclassified_count / agent_total_typos) * 100, 1) if agent_total_typos > 0 else 0.0
            typos_dist.append({
                "typology_key": "unclassified",
                "typology_name": "Sin clasificar",
                "total_calls": unclassified_count,
                "percentage": pct
            })
            
        typos_dist.sort(key=lambda x: x["total_calls"], reverse=True)
        typologies_list.append({
            "hubspot_owner_id": oid,
            "agent_initials": info["initials"],
            "typologies": typos_dist
        })
        
        # Averages in previous period for Delta calculation
        total_calls_ant = len(agent_anterior_rows)
        avg_eval_ant = get_avg_score_mass(agent_anterior_rows, "evaluacion_global")
        citas_count_ant = sum(1 for r in agent_anterior_rows if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") == "cita")
        cierre_cita_rate_ant = round(citas_count_ant / total_calls_ant, 2) if total_calls_ant > 0 else 0.0
        
        # Delta calculations
        delta_eval = round(avg_eval - avg_eval_ant, 2) if (avg_eval is not None and avg_eval_ant is not None) else (round(avg_eval, 2) if avg_eval is not None else 0.0)
        delta_calls = total_calls - total_calls_ant
        delta_cierre = round(cierre_cita_rate - cierre_cita_rate_ant, 2)
        
        delta_vs_previous_period = {
            "avg_evaluacion_global": delta_eval,
            "total_calls": delta_calls,
            "cierre_cita_rate": delta_cierre
        }
        
        # Dynamic comparison metric values
        if is_fixed:
            if metric_key == "evaluacion_global":
                selected_metric_avg = avg_eval
                selected_metric_count = total_calls
                selected_metric_delta = delta_eval
            elif metric_key == "total_calls":
                selected_metric_avg = float(total_calls)
                selected_metric_count = total_calls
                selected_metric_delta = float(delta_calls)
            elif metric_key == "cierre_cita_rate":
                selected_metric_avg = cierre_cita_rate
                selected_metric_count = total_calls
                selected_metric_delta = delta_cierre
            else:
                selected_metric_avg = None
                selected_metric_count = 0
                selected_metric_delta = 0.0
        else:
            agent_actual_crits = []
            agent_anterior_crits = []
            for cr, ts, r_oid in criterion_rows:
                if r_oid != oid:
                    continue
                if not ts:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if start_actual <= ts <= end_actual:
                    agent_actual_crits.append(cr)
                elif start_anterior <= ts < start_actual:
                    agent_anterior_crits.append(cr)
                    
            actual_scores = [v for cr in agent_actual_crits if (v := extract_criterion_float_value(cr)) is not None]
            anterior_scores = [v for cr in agent_anterior_crits if (v := extract_criterion_float_value(cr)) is not None]
            
            selected_metric_avg = round(sum(actual_scores) / len(actual_scores), 2) if actual_scores else None
            selected_metric_count = len(actual_scores)
            selected_metric_avg_ant = round(sum(anterior_scores) / len(anterior_scores), 2) if anterior_scores else None
            
            if selected_metric_avg is not None and selected_metric_avg_ant is not None:
                selected_metric_delta = round(selected_metric_avg - selected_metric_avg_ant, 2)
            elif selected_metric_avg is not None:
                selected_metric_delta = round(selected_metric_avg, 2)
            else:
                selected_metric_delta = 0.0

        # Append Agent Metrics
        agents_list.append({
            "hubspot_owner_id": oid,
            "agent_initials": info["initials"],
            "agent_name": info["name"],
            "total_calls": total_calls,
            "completed_calls": completed_calls,
            "avg_evaluacion_global": avg_eval,
            "avg_claridad": avg_cla,
            "avg_empatia": avg_emp,
            "avg_procedimiento": avg_pro,
            "cierre_cita_rate": cierre_cita_rate,
            "main_typology": main_typology,
            "delta_vs_previous_period": delta_vs_previous_period,
            
            # Selected Metric Summary
            "selected_metric_key": metric_key,
            "selected_metric_label": metric_label,
            "selected_metric_avg": selected_metric_avg,
            "selected_metric_count": selected_metric_count,
            "selected_metric_delta_vs_previous_period": selected_metric_delta
        })
        
        # Time Series Points
        points = []
        for b_dt in buckets:
            if bucket_interval == "hour":
                b_key = b_dt.strftime("%Y-%m-%d %H:00")
            elif bucket_interval == "day":
                b_key = b_dt.strftime("%Y-%m-%d")
            else:
                b_key = b_dt.strftime("%Y-%m-%d")
                
            b_rows = []
            for r in agent_actual_rows:
                ts = r.call_timestamp
                if not ts:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                    
                if bucket_interval == "hour":
                    row_key = ts.strftime("%Y-%m-%d %H:00")
                elif bucket_interval == "day":
                    row_key = ts.strftime("%Y-%m-%d")
                else:
                    row_key = (ts - timedelta(days=ts.weekday())).strftime("%Y-%m-%d")
                    
                if row_key == b_key:
                    b_rows.append(r)
                    
            if b_rows:
                b_total = len(b_rows)
                b_citas = sum(1 for r in b_rows if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") == "cita")
                b_avg_eval = get_avg_score_mass(b_rows, "evaluacion_global")
                b_avg_emp = get_avg_score_mass(b_rows, "empatia")
                b_avg_cla = get_avg_score_mass(b_rows, "claridad")
                b_avg_pro = get_avg_score_mass(b_rows, "procedimiento")
                b_rate = round(b_citas / b_total, 2)
            else:
                b_total = 0
                b_avg_eval = None
                b_avg_emp = None
                b_avg_cla = None
                b_avg_pro = None
                b_rate = 0.0
                
            # Compute bucket dynamic/fixed metric value
            if is_fixed:
                if metric_key == "evaluacion_global":
                    b_selected_value = b_avg_eval
                elif metric_key == "total_calls":
                    b_selected_value = float(b_total)
                elif metric_key == "cierre_cita_rate":
                    b_selected_value = b_rate
                else:
                    b_selected_value = None
            else:
                b_crits = []
                for cr, ts, r_oid in criterion_rows:
                    if r_oid != oid:
                        continue
                    if not ts:
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                        
                    if bucket_interval == "hour":
                        row_key = ts.strftime("%Y-%m-%d %H:00")
                    elif bucket_interval == "day":
                        row_key = ts.strftime("%Y-%m-%d")
                    else:
                        row_key = (ts - timedelta(days=ts.weekday())).strftime("%Y-%m-%d")
                        
                    if row_key == b_key:
                        b_crits.append(cr)
                        
                b_scores = [v for cr in b_crits if (v := extract_criterion_float_value(cr)) is not None]
                b_selected_value = round(sum(b_scores) / len(b_scores), 2) if b_scores else None
                
            points.append({
                "bucket": b_key,
                "total_calls": b_total,
                "avg_evaluacion_global": b_avg_eval,
                "avg_empatia": b_avg_emp,
                "avg_claridad": b_avg_cla,
                "avg_procedimiento": b_avg_pro,
                "cierre_cita_rate": b_rate,
                
                # Dynamic Series Point Fields
                "selected_metric_key": metric_key,
                "selected_metric_label": metric_label,
                "selected_metric_value": b_selected_value
            })
            
        series_list.append({
            "hubspot_owner_id": oid,
            "agent_initials": info["initials"],
            "points": points
        })
        
        # Criteria Summary
        agent_criteria = []
        for c_key, c_name in CRITERIA_NAMES.items():
            scores = []
            for r in agent_actual_rows:
                s = extract_score_from_mass(r.result_json, r.items_json, c_key)
                if s is not None:
                    scores.append(to_float(s))
            if scores:
                avg_score = round(sum(scores) / len(scores), 1)
                count = len(scores)
            else:
                avg_score = None
                count = 0
            agent_criteria.append({
                "criterion_key": c_key,
                "criterion_name": c_name,
                "avg_score": avg_score,
                "count": count
            })
            
        criteria_list.append({
            "hubspot_owner_id": oid,
            "agent_initials": info["initials"],
            "criteria": agent_criteria
        })
        
    # 10. Compile Global KPI Summary
    active_agents = [a for a in agents_list if a["total_calls"] > 0]
    total_calls_global = sum(a["total_calls"] for a in agents_list)
    
    best_avg_agent = {}
    best_imp_agent = {}
    highest_vol_agent = {}
    
    if active_agents:
        with_avg = [a for a in active_agents if a["avg_evaluacion_global"] is not None]
        if with_avg:
            best_avg = max(with_avg, key=lambda x: x["avg_evaluacion_global"])
            best_avg_agent = {
                "hubspot_owner_id": best_avg["hubspot_owner_id"],
                "agent_initials": best_avg["agent_initials"],
                "avg_evaluacion_global": best_avg["avg_evaluacion_global"]
            }
            
        with_imp = [a for a in active_agents if a["delta_vs_previous_period"].get("avg_evaluacion_global") is not None]
        if with_imp:
            best_imp = max(with_imp, key=lambda x: x["delta_vs_previous_period"]["avg_evaluacion_global"])
            best_imp_agent = {
                "hubspot_owner_id": best_imp["hubspot_owner_id"],
                "agent_initials": best_imp["agent_initials"],
                "delta_avg_evaluacion_global": best_imp["delta_vs_previous_period"]["avg_evaluacion_global"]
            }
            
        highest_vol = max(active_agents, key=lambda x: x["total_calls"])
        highest_vol_agent = {
            "hubspot_owner_id": highest_vol["hubspot_owner_id"],
            "agent_initials": highest_vol["agent_initials"],
            "total_calls": highest_vol["total_calls"]
        }
        
    summary = {
        "agents_count": len(active_agents),
        "total_calls": total_calls_global,
        "best_agent_by_avg": best_avg_agent,
        "best_agent_by_improvement": best_imp_agent,
        "highest_volume_agent": highest_vol_agent
    }
    
    # Sort agents list by total_calls desc, then avg_evaluacion_global desc
    agents_list.sort(key=lambda x: (x["total_calls"], x["avg_evaluacion_global"] or 0.0), reverse=True)
    
    filters = {
        "hubspot_owner_ids": hubspot_owner_ids,
        "service_id": service_id,
        "service_key": service_key,
        "typology_key": typology_key,
        "period": period,
        "date_from": start_actual.isoformat() if start_actual else None,
        "date_to": end_actual.isoformat() if end_actual else None,
        "bucket": bucket_interval
    }
    
    return {
        "filters": filters,
        "summary": summary,
        "agents": agents_list,
        "series": series_list,
        "typology_distribution_by_agent": typologies_list,
        "criteria_summary_by_agent": criteria_list,
        "available_metrics": available_metrics
    }

