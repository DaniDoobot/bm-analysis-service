"""API Router for Analytics v2."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_admin
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
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

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Analytics V2"])

METRICS_CATALOG = [
    {"key": "evaluacion_global", "label": "Evaluación global", "type": "score", "order": 10, "default_selected": True},
    {"key": "empatia", "label": "Empatía", "type": "score", "order": 20, "default_selected": True},
    {"key": "claridad", "label": "Claridad", "type": "score", "order": 30, "default_selected": True},
    {"key": "simpatia", "label": "Simpatía", "type": "score", "order": 40, "default_selected": True},
    {"key": "procedimiento", "label": "Procedimiento", "type": "score", "order": 50, "default_selected": True},
    {"key": "cierre_cita", "label": "Cierre de cita", "type": "percentage", "order": 60, "default_selected": False},
]

KEY_ALIASES = {
    "evaluacion_global": ["evaluacion_global", "global_score", "puntuacion_global"],
    "empatia": ["empatia"],
    "claridad": ["claridad"],
    "simpatia": ["simpatia", "tono_simpatia", "prueba_simpatia"],
    "procedimiento": ["procedimiento", "adherencia_procedimiento"],
    "cierre_cita": ["cierre_cita", "cita_resultado", "cita", "cierre"]
}

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
    aliases = KEY_ALIASES.get(key, [key])
    
    if key == "evaluacion_global":
        val = r.evaluacion_global
        if val is None:
            val = extract_score_from_mass(r.result_json, r.items_json, "evaluacion_global")
        return float(val) if val is not None else None

    # Find matching criterion in database rows if available
    match_row = next((c for c in criteria if c.criterion_key in aliases), None)
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

    # Fallback to result_json or items_json
    rj = r.result_json or {}
    for alias in aliases:
        if key == "cierre_cita":
            val = rj.get(alias)
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
            val = extract_score_from_mass(r.result_json, r.items_json, alias)
            if val is not None:
                return float(val)
                
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
    current_user: Annotated[User, Depends(require_admin)]
):
    """Retrieve the catalogue of compared metrics available in Analytics v2."""
    return METRICS_CATALOG


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
    current_user: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date_from: Annotated[str | None, Query(description="Start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="End date (ISO or YYYY-MM-DD)")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    agent_owner_ids: Annotated[list[str] | None, Query(description="Filter agent owner IDs")] = None,
    agent_owner_ids_bracket: Annotated[list[str] | None, Query(alias="agent_owner_ids[]", description="Filter agent owner IDs (array format)")] = None,
    item_keys: Annotated[list[str] | None, Query(description="Filter compared item keys")] = None,
    item_keys_bracket: Annotated[list[str] | None, Query(alias="item_keys[]", description="Filter compared item keys (array format)")] = None,
):
    """
    Retrieve agents performance comparison breakdown.
    Optimized to run exactly two SQL queries to avoid N+1 issues.
    """
    try:
        # 1. Resolve timeframe
        dt_from, dt_to, _ = resolve_date_range(date_from, date_to, period=None, default_period="30d")
        
        # 2. Build Mass Evaluation Results filter query
        stmt = select(MassEvaluationResult).where(MassEvaluationResult.status == "completed")
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

        owner_ids = parse_list_param(agent_owner_ids) + parse_list_param(agent_owner_ids_bracket)
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
        keys_to_use = parse_list_param(item_keys) + parse_list_param(item_keys_bracket)
        if keys_to_use:
            items_to_use = [item for item in METRICS_CATALOG if item["key"] in keys_to_use]
        else:
            items_to_use = METRICS_CATALOG

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
    current_user: Annotated[User, Depends(require_admin)],
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
):
    """
    Retrieve chronological evolution timeline for chosen analytics metrics.
    Grouped by time intervals (hour, day, or week).
    """
    try:
        # 1. Resolve timeframe
        dt_from, dt_to, recommended_bucket = resolve_date_range(date_from, date_to, period=None, default_period="30d")
        bucket_interval = bucket if bucket in ["hour", "day", "week"] else recommended_bucket

        # 2. Build Mass Evaluation Results filter query
        stmt = select(MassEvaluationResult).where(MassEvaluationResult.status == "completed")
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

        owner_ids = parse_list_param(agent_owner_ids) + parse_list_param(agent_owner_ids_bracket)
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
        keys_to_use = parse_list_param(item_keys) + parse_list_param(item_keys_bracket)
        if keys_to_use:
            items_to_use = [item for item in METRICS_CATALOG if item["key"] in keys_to_use]
        else:
            items_to_use = METRICS_CATALOG

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
                        count=count
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
