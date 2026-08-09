"""API Router for Analytics v2."""
import logging
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, text, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_tenant_context
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.models.analyses import Analysis, AnalysisCriterionResult
from app.schemas.analytics import (
    AnalyticsItem,
    AgentInfo,
    AgentComparisonRow,
    AgentComparisonResponse,
    EvolutionPoint,
    ItemEvolutionSeries,
)
from app.services.dashboard_service import resolve_date_range, extract_score_from_mass
from app.utils.hubspot_owners import resolve_owner_name
from app.utils.normalizers import normalize_typology, normalize_direction
from app.utils.cache import analytics_cache
from app.utils.service_resolvers import resolve_service_id

def _format_int_list(lst) -> str:
    if not lst:
        return "(-1)"
    return f"({','.join(str(int(x)) for x in lst)})"

def _format_str_list(lst) -> str:
    if not lst:
        return "('-1')"
    safe_vals = []
    for x in lst:
        clean = str(x).replace("'", "''")
        safe_vals.append(f"'{clean}'")
    return f"({','.join(safe_vals)})"

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Analytics V2"])

BASE_METRICS = [
    {"key": "evaluacion_global", "label": "Evaluación global", "type": "score", "order": 10, "default_selected": True},
    {"key": "empatia", "label": "Empatía", "type": "score", "order": 20, "default_selected": True},
    {"key": "claridad", "label": "Claridad", "type": "score", "order": 30, "default_selected": True},
    {"key": "simpatia", "label": "Simpatía", "type": "score", "order": 40, "default_selected": True},
    {"key": "procedimiento", "label": "Procedimiento", "type": "score", "order": 50, "default_selected": True},
    {"key": "cierre_cita", "label": "Cierre de cita", "type": "percentage", "order": 60, "default_selected": False},
]

KNOWN_CRITERIA_FALLBACK = [
    {"key": "saludo_inicio", "label": "Saludo de Inicio", "type": "score"},
    {"key": "n3_preguntas", "label": "N3 Preguntas", "type": "score"},
    {"key": "uso_preguntas", "label": "Uso de Preguntas", "type": "score"},
    {"key": "despedida_refuerzo", "label": "Despedida con Refuerzo", "type": "score"},
    {"key": "gestion_objeciones", "label": "Gestión de Objeciones", "type": "score"},
    {"key": "uso_nombre_paciente", "label": "Uso del Nombre del Paciente", "type": "score"},
    {"key": "explicaciones_medicas", "label": "Explicaciones Médicas", "type": "score"},
    {"key": "claridad_explicacion_economica", "label": "Claridad Explicación Económica", "type": "score"},
]

KNOWN_LABELS = {
    "evaluacion_global": "Evaluación global",
    "empatia": "Empatía",
    "claridad": "Claridad",
    "simpatia": "Simpatía",
    "procedimiento": "Procedimiento",
    "cierre_cita": "Cierre de cita",
    "saludo_inicio": "Saludo de Inicio",
    "n3_preguntas": "N3 Preguntas",
    "despedida_refuerzo": "Despedida con Refuerzo",
    "gestion_objeciones": "Gestión de Objeciones",
    "uso_nombre_paciente": "Uso del Nombre del Paciente",
    "uso_preguntas": "Uso de Preguntas",
    "explicaciones_medicas": "Explicaciones Médicas",
    "claridad_explicacion_economica": "Claridad Explicación Económica",
}

def normalize_key(raw_key: str) -> str:
    if not raw_key:
        return ""
    # Normalize unicode to decompose accents/tildes
    s = unicodedata.normalize("NFKD", raw_key).encode("ascii", "ignore").decode("utf-8")
    s = s.lower().strip().replace(" ", "_").replace("-", "_")
    s = "".join([c for c in s if c.isalnum() or c == "_"])
    
    # Standard stable key mappings to resolve aliases
    special_mappings = {
        "despedida_con_refuerzo": "despedida_refuerzo",
        "global_score": "evaluacion_global",
        "puntuacion_global": "evaluacion_global",
        "tono_simpatia": "simpatia",
        "prueba_simpatia": "simpatia",
        "adherencia_procedimiento": "procedimiento",
        "cita_resultado": "cierre_cita",
        "cita": "cierre_cita",
        "cierre": "cierre_cita",
        "reformulacion_patologia": "reformula_patologia",
    }
    return special_mappings.get(s, s)

