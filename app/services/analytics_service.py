"""Service for advanced analytic metrics, items, comparisons, and evolution."""
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Set, Tuple
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.models.mass_evaluations import MassEvaluationResult
from app.models.services import Service
from app.models.typologies import Typology
from app.models.prompts import Prompt, PromptBaseStructure
from app.models.criteria import PromptCriterion
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting
from app.utils.hubspot_owners import OWNER_TO_NAME, resolve_owner_name, resolve_agent_display
from app.services.dashboard_service import (
    extract_score_from_mass,
    to_float,
    _effective_ts,
    resolve_date_range
)

logger = logging.getLogger(__name__)

DEFAULT_KEY_ITEMS = ["claridad", "empatia", "procedimiento", "saludo_inicio", "cierre_cita"]


def get_initials(name: str) -> str:
    parts = name.strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    elif len(parts) == 1:
        return parts[0][:2].upper()
    return "??"


async def get_all_agents(db: AsyncSession) -> Dict[str, Dict[str, str]]:
    """Retrieve all agents in the system (static list + db users + training settings)."""
    agents = {}

    # 1. Static mapping
    for oid, name in OWNER_TO_NAME.items():
        agents[oid] = {
            "name": name,
            "initials": get_initials(name)
        }

    # 2. Database users (role is agent/agente)
    res = await db.execute(select(User).where(User.role.in_(["agent", "agente"])))
    for u in res.scalars().all():
        oid = u.hubspot_owner_id
        if oid:
            name = u.name or u.username or resolve_owner_name(oid) or oid
            agents[oid] = {
                "name": name,
                "initials": u.agent_initials or get_initials(name)
            }

    # 3. Training Agent Settings
    res_t = await db.execute(select(TrainingAgentSetting).where(TrainingAgentSetting.is_enabled == True))
    for t in res_t.scalars().all():
        oid = t.hubspot_owner_id
        if oid:
            agents[oid] = {
                "name": t.agent_name or oid,
                "initials": t.agent_initials or get_initials(t.agent_name)
            }

    return agents


async def resolve_service_id(db: AsyncSession, service_str: str | None) -> int | None:
    """Helper to resolve service string (id/key/name) to service_id."""
    if not service_str:
        return None
    try:
        # Try if it's an ID
        service_id = int(service_str)
        return service_id
    except ValueError:
        pass

    # Try lookup by key or name
    stmt = select(Service).where(
        (func.lower(Service.service_key) == service_str.lower()) |
        (func.lower(Service.service_name) == service_str.lower())
    )
    res = await db.execute(stmt)
    s = res.scalars().first()
    return s.service_id if s else None


async def get_analytics_items(db: AsyncSession, service_str: str | None = None) -> List[Dict[str, Any]]:
    """Retrieve all evaluable items for a service, marking 5 default items as selected."""
    service_id = await resolve_service_id(db, service_str)

    # Query criteria keys from PromptCriterion
    stmt = select(
        PromptCriterion.criterion_key,
        PromptCriterion.criterion_name,
        PromptCriterion.criterion_type
    ).join(
        Prompt, Prompt.prompt_id == PromptCriterion.prompt_id
    ).where(
        PromptCriterion.is_active == True,
        PromptCriterion.deleted_at.is_(None)
    )

    if service_id is not None:
        stmt = stmt.where(Prompt.service_id == service_id)

    res = await db.execute(stmt)
    rows = res.all()

    # De-duplicate criteria by key
    items_map = {}
    
    # Fallback/standard items to ensure catalog population
    from app.services.dashboard_service import CRITERIA_NAMES
    for key, name in CRITERIA_NAMES.items():
        # Map criterion keys to types
        m_type = "score"
        if key in ["cierre_cita", "cierre_cita_rate"]:
            m_type = "percentage"
        items_map[key] = {
            "key": key,
            "label": name,
            "type": m_type,
            "default_selected": False,
            "order": 999
        }

    # Add dynamic criteria
    for key, name, c_type in rows:
        if not key:
            continue
        # Map DB criterion types to our standard analytical types
        m_type = "score"
        if c_type in ["percentage", "boolean"]:
            m_type = "percentage"
        elif c_type == "number":
            m_type = "count"
        elif c_type == "category":
            m_type = "category"

        items_map[key] = {
            "key": key,
            "label": name or key.replace("_", " ").capitalize(),
            "type": m_type,
            "default_selected": False,
            "order": 100
        }

    items_list = list(items_map.values())
    
    # Determine the default selected items (prioritize: clarity, empathy, procedimiento, saludo_inicio, cierre_cita)
    default_keys = [k for k in DEFAULT_KEY_ITEMS if k in items_map]
    
    # If we have less than 5 from the priority list, add others
    if len(default_keys) < 5:
        for it in items_list:
            if it["key"] not in default_keys and len(default_keys) < 5:
                default_keys.append(it["key"])

    for it in items_list:
        if it["key"] in default_keys:
            it["default_selected"] = True

    # Order items list: default_selected first, then by order, then by label
    items_list.sort(key=lambda x: (not x["default_selected"], x["order"], x["label"]))
    
    # Add ordered indices
    for idx, it in enumerate(items_list):
        it["order"] = idx + 1

    return items_list


