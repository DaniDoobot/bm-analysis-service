"""API Router for Analytics v2."""
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, text
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
        if context:
            stmt1 = stmt1.where(MassEvaluationResult.company_id.in_(context.allowed_company_ids))
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
        if context:
            stmt2 = stmt2.where(Analysis.company_id.in_(context.allowed_company_ids))
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
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Retrieve the catalogue of compared metrics available in Analytics v2."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de nivel superior."
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
    agent_owner_ids: Annotated[list[str] | None, Query(description="Filter agent owner IDs")] = None,
    agent_owner_ids_bracket: Annotated[list[str] | None, Query(alias="agent_owner_ids[]", description="Filter agent owner IDs (array format)")] = None,
    item_keys: Annotated[list[str] | None, Query(description="Filter compared item keys")] = None,
    item_keys_bracket: Annotated[list[str] | None, Query(alias="item_keys[]", description="Filter compared item keys (array format)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
):
    """
    Retrieve agents performance comparison breakdown.
    Optimized to run exactly two SQL queries to avoid N+1 issues.
    """
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de nivel superior."
        )
    try:
        # 1. Resolve timeframe
        dt_from, dt_to, _ = resolve_date_range(date_from, date_to, period=None, default_period="30d")
        
        # 2. Build Mass Evaluation Results filter query
        stmt = select(MassEvaluationResult).where(MassEvaluationResult.status == "completed")
        
        stmt = stmt.where(MassEvaluationResult.company_id.in_(context.allowed_company_ids))
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
        if service_id is not None:
            if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
                stmt = stmt.where(MassEvaluationResult.service_id == -1)
            else:
                stmt = stmt.where(MassEvaluationResult.service_id == service_id)
        elif service_key is not None:
            stmt = stmt.where(MassEvaluationResult.service_key == service_key)

        typo_ids = None
        if typology_ids and typology_ids.strip():
            typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]
        if typo_ids:
            stmt = stmt.where(MassEvaluationResult.typology_id.in_(typo_ids))
        if duration_min_seconds is not None:
            stmt = stmt.where(MassEvaluationResult.call_duration_seconds >= duration_min_seconds)
        if duration_max_seconds is not None:
            stmt = stmt.where(MassEvaluationResult.call_duration_seconds <= duration_max_seconds)
            
        # Official scale: 0-10 (matches DB storage). Legacy compat: if value > 10 assume 0-100 and divide.
        score_min_scaled = (avg_score_min / 10.0 if avg_score_min > 10.0 else avg_score_min) if avg_score_min is not None else None
        score_max_scaled = (avg_score_max / 10.0 if avg_score_max > 10.0 else avg_score_max) if avg_score_max is not None else None
            
        if score_min_scaled is not None:
            stmt = stmt.where(MassEvaluationResult.evaluacion_global >= score_min_scaled)
        if score_max_scaled is not None:
            stmt = stmt.where(MassEvaluationResult.evaluacion_global <= score_max_scaled)

        owner_ids = parse_list_param(agent_owner_ids) + parse_list_param(agent_owner_ids_bracket)
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

        # Query Results
        res = await db.execute(stmt)
        results = res.scalars().all()

        # 3. Query associated criteria results for the matched analyses
        analysis_ids = [r.mass_analysis_id for r in results]
        criteria_by_analysis = {}
        if analysis_ids:
            stmt_crit = select(MassEvaluationCriterionResult).where(
                MassEvaluationCriterionResult.mass_analysis_id.in_(analysis_ids),
                MassEvaluationCriterionResult.is_applicable == True
            )
            res_crit = await db.execute(stmt_crit)
            for c in res_crit.scalars().all():
                criteria_by_analysis.setdefault(c.mass_analysis_id, []).append(c)

        # 4. Resolve unique list of agents
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

        agents_list = [
            AgentInfo(hubspot_owner_id=oid, agent_name=name)
            for oid, name in sorted(agents_found.items(), key=lambda x: x[1])
        ]

        # 5. Filter items catalogue
        all_metrics = await get_all_metrics(db, context=context)
        keys_to_use = parse_list_param(item_keys) + parse_list_param(item_keys_bracket)
        if keys_to_use:
            items_to_use = [item for item in all_metrics if item["key"] in keys_to_use]
        else:
            items_to_use = all_metrics

        items_list = [AnalyticsItem(**item) for item in items_to_use]

        # 6. Build comparison rows
        comparison_rows = []
        for oid, agent_name in agents_found.items():
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
        )
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
    agent_owner_ids: Annotated[list[str] | None, Query(description="Filter agent owner IDs")] = None,
    agent_owner_ids_bracket: Annotated[list[str] | None, Query(alias="agent_owner_ids[]", description="Filter agent owner IDs (array format)")] = None,
    item_keys: Annotated[list[str] | None, Query(description="Filter compared item keys")] = None,
    item_keys_bracket: Annotated[list[str] | None, Query(alias="item_keys[]", description="Filter compared item keys (array format)")] = None,
    bucket: Annotated[str | None, Query(description="Timeline grouping interval: hour | day | week")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
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
    try:
        # 1. Resolve timeframe
        dt_from, dt_to, recommended_bucket = resolve_date_range(date_from, date_to, period=None, default_period="30d")
        bucket_interval = bucket if bucket in ["hour", "day", "week"] else recommended_bucket

        # 2. Build Mass Evaluation Results filter query
        stmt = select(MassEvaluationResult).where(MassEvaluationResult.status == "completed")
        
        stmt = stmt.where(MassEvaluationResult.company_id.in_(context.allowed_company_ids))
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
        if service_id is not None:
            if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
                stmt = stmt.where(MassEvaluationResult.service_id == -1)
            else:
                stmt = stmt.where(MassEvaluationResult.service_id == service_id)
        elif service_key is not None:
            stmt = stmt.where(MassEvaluationResult.service_key == service_key)

        typo_ids = None
        if typology_ids and typology_ids.strip():
            typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]
        if typo_ids:
            stmt = stmt.where(MassEvaluationResult.typology_id.in_(typo_ids))
        if duration_min_seconds is not None:
            stmt = stmt.where(MassEvaluationResult.call_duration_seconds >= duration_min_seconds)
        if duration_max_seconds is not None:
            stmt = stmt.where(MassEvaluationResult.call_duration_seconds <= duration_max_seconds)
            
        # Official scale: 0-10 (matches DB storage). Legacy compat: if value > 10 assume 0-100 and divide.
        score_min_scaled = (avg_score_min / 10.0 if avg_score_min > 10.0 else avg_score_min) if avg_score_min is not None else None
        score_max_scaled = (avg_score_max / 10.0 if avg_score_max > 10.0 else avg_score_max) if avg_score_max is not None else None
            
        if score_min_scaled is not None:
            stmt = stmt.where(MassEvaluationResult.evaluacion_global >= score_min_scaled)
        if score_max_scaled is not None:
            stmt = stmt.where(MassEvaluationResult.evaluacion_global <= score_max_scaled)

        owner_ids = parse_list_param(agent_owner_ids) + parse_list_param(agent_owner_ids_bracket)
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

        # Query Results
        res = await db.execute(stmt)
        results = res.scalars().all()

        # 3. Query associated criteria results for the matched analyses
        analysis_ids = [r.mass_analysis_id for r in results]
        criteria_by_analysis = {}
        if analysis_ids:
            stmt_crit = select(MassEvaluationCriterionResult).where(
                MassEvaluationCriterionResult.mass_analysis_id.in_(analysis_ids),
                MassEvaluationCriterionResult.is_applicable == True
            )
            res_crit = await db.execute(stmt_crit)
            for c in res_crit.scalars().all():
                criteria_by_analysis.setdefault(c.mass_analysis_id, []).append(c)

        # 4. Group results by time interval bucket
        buckets_map: dict[str, list[MassEvaluationResult]] = {}
        for r in results:
            ts = _effective_ts(r)
            if not ts:
                continue
            if bucket_interval == "hour":
                b_key = ts.strftime("%Y-%m-%d %H:00")
            elif bucket_interval == "day":
                b_key = ts.strftime("%Y-%m-%d")
            else:
                # Group by Monday start of week
                b_key = (ts - timedelta(days=ts.weekday())).strftime("%Y-%m-%d")
            buckets_map.setdefault(b_key, []).append(r)

        # 5. Filter items catalogue
        all_metrics = await get_all_metrics(db, context=context)
        keys_to_use = parse_list_param(item_keys) + parse_list_param(item_keys_bracket)
        if keys_to_use:
            items_to_use = [item for item in all_metrics if item["key"] in keys_to_use]
        else:
            items_to_use = all_metrics

        # 6. Construct timeline points per item series
        series_list = []
        for item in items_to_use:
            key = item["key"]
            points = []
            
            # Sorted bucket keys chronologically
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
                
                # We return even empty points if evaluations existed in that bucket, or omit if count=0 depending on preference.
                # Here we follow the exact spec requirement: "Si no hay datos válidos, value=null y count=0"
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
                    points=points
                )
            )

        return series_list
    except Exception as e:
        logger.exception("Failed to retrieve Analytics items evolution timeline")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load items evolution: {str(e)}"
        )



@router.get("/filter-options")
async def get_filter_options(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: Annotated[int | None, Query(description="Filter active typologies by service ID")] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """
    Retrieve filter configuration options: active typologies, duration range, and score bounds.
    """
    try:
        # 1. Fetch active typologies per service
        typo_query = f"SELECT t.typology_id, t.typology_key, t.typology_name, t.service_id, s.service_key FROM bm_typologies t JOIN bm_services s ON t.service_id = s.service_id WHERE t.is_active = true AND s.company_id IN {_format_int_list(context.allowed_company_ids)}"
        params = {}
        if context.allowed_service_ids is not None:
            typo_query += f" AND t.service_id IN {_format_int_list(context.allowed_service_ids)}"
            
        if service_id is not None:
            if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
                typo_query += " AND t.service_id = -1"
            else:
                typo_query += " AND t.service_id = :service_id"
                params["service_id"] = service_id

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

        # 2. Fetch min and max call duration
        dur_query = f"SELECT MIN(call_duration_seconds), MAX(call_duration_seconds) FROM bm_mass_evaluation_results WHERE status = 'completed' AND company_id IN {_format_int_list(context.allowed_company_ids)}"
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