async def get_all_metrics(db: AsyncSession, context: TenantContext | None = None) -> list[dict]:
    metrics = list(BASE_METRICS)
    existing_keys = {m["key"] for m in metrics}
    
    try:
        stmt1 = select(
            MassEvaluationCriterionResult.criterion_key,
            func.max(MassEvaluationCriterionResult.criterion_name).label("name"),
            func.max(MassEvaluationCriterionResult.criterion_type).label("type")
        ).join(
            MassEvaluationResult, MassEvaluationCriterionResult.mass_analysis_id == MassEvaluationResult.mass_analysis_id
        ).where(
            MassEvaluationCriterionResult.criterion_key != None
        )
        if context and not context.is_super_admin:
            stmt1 = stmt1.where(
                or_(
                    MassEvaluationResult.company_id.in_(context.allowed_company_ids),
                    MassEvaluationResult.company_id.is_(None)
                )
            )
            if context.allowed_service_ids is not None:
                stmt1 = stmt1.where(MassEvaluationResult.service_id.in_(context.allowed_service_ids))
        stmt1 = stmt1.group_by(MassEvaluationCriterionResult.criterion_key)
        res1 = await db.execute(stmt1)
        rows1 = res1.all()
    except Exception as e:
        logger.warning(f"Error querying MassEvaluationCriterionResult for catalog: {e}")
        rows1 = []
        
    try:
        stmt2 = select(
            AnalysisCriterionResult.criterion_key,
            func.max(AnalysisCriterionResult.criterion_name).label("name"),
            func.max(AnalysisCriterionResult.criterion_type).label("type")
        ).join(
            Analysis, AnalysisCriterionResult.analysis_id == Analysis.analysis_id
        ).where(
            AnalysisCriterionResult.criterion_key != None
        )
        if context and not context.is_super_admin:
            stmt2 = stmt2.where(
                or_(
                    Analysis.company_id.in_(context.allowed_company_ids),
                    Analysis.company_id.is_(None)
                )
            )
            if context.allowed_service_ids is not None:
                stmt2 = stmt2.where(Analysis.service_id.in_(context.allowed_service_ids))
        stmt2 = stmt2.group_by(AnalysisCriterionResult.criterion_key)
        res2 = await db.execute(stmt2)
        rows2 = res2.all()
    except Exception as e:
        logger.warning(f"Error querying AnalysisCriterionResult for catalog: {e}")
        rows2 = []
        
    discovered = {}
    for row in rows1 + rows2:
        key = row[0]
        name = row[1]
        c_type = row[2]
        norm = normalize_key(key)
        if not norm or norm in existing_keys:
            continue
        if norm not in discovered:
            discovered[norm] = {"name": name or key, "type": c_type}
            
    for fallback in KNOWN_CRITERIA_FALLBACK:
        k = fallback["key"]
        if k not in existing_keys and k not in discovered:
            discovered[k] = {"name": fallback["label"], "type": fallback["type"]}
            
    order = 70
    for key, info in sorted(discovered.items(), key=lambda x: x[0]):
        t = "score"
        c_type = info["type"] or ""
        if key == "cierre_cita" or "percent" in c_type.lower() or "percentage" in c_type.lower():
            t = "percentage"
            
        label = KNOWN_LABELS.get(key, info["name"])
        if label == label.lower():
            label = label.replace("_", " ").title()
            
        metrics.append({
            "key": key,
            "label": label,
            "type": t,
            "order": order,
            "default_selected": False
        })
        order += 10
        
    return metrics

def parse_list_param(values: list[str] | None) -> list[str]:
    if not values:
        return []
    result = []
    for val in values:
        if not val:
            continue
        if "," in val:
            result.extend([item.strip() for item in val.split(",") if item.strip()])
        else:
            result.append(val.strip())
    return result

def _effective_ts(row: Any) -> datetime | None:
    """Returns call_timestamp if set, otherwise analysis_timestamp."""
    ts = getattr(row, "call_timestamp", None) or getattr(row, "analysis_timestamp", None)
    if ts and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts

def extract_metric_value(r: MassEvaluationResult, criteria: list[MassEvaluationCriterionResult], key: str) -> float | None:
    """Extract metric value cleanly handling score vs percentage and aliases."""
    if key == "evaluacion_global":
        val = r.evaluacion_global
        if val is None:
            val = extract_score_from_mass(r.result_json, r.items_json, "evaluacion_global")
        return float(val) if val is not None else None

    # Find matching criterion in database rows if available
    match_row = next((c for c in criteria if normalize_key(c.criterion_key) == key), None)
    if match_row is not None:
        if key == "cierre_cita":
            if match_row.boolean_value is not None:
                return 100.0 if match_row.boolean_value else 0.0
            if match_row.percentage_value is not None:
                return float(match_row.percentage_value)
            if match_row.numeric_value is not None:
                return float(match_row.numeric_value)
        else:
            if match_row.numeric_value is not None:
                return float(match_row.numeric_value)
            if match_row.boolean_value is not None:
                return 10.0 if match_row.boolean_value else 0.0
            if match_row.percentage_value is not None:
                return float(match_row.percentage_value) / 10.0  # Normalize percentage to 0-10 score if needed

    # Fallback to result_json
    rj = r.result_json or {}
    matching_rj_key = next((k for k in rj.keys() if normalize_key(k) == key), None)
    if matching_rj_key is not None:
        val = rj.get(matching_rj_key)
        if key == "cierre_cita":
            if val is not None:
                if isinstance(val, bool):
                    return 100.0 if val else 0.0
                elif isinstance(val, (int, float)):
                    return float(val) if val > 1.0 else float(val) * 100.0
                elif isinstance(val, str):
                    cleaned = val.strip().lower()
                    if cleaned in ["si", "sí", "true", "1"]:
                        return 100.0
                    if cleaned in ["no", "false", "0"]:
                        return 0.0
        else:
            if isinstance(val, bool):
                return 10.0 if val else 0.0
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                try:
                    return float(val)
                except ValueError:
                    cleaned = val.strip().lower()
                    if cleaned in ["si", "sí"]:
                        return 10.0
                    if cleaned == "no":
                        return 0.0

    # Fallback to items_json
    items_list = r.items_json if isinstance(r.items_json, list) else []
    for item in items_list:
        if not isinstance(item, dict):
            continue
        item_key = item.get("key") or item.get("criterion_key") or item.get("output_key")
        if item_key and normalize_key(item_key) == key:
            v = item.get("value") or item.get("score") or item.get("valor")
            if v is not None:
                if key == "cierre_cita":
                    if isinstance(v, bool):
                        return 100.0 if v else 0.0
                    elif isinstance(v, (int, float)):
                        return float(v) if v > 1.0 else float(v) * 100.0
                    elif isinstance(v, str):
                        cleaned = v.strip().lower()
                        if cleaned in ["si", "sí", "true", "1"]:
                            return 100.0
                        if cleaned in ["no", "false", "0"]:
                            return 0.0
                else:
                    if isinstance(v, bool):
                        return 10.0 if v else 0.0
                    if isinstance(v, (int, float)):
                        return float(v)
                    if isinstance(v, str):
                        try:
                            return float(v)
                        except ValueError:
                            cleaned = v.strip().lower()
                            if cleaned in ["si", "sí"]:
                                return 10.0
                            if cleaned == "no":
                                return 0.0

    return None