async def query_mass_evaluation_results(
    db: AsyncSession,
    agent_owner_ids: List[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    service_str: str | None = None
) -> List[MassEvaluationResult]:
    """Query mass evaluation results based on standard analytical filters."""
    service_id = await resolve_service_id(db, service_str)

    stmt = select(MassEvaluationResult).where(
        MassEvaluationResult.status == "completed"
    )

    if agent_owner_ids:
        stmt = stmt.where(MassEvaluationResult.hubspot_owner_id.in_(agent_owner_ids))

    if service_id is not None:
        stmt = stmt.where(MassEvaluationResult.service_id == service_id)
    elif service_str:
        # Fallback to key matching if service is string and not resolved to id
        stmt = stmt.where(MassEvaluationResult.service_key == service_str)

    dt_from, dt_to, _ = resolve_date_range(date_from, date_to, period=None, default_period="30d")
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

    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_agents_comparison_analytics(
    db: AsyncSession,
    agent_owner_ids: List[str] | None = None,
    item_keys: List[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    service_str: str | None = None
) -> List[Dict[str, Any]]:
    """Get flat list of agent-item comparisons, guaranteeing all agents are represented."""
    results = await query_mass_evaluation_results(db, agent_owner_ids, date_from, date_to, service_str)
    
    # 1. Resolve all agents to return
    all_agents_map = await get_all_agents(db)
    if agent_owner_ids:
        target_agents = {oid: info for oid, info in all_agents_map.items() if oid in agent_owner_ids}
    else:
        target_agents = all_agents_map

    # 2. Resolve items/criterias
    available_items = await get_analytics_items(db, service_str)
    available_items_map = {it["key"]: it for it in available_items}
    
    if item_keys:
        target_items = [available_items_map[k] for k in item_keys if k in available_items_map]
    else:
        # Return all available items
        target_items = available_items

    flat_comparisons = []

    for oid, agent_info in target_agents.items():
        # Get results for this agent
        agent_rows = [r for r in results if r.hubspot_owner_id == oid]
        agent_has_data = len(agent_rows) > 0

        for item in target_items:
            key = item["key"]
            label = item["label"]
            m_type = item["type"]

            if agent_has_data:
                # Extract values for this criterion
                scores = []
                for r in agent_rows:
                    val = extract_score_from_mass(r.result_json, r.items_json, key)
                    if val is not None:
                        scores.append(to_float(val))

                if scores:
                    value = to_float(round(sum(scores) / len(scores), 1))
                    count = len(scores)
                    has_data = True
                else:
                    value = None
                    count = 0
                    has_data = False
            else:
                value = None
                count = 0
                has_data = False

            flat_comparisons.append({
                "agent_id": oid,
                "agent_name": agent_info["name"],
                "agent_initials": agent_info["initials"],
                "item_key": key,
                "item_label": label,
                "metric_type": m_type,
                "value": value,
                "count": count,
                "has_data": has_data
            })

    return flat_comparisons


async def get_items_evolution_analytics(
    db: AsyncSession,
    agent_owner_ids: List[str] | None = None,
    item_keys: List[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    service_str: str | None = None,
    bucket: str | None = None
) -> Dict[str, Any]:
    """Get time-series evolution for evaluable items, grouped by date bucket."""
    results = await query_mass_evaluation_results(db, agent_owner_ids, date_from, date_to, service_str)
    
    # Resolve items/criterias
    available_items = await get_analytics_items(db, service_str)
    available_items_map = {it["key"]: it for it in available_items}
    
    if item_keys:
        target_items = [available_items_map[k] for k in item_keys if k in available_items_map]
    else:
        # Default to default_selected items
        target_items = [it for it in available_items if it["default_selected"]]

    # Generate time buckets
    dt_from, dt_to, recommended_bucket = resolve_date_range(date_from, date_to, period=None, default_period="30d")
    bucket_interval = bucket if bucket in ["hour", "day", "week", "month"] else recommended_bucket

    # Build chronological buckets list
    buckets = []
    if bucket_interval == "hour":
        curr = dt_from.replace(minute=0, second=0, microsecond=0)
        while curr <= dt_to:
            buckets.append(curr)
            curr += timedelta(hours=1)
    elif bucket_interval == "week":
        curr = dt_from.replace(hour=0, minute=0, second=0, microsecond=0)
        curr = curr - timedelta(days=curr.weekday())
        while curr <= dt_to:
            buckets.append(curr)
            curr += timedelta(days=7)
    elif bucket_interval == "month":
        curr = dt_from.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while curr <= dt_to:
            buckets.append(curr)
            # increment month
            if curr.month == 12:
                curr = curr.replace(year=curr.year + 1, month=1)
            else:
                curr = curr.replace(month=curr.month + 1)
    else:  # day
        curr = dt_from.replace(hour=0, minute=0, second=0, microsecond=0)
        while curr <= dt_to:
            buckets.append(curr)
            curr += timedelta(days=1)

    series_list = []

    for item in target_items:
        key = item["key"]
        label = item["label"]
        points = []

        for b_dt in buckets:
            if bucket_interval == "hour":
                b_key = b_dt.strftime("%Y-%m-%d %H:00")
            elif bucket_interval == "month":
                b_key = b_dt.strftime("%Y-%m")
            else:  # day or week
                b_key = b_dt.strftime("%Y-%m-%d")

            # Filter rows for this bucket
            b_rows = []
            for r in results:
                ts = _effective_ts(r)
                if not ts:
                    continue
                if bucket_interval == "hour":
                    row_key = ts.strftime("%Y-%m-%d %H:00")
                elif bucket_interval == "month":
                    row_key = ts.strftime("%Y-%m")
                elif bucket_interval == "week":
                    row_key = (ts - timedelta(days=ts.weekday())).strftime("%Y-%m-%d")
                else:
                    row_key = ts.strftime("%Y-%m-%d")

                if row_key == b_key:
                    b_rows.append(r)

            # Compute average score and count for the item
            scores = []
            for r in b_rows:
                val = extract_score_from_mass(r.result_json, r.items_json, key)
                if val is not None:
                    scores.append(to_float(val))

            if scores:
                value = to_float(round(sum(scores) / len(scores), 1))
                count = len(scores)
            else:
                value = None
                count = 0

            points.append({
                "date": b_key,
                "value": value,
                "count": count
            })

        series_list.append({
            "item_key": key,
            "item_label": label,
            "points": points
        })

    # Available items mapping
    evaluable_items_res = [
        {
            "key": it["key"],
            "label": it["label"],
            "type": it["type"],
            "default_selected": it["default_selected"],
            "order": it["order"]
        } for it in available_items
    ]

    return {
        "available_items": evaluable_items_res,
        "series": series_list
    }


async def get_global_kpis(
    db: AsyncSession,
    agent_owner_ids: List[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    service_str: str | None = None
) -> Dict[str, Dict[str, Any]]:
    """Compute aggregate global KPIs including counts and types."""
    results = await query_mass_evaluation_results(db, agent_owner_ids, date_from, date_to, service_str)

    # 1. Global score (evaluacion_global)
    evals = []
    for r in results:
        v = extract_score_from_mass(r.result_json, r.items_json, "evaluacion_global")
        if v is not None:
            evals.append(to_float(v))

    global_score = {
        "value": to_float(round(sum(evals) / len(evals), 1)) if evals else None,
        "count": len(evals),
        "metric_type": "score",
        "has_data": len(evals) > 0
    }

    # 2. Sentiment score
    sents = []
    for r in results:
        v = extract_score_from_mass(r.result_json, r.items_json, "sentiment")
        if v is not None:
            sents.append(to_float(v))

    sentiment = {
        "value": to_float(round(sum(sents) / len(sents), 1)) if sents else None,
        "count": len(sents),
        "metric_type": "score",
        "has_data": len(sents) > 0
    }

    # 3. Closing rate (cierre_cita_rate / percentage)
    citas = sum(1 for r in results if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") == "cita")
    total_tipo = sum(1 for r in results if r.result_json and isinstance(r.result_json, dict) and r.result_json.get("tipo_llamada") is not None)
    
    closing_rate = {
        "value": to_float(round((citas / total_tipo) * 100)) if total_tipo > 0 else None,
        "count": total_tipo,
        "metric_type": "percentage",
        "has_data": total_tipo > 0
    }

    return {
        "global_score": global_score,
        "sentiment": sentiment,
        "closing_rate": closing_rate
    }