@router.get(
    "/analytics/items",
    response_model=list[AnalyticsItem],
    responses={
        401: {"description": "Unauthorized Bearer token"},
        403: {"description": "Forbidden role requirement failure"},
        500: {"description": "Internal server error"}
    }
)
async def get_analytics_items(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    service: Annotated[str | None, Query(description="Filter by service key, slug, or ID")] = None,
):
    """Retrieve the catalogue of compared metrics available in Analytics v2."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de nivel superior."
        )
    eff_service_id, _ = await resolve_service_id(
        db,
        service_id=service_id,
        service_key=service_key,
        service_param=service,
        company_ids=None if context.is_super_admin else context.allowed_company_ids
    )
    return await get_all_metrics(db, context=context)



@router.get(
    "/analytics/agents-comparison",
    response_model=AgentComparisonResponse,
    responses={
        401: {"description": "Unauthorized Bearer token"},
        403: {"description": "Forbidden role requirement"},
        422: {"description": "Validation error on params"},
        500: {"description": "Internal server error"}
    }
)
async def get_agents_comparison(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date_from: Annotated[str | None, Query(description="Start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="End date (ISO or YYYY-MM-DD)")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    service: Annotated[str | None, Query(description="Filter by service key, slug, or ID (alias for service_key/service_id)")] = None,
    agent_owner_ids: Annotated[list[str] | None, Query(description="Filter agent owner IDs")] = None,
    agent_owner_ids_bracket: Annotated[list[str] | None, Query(alias="agent_owner_ids[]", description="Filter agent owner IDs (array format)")] = None,
    item_keys: Annotated[list[str] | None, Query(description="Filter compared item keys")] = None,
    item_keys_bracket: Annotated[list[str] | None, Query(alias="item_keys[]", description="Filter compared item keys (array format)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    typology: Annotated[str | None, Query(description="Filter by typology key/name")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Filter by call type")] = None,
    call_type: Annotated[str | None, Query(description="Filter by call type")] = None,
    selected_typology: Annotated[str | None, Query(description="Filter by selected typology")] = None,
    typologies: Annotated[str | None, Query(description="Filter by typology")] = None,
    direction: Annotated[str | None, Query(description="all | inbound | outbound")] = None,
    call_direction: Annotated[str | None, Query(description="Filter by call direction")] = None,
    inbound_outbound: Annotated[str | None, Query(description="Filter by inbound/outbound")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
):
    """
    Retrieve agents performance comparison breakdown.
    Optimized to run exactly two SQL queries with targeted scalar column selection to avoid ORM overhead.
    """
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de nivel superior."
        )
    t_start = time.perf_counter()
    try:
        eff_service_id, eff_service_key = await resolve_service_id(
            db,
            service_id=service_id,
            service_key=service_key,
            service_param=service,
            company_ids=None if context.is_super_admin else context.allowed_company_ids
        )

        raw_typology = typology or typology_key or tipo_llamada or call_type or selected_typology or typologies
        norm_t = normalize_typology(raw_typology)
        raw_direction = direction or call_direction or inbound_outbound
        norm_d = normalize_direction(raw_direction)

        # 1. Resolve timeframe and validate
        dt_from, dt_to, _ = resolve_date_range(date_from, date_to, period=None, default_period="30d")
        if dt_from and dt_to and dt_from > dt_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Rango de fechas inválido: date_from ({date_from}) no puede ser posterior a date_to ({date_to})."
            )

        # Build unique cache key
        owner_ids = parse_list_param(agent_owner_ids) + parse_list_param(agent_owner_ids_bracket)
        item_req_keys = parse_list_param(item_keys) + parse_list_param(item_keys_bracket)
        cache_key = (
            f"agents_comp:{context.company_id}:{context.normalized_role}:{eff_service_id}:{eff_service_key}:"
            f"{date_from}:{date_to}:{sorted(owner_ids)}:{sorted(item_req_keys)}:{norm_t}:{norm_d}:"
            f"{duration_min_seconds}:{duration_max_seconds}:{avg_score_min}:{avg_score_max}"
        )

        async def _compute():
            # 2. Build Mass Evaluation Results filter query - select ONLY scalar columns needed
            stmt = select(
                MassEvaluationResult.mass_analysis_id,
                MassEvaluationResult.hubspot_owner_id,
                MassEvaluationResult.agent_name,
                MassEvaluationResult.evaluacion_global,
                MassEvaluationResult.result_json,
                MassEvaluationResult.items_json,
                MassEvaluationResult.call_timestamp,
                MassEvaluationResult.analysis_timestamp,
            ).where(MassEvaluationResult.status == "completed")

            if not context.is_super_admin:
                stmt = stmt.where(
                    or_(
                        MassEvaluationResult.company_id.in_(context.allowed_company_ids),
                        MassEvaluationResult.company_id.is_(None)
                    )
                )
            if context.allowed_service_ids is not None:
                stmt = stmt.where(MassEvaluationResult.service_id.in_(context.allowed_service_ids))
                
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
            if eff_service_id is not None:
                if context.allowed_service_ids is not None and eff_service_id not in context.allowed_service_ids:
                    stmt = stmt.where(MassEvaluationResult.service_id == -1)
                else:
                    stmt = stmt.where(MassEvaluationResult.service_id == eff_service_id)
            elif eff_service_key is not None:
                stmt = stmt.where(MassEvaluationResult.service_key == eff_service_key)

            typo_ids = None
            if typology_ids and typology_ids.strip():
                typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]
            if typo_ids:
                stmt = stmt.where(MassEvaluationResult.typology_id.in_(typo_ids))
            elif norm_t:
                stmt = stmt.where(
                    or_(
                        func.lower(MassEvaluationResult.typology_key) == norm_t,
                        func.lower(func.coalesce(MassEvaluationResult.result_json["tipo_llamada"].astext, "")) == norm_t
                    )
                )

            if norm_d:
                stmt = stmt.where(
                    or_(
                        func.lower(MassEvaluationResult.direction) == norm_d,
                        func.lower(func.coalesce(MassEvaluationResult.result_json["inbound_outbound"].astext, "")) == norm_d
                    )
                )
            if duration_min_seconds is not None:
                stmt = stmt.where(MassEvaluationResult.call_duration_seconds >= duration_min_seconds)
            if duration_max_seconds is not None:
                stmt = stmt.where(MassEvaluationResult.call_duration_seconds <= duration_max_seconds)
                
            score_min_scaled = (avg_score_min / 10.0 if avg_score_min > 10.0 else avg_score_min) if avg_score_min is not None else None
            score_max_scaled = (avg_score_max / 10.0 if avg_score_max > 10.0 else avg_score_max) if avg_score_max is not None else None
                
            if score_min_scaled is not None:
                stmt = stmt.where(MassEvaluationResult.evaluacion_global >= score_min_scaled)
            if score_max_scaled is not None:
                stmt = stmt.where(MassEvaluationResult.evaluacion_global <= score_max_scaled)

            if context.allowed_agent_ids is not None:
                if owner_ids:
                    allowed_requested = [oid for oid in owner_ids if oid in context.allowed_agent_ids]
                    if not allowed_requested:
                        stmt = stmt.where(MassEvaluationResult.hubspot_owner_id == "-1")
                    else:
                        stmt = stmt.where(MassEvaluationResult.hubspot_owner_id.in_(allowed_requested))
                else:
                    stmt = stmt.where(MassEvaluationResult.hubspot_owner_id.in_(context.allowed_agent_ids))
            else:
                if owner_ids:
                    stmt = stmt.where(MassEvaluationResult.hubspot_owner_id.in_(owner_ids))

            t_db_start = time.perf_counter()
            res = await db.execute(stmt)
            results = res.all()
            db_ms = (time.perf_counter() - t_db_start) * 1000.0

            analysis_ids = [r.mass_analysis_id for r in results]
            criteria_by_analysis = {}
            if analysis_ids:
                stmt_crit = select(
                    MassEvaluationCriterionResult.mass_analysis_id,
                    MassEvaluationCriterionResult.criterion_key,
                    MassEvaluationCriterionResult.numeric_value,
                    MassEvaluationCriterionResult.boolean_value,
                    MassEvaluationCriterionResult.percentage_value
                ).where(
                    MassEvaluationCriterionResult.mass_analysis_id.in_(analysis_ids),
                    MassEvaluationCriterionResult.is_applicable == True
                )
                res_crit = await db.execute(stmt_crit)
                for c in res_crit.all():
                    criteria_by_analysis.setdefault(c.mass_analysis_id, []).append(c)

            available_catalog = await get_available_agents(db, context=context, service_id=eff_service_id)
            cat_by_oid = {a.hubspot_owner_id: a for a in available_catalog}

            agents_found = {}
            for r in results:
                oid = r.hubspot_owner_id
                if oid and oid not in agents_found:
                    name = resolve_owner_name(oid)
                    if not name and r.agent_name and not r.agent_name.isdigit():
                        name = r.agent_name
                    if not name:
                        name = oid
                    agents_found[oid] = name

            if agents_found:
                agents_list = []
                for oid, name in sorted(agents_found.items(), key=lambda x: x[1]):
                    if oid in cat_by_oid:
                        agents_list.append(cat_by_oid[oid])
                    else:
                        agents_list.append(
                            AgentInfo(
                                hubspot_owner_id=oid,
                                agent_name=name,
                                name=name,
                                agent_initials=None,
                                initials=None,
                                label=name,
                                service_id=eff_service_id,
                                service_name=None,
                            )
                        )
            else:
                agents_list = available_catalog

            all_metrics = await get_all_metrics(db, context=context)
            if item_req_keys:
                effective_keys = item_req_keys[:50] if len(item_req_keys) > 50 else item_req_keys
                items_to_use = [item for item in all_metrics if item["key"] in effective_keys]
            else:
                default_items = [item for item in all_metrics if item.get("default_selected")]
                if default_items:
                    items_to_use = default_items[:20]
                else:
                    items_to_use = all_metrics[:20]

            items_list = [AnalyticsItem(**item) for item in items_to_use]

            comparison_rows = []
            for oid, agent_name in sorted(agents_found.items(), key=lambda x: x[1]):
                agent_results = [r for r in results if r.hubspot_owner_id == oid]
                for item in items_to_use:
                    key = item["key"]
                    extracted_vals = []
                    for r in agent_results:
                        crit_rows = criteria_by_analysis.get(r.mass_analysis_id, [])
                        val = extract_metric_value(r, crit_rows, key)
                        if val is not None:
                            extracted_vals.append(val)
                    
                    count = len(extracted_vals)
                    value = round(sum(extracted_vals) / count, 1) if count > 0 else None
                    
                    comparison_rows.append(
                        AgentComparisonRow(
                            hubspot_owner_id=oid,
                            agent_name=agent_name,
                            item_key=key,
                            item_label=item["label"],
                            metric_type=item["type"],
                            value=value,
                            count=count
                        )
                    )

            return AgentComparisonResponse(
                agents=agents_list,
                items=items_list,
                comparison=comparison_rows
            ), len(results), len(comparison_rows), db_ms

        (resp, rows_scanned, rows_returned, db_ms), is_cache_hit = await analytics_cache.get_or_compute(cache_key, _compute, ttl=30)
        total_processing_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
        aggregation_ms = round(max(0.0, total_processing_ms - db_ms), 1)

        logger.info(
            "[perf.agents_comparison] endpoint=/bm/analytics/agents-comparison company_id=%s service_param=%s service_id=%s service_key=%s date_from=%s date_to=%s agents_count=%d items_count=%d mode=scores direction=%s rows_scanned=%d rows_returned=%d db_ms=%.1f aggregation_ms=%.1f total_ms=%.1f",
            context.allowed_company_ids if context else None, service, eff_service_id, eff_service_key, date_from, date_to, len(resp.agents), len(resp.items), norm_d, rows_scanned, rows_returned, db_ms, aggregation_ms, total_processing_ms
        )

        return resp
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to retrieve Analytics agents comparison")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load agents comparison: {str(e)}"
        )



@router.get(
    "/analytics/items-evolution",
    response_model=list[ItemEvolutionSeries],
    responses={
        401: {"description": "Unauthorized Bearer token"},
        403: {"description": "Forbidden role requirement"},
        422: {"description": "Validation error on params"},
        500: {"description": "Internal server error"}
    }
)
async def get_items_evolution(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date_from: Annotated[str | None, Query(description="Start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="End date (ISO or YYYY-MM-DD)")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    service: Annotated[str | None, Query(description="Filter by service key, slug, or ID")] = None,
    agent_owner_ids: Annotated[list[str] | None, Query(description="Filter agent owner IDs")] = None,
    agent_owner_ids_bracket: Annotated[list[str] | None, Query(alias="agent_owner_ids[]", description="Filter agent owner IDs (array format)")] = None,
    item_keys: Annotated[list[str] | None, Query(description="Filter compared item keys")] = None,
    item_keys_bracket: Annotated[list[str] | None, Query(alias="item_keys[]", description="Filter compared item keys (array format)")] = None,
    bucket: Annotated[str | None, Query(description="Timeline grouping interval: hour | day | week")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    typology: Annotated[str | None, Query(description="Filter by typology key/name")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Filter by call type")] = None,
    call_type: Annotated[str | None, Query(description="Filter by call type")] = None,
    selected_typology: Annotated[str | None, Query(description="Filter by selected typology")] = None,
    typologies: Annotated[str | None, Query(description="Filter by typology")] = None,
    direction: Annotated[str | None, Query(description="all | inbound | outbound")] = None,
    call_direction: Annotated[str | None, Query(description="Filter by call direction")] = None,
    inbound_outbound: Annotated[str | None, Query(description="Filter by inbound/outbound")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
):
    """
    Retrieve chronological evolution timeline for chosen analytics metrics.
    Grouped by time intervals (hour, day, or week).
    """
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de nivel superior."
        )
    t_start = time.perf_counter()
    try:
        eff_service_id, eff_service_key = await resolve_service_id(
            db,
            service_id=service_id,
            service_key=service_key,
            service_param=service,
            company_ids=None if context.is_super_admin else context.allowed_company_ids
        )
        raw_typology = typology or typology_key or tipo_llamada or call_type or selected_typology or typologies
        norm_t = normalize_typology(raw_typology)
        raw_direction = direction or call_direction or inbound_outbound
        norm_d = normalize_direction(raw_direction)

        dt_from, dt_to, bucket_interval = resolve_date_range(date_from, date_to, period=None, default_period="30d")
        if bucket and bucket.strip().lower() in ("hour", "day", "week"):
            bucket_interval = bucket.strip().lower()
        if dt_from and dt_to and dt_from > dt_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Rango de fechas inválido: date_from ({date_from}) no puede ser posterior a date_to ({date_to})."
            )

        owner_ids = parse_list_param(agent_owner_ids) + parse_list_param(agent_owner_ids_bracket)
        item_req_keys = parse_list_param(item_keys) + parse_list_param(item_keys_bracket)
        cache_key = (
            f"items_evo:{context.company_id}:{context.normalized_role}:{eff_service_id}:{eff_service_key}:"
            f"{date_from}:{date_to}:{sorted(owner_ids)}:{sorted(item_req_keys)}:{bucket_interval}:{norm_t}:{norm_d}:"
            f"{duration_min_seconds}:{duration_max_seconds}:{avg_score_min}:{avg_score_max}"
        )

        async def _compute():
            stmt = select(
                MassEvaluationResult.mass_analysis_id,
                MassEvaluationResult.hubspot_owner_id,
                MassEvaluationResult.agent_name,
                MassEvaluationResult.evaluacion_global,
                MassEvaluationResult.result_json,
                MassEvaluationResult.items_json,
                MassEvaluationResult.call_timestamp,
                MassEvaluationResult.analysis_timestamp,
            ).where(MassEvaluationResult.status == "completed")

            if not context.is_super_admin:
                stmt = stmt.where(
                    or_(
                        MassEvaluationResult.company_id.in_(context.allowed_company_ids),
                        MassEvaluationResult.company_id.is_(None)
                    )
                )
            if context.allowed_service_ids is not None:
                stmt = stmt.where(MassEvaluationResult.service_id.in_(context.allowed_service_ids))
                
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
            if eff_service_id is not None:
                if context.allowed_service_ids is not None and eff_service_id not in context.allowed_service_ids:
                    stmt = stmt.where(MassEvaluationResult.service_id == -1)
                else:
                    stmt = stmt.where(MassEvaluationResult.service_id == eff_service_id)
            elif eff_service_key is not None:
                stmt = stmt.where(MassEvaluationResult.service_key == eff_service_key)

            typo_ids = None
            if typology_ids and typology_ids.strip():
                typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]
            if typo_ids:
                stmt = stmt.where(MassEvaluationResult.typology_id.in_(typo_ids))
            elif norm_t:
                stmt = stmt.where(
                    or_(
                        func.lower(MassEvaluationResult.typology_key) == norm_t,
                        func.lower(func.coalesce(MassEvaluationResult.result_json["tipo_llamada"].astext, "")) == norm_t
                    )
                )

            if norm_d:
                stmt = stmt.where(
                    or_(
                        func.lower(MassEvaluationResult.direction) == norm_d,
                        func.lower(func.coalesce(MassEvaluationResult.result_json["inbound_outbound"].astext, "")) == norm_d
                    )
                )
            if duration_min_seconds is not None:
                stmt = stmt.where(MassEvaluationResult.call_duration_seconds >= duration_min_seconds)
            if duration_max_seconds is not None:
                stmt = stmt.where(MassEvaluationResult.call_duration_seconds <= duration_max_seconds)
                
            score_min_scaled = (avg_score_min / 10.0 if avg_score_min > 10.0 else avg_score_min) if avg_score_min is not None else None
            score_max_scaled = (avg_score_max / 10.0 if avg_score_max > 10.0 else avg_score_max) if avg_score_max is not None else None
                
            if score_min_scaled is not None:
                stmt = stmt.where(MassEvaluationResult.evaluacion_global >= score_min_scaled)
            if score_max_scaled is not None:
                stmt = stmt.where(MassEvaluationResult.evaluacion_global <= score_max_scaled)

            if context.allowed_agent_ids is not None:
                if owner_ids:
                    allowed_requested = [oid for oid in owner_ids if oid in context.allowed_agent_ids]
                    if not allowed_requested:
                        stmt = stmt.where(MassEvaluationResult.hubspot_owner_id == "-1")
                    else:
                        stmt = stmt.where(MassEvaluationResult.hubspot_owner_id.in_(allowed_requested))
                else:
                    stmt = stmt.where(MassEvaluationResult.hubspot_owner_id.in_(context.allowed_agent_ids))
            else:
                if owner_ids:
                    stmt = stmt.where(MassEvaluationResult.hubspot_owner_id.in_(owner_ids))

            res = await db.execute(stmt)
            results = res.all()

            analysis_ids = [r.mass_analysis_id for r in results]
            criteria_by_analysis = {}
            if analysis_ids:
                stmt_crit = select(
                    MassEvaluationCriterionResult.mass_analysis_id,
                    MassEvaluationCriterionResult.criterion_key,
                    MassEvaluationCriterionResult.numeric_value,
                    MassEvaluationCriterionResult.boolean_value,
                    MassEvaluationCriterionResult.percentage_value
                ).where(
                    MassEvaluationCriterionResult.mass_analysis_id.in_(analysis_ids),
                    MassEvaluationCriterionResult.is_applicable == True
                )
                res_crit = await db.execute(stmt_crit)
                for c in res_crit.all():
                    criteria_by_analysis.setdefault(c.mass_analysis_id, []).append(c)

            buckets_map: dict[str, list[Any]] = {}
            for r in results:
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

            all_metrics = await get_all_metrics(db, context=context)
            if item_req_keys:
                effective_keys = item_req_keys[:50] if len(item_req_keys) > 50 else item_req_keys
                items_to_use = [item for item in all_metrics if item["key"] in effective_keys]
            else:
                default_items = [item for item in all_metrics if item.get("default_selected")]
                if default_items:
                    items_to_use = default_items[:20]
                else:
                    items_to_use = all_metrics[:20]

            series_list = []
            for item in items_to_use:
                key = item["key"]
                points = []
                for b_key in sorted(buckets_map.keys()):
                    bucket_results = buckets_map[b_key]
                    extracted_vals = []
                    for r in bucket_results:
                        crit_rows = criteria_by_analysis.get(r.mass_analysis_id, [])
                        val = extract_metric_value(r, crit_rows, key)
                        if val is not None:
                            extracted_vals.append(val)
                    
                    count = len(extracted_vals)
                    value = round(sum(extracted_vals) / count, 1) if count > 0 else None
                    points.append(
                        EvolutionPoint(
                            date=b_key,
                            value=value,
                            count=count,
                            analysis_count=count
                        )
                    )
                    
                series_list.append(
                    ItemEvolutionSeries(
                        item_key=key,
                        item_label=item["label"],
                        metric_type=item["type"],
                        bucket_interval=bucket_interval,
                        points=points
                    )
                )

            return series_list

        series_res, _ = await analytics_cache.get_or_compute(cache_key, _compute, ttl=30)
        return series_res
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to retrieve Analytics items evolution timeline")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load items evolution: {str(e)}"
        )



async def get_available_agents(
    db: AsyncSession,
    context: TenantContext | None = None,
    service_id: int | None = None,
) -> list[AgentInfo]:
    """
    Retrieve all available call center agents for the current scope/service,
    returning full metadata including initials and label for frontend selectors.
    """
    from app.utils.agent_resolvers import build_user_initials_maps, resolve_agent_initials
    from app.utils.hubspot_owners import OWNER_TO_NAME, resolve_owner_name

    by_owner, by_name, users_list = await build_user_initials_maps(db, company_id=None)

    query = """
        SELECT DISTINCT hubspot_owner_id, agent_name, service_id, service_name
        FROM bm_mass_evaluation_results
        WHERE status = 'completed' AND hubspot_owner_id IS NOT NULL
    """
    params = {}
    if context and not context.is_super_admin:
        query += f" AND (company_id IN {_format_int_list(context.allowed_company_ids)} OR company_id IS NULL)"
        if context.allowed_service_ids is not None:
            query += f" AND service_id IN {_format_int_list(context.allowed_service_ids)}"
        if context.allowed_agent_ids is not None:
            query += f" AND hubspot_owner_id IN {_format_str_list(context.allowed_agent_ids)}"

    if service_id is not None:
        if context and context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
            query += " AND service_id = -1"
        else:
            query += " AND service_id = :service_id"
            params["service_id"] = service_id

    res = await db.execute(text(query), params)
    db_rows = res.fetchall()

    agents_map: dict[str, dict[str, Any]] = {}

    # 1. Standard known mapping (only include globally if service_id is None)
    if service_id is None:
        for oid, name in OWNER_TO_NAME.items():
            if context and context.allowed_agent_ids is not None and oid not in context.allowed_agent_ids:
                continue
            initials = resolve_agent_initials(
                hubspot_owner_id=oid,
                agent_name=name,
                by_owner=by_owner,
                by_name=by_name,
                users_list=users_list,
            )
            label = f"{initials} · {name}" if initials else name
            agents_map[oid] = {
                "hubspot_owner_id": oid,
                "agent_name": name,
                "name": name,
                "agent_initials": initials,
                "initials": initials,
                "label": label,
                "service_id": None,
                "service_name": None,
            }

    # 2. Add agents from DB results matching service scope
    for row in db_rows:
        oid = str(row[0]).strip()
        r_name = row[1]
        svc_id = row[2]
        svc_name = row[3]

        if context and context.allowed_agent_ids is not None and oid not in context.allowed_agent_ids:
            continue

        disp_name = resolve_owner_name(oid) or (r_name if r_name and not r_name.isdigit() else oid)
        if disp_name.startswith("Agente no identificado") and oid not in OWNER_TO_NAME:
            pass

        initials = resolve_agent_initials(
            hubspot_owner_id=oid,
            agent_name=disp_name,
            by_owner=by_owner,
            by_name=by_name,
            users_list=users_list,
        )
        label = f"{initials} · {disp_name}" if initials else disp_name

        if oid not in agents_map:
            agents_map[oid] = {
                "hubspot_owner_id": oid,
                "agent_name": disp_name,
                "name": disp_name,
                "agent_initials": initials,
                "initials": initials,
                "label": label,
                "service_id": svc_id or service_id,
                "service_name": svc_name,
            }
        else:
            if svc_id and not agents_map[oid]["service_id"]:
                agents_map[oid]["service_id"] = svc_id
            if svc_name and not agents_map[oid]["service_name"]:
                agents_map[oid]["service_name"] = svc_name

    # 3. Add users from bm_users with hubspot_owner_id matching service scope
    for u in users_list:
        oid = u.get("hubspot_owner_id")
        if not oid:
            continue
        if context and context.allowed_agent_ids is not None and oid not in context.allowed_agent_ids:
            continue
        if service_id is not None and u.get("service_id") and u.get("service_id") != service_id:
            continue
        u_name = u.get("name") or u.get("username") or resolve_owner_name(oid) or oid
        u_inits = u.get("agent_initials") or resolve_agent_initials(
            hubspot_owner_id=oid,
            agent_name=u_name,
            by_owner=by_owner,
            by_name=by_name,
            users_list=users_list,
        )
        label = f"{u_inits} · {u_name}" if u_inits else u_name
        if oid not in agents_map:
            agents_map[oid] = {
                "hubspot_owner_id": oid,
                "agent_name": u_name,
                "name": u_name,
                "agent_initials": u_inits,
                "initials": u_inits,
                "label": label,
                "service_id": u.get("service_id") or service_id,
                "service_name": None,
            }

    agents_list = [
        AgentInfo(**item)
        for item in sorted(list(agents_map.values()), key=lambda x: x["name"])
    ]
    return agents_list


@router.get("/analytics/filter-options")
@router.get("/filter-options")
async def get_filter_options(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: Annotated[int | None, Query(description="Filter active typologies by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    service: Annotated[str | None, Query(description="Filter by service key, slug, or ID")] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """
    Retrieve filter configuration options: active typologies, agents, duration range, and score bounds.
    """
    try:
        eff_service_id, _ = await resolve_service_id(
            db,
            service_id=service_id,
            service_key=service_key,
            service_param=service,
            company_ids=None if context.is_super_admin else context.allowed_company_ids
        )

        # 1. Fetch available agents list for current scope
        available_agents = await get_available_agents(db, context=context, service_id=eff_service_id)

        # 2. Fetch active typologies per service
        typo_query = f"SELECT t.typology_id, t.typology_key, t.typology_name, t.service_id, s.service_key FROM bm_typologies t JOIN bm_services s ON t.service_id = s.service_id WHERE t.is_active = true AND s.company_id IN {_format_int_list(context.allowed_company_ids)}"
        params = {}
        if context.allowed_service_ids is not None:
            typo_query += f" AND t.service_id IN {_format_int_list(context.allowed_service_ids)}"
            
        if eff_service_id is not None:
            if context.allowed_service_ids is not None and eff_service_id not in context.allowed_service_ids:
                typo_query += " AND t.service_id = -1"
            else:
                typo_query += " AND t.service_id = :service_id"
                params["service_id"] = eff_service_id


        typo_res = await db.execute(text(typo_query), params)
            
        typologies_list = []
        for row in typo_res.fetchall():
            typologies_list.append({
                "id": row[0],
                "typology_key": row[1],
                "name": row[2],
                "service_id": row[3],
                "service_key": row[4]
            })

        # 3. Fetch min and max call duration
        dur_query = f"SELECT MIN(call_duration_seconds), MAX(call_duration_seconds) FROM bm_mass_evaluation_results WHERE status = 'completed' AND (company_id IN {_format_int_list(context.allowed_company_ids)} OR company_id IS NULL)"
        dur_params = {}
        if context.allowed_service_ids is not None:
            dur_query += f" AND service_id IN {_format_int_list(context.allowed_service_ids)}"
        if context.allowed_agent_ids is not None:
            dur_query += f" AND hubspot_owner_id IN {_format_str_list(context.allowed_agent_ids)}"
            
        dur_res = await db.execute(text(dur_query), dur_params)
        dur_row = dur_res.fetchone()
        
        min_seconds = 0
        max_seconds = 1800
        if dur_row:
            if dur_row[0] is not None:
                min_seconds = int(dur_row[0])
            if dur_row[1] is not None:
                max_seconds = int(dur_row[1])

        return {
            "agents": available_agents,
            "typologies": typologies_list,
            "duration": {
                "min_seconds": min_seconds,
                "max_seconds": max_seconds
            },
            "avg_score": {
                "min": 0,
                "max": 10,
                "scale": "score_0_10"
            }
        }
    except Exception as e:
        logger.exception("Failed to retrieve filter options")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load filter options: {str(e)}"
        )
