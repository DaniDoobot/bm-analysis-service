"""
Centralized Analytics Query Context & Filter Normalization Helper.

Provides a single source of truth for parsing dates, agents, service, typology,
and tenant scoping filters across all analytical endpoints and dashboard views.
"""
from datetime import datetime, timezone
from typing import Any, List, Optional, Union
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.utils.dates import safe_parse_datetime
from app.utils.normalizers import normalize_direction, normalize_typology


class AnalyticsQueryContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    created_from: Optional[datetime] = None
    created_to: Optional[datetime] = None
    date_field_used: str = "call_timestamp"
    
    agent_owner_id: Optional[str] = None
    agent_owner_ids: Optional[List[str]] = None
    
    service_id: Optional[int] = None
    service_key: Optional[str] = None
    
    typology_key: Optional[str] = None
    typology_ids: Optional[List[int]] = None
    
    direction: Optional[str] = None
    execution_source: Optional[str] = None
    
    global_score_min: Optional[float] = None
    global_score_max: Optional[float] = None

    company_ids: Optional[List[int]] = None
    service_ids: Optional[List[int]] = None
    allowed_agent_ids: Optional[List[str]] = None


def parse_date_bound_start(raw_val: Union[str, datetime, None]) -> Optional[datetime]:
    if raw_val is None:
        return None
    if isinstance(raw_val, datetime):
        return raw_val
    dt = safe_parse_datetime(str(raw_val))
    if dt and (isinstance(raw_val, str) and (len(raw_val.strip()) <= 10 or ":" not in raw_val)):
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt


def parse_date_bound_end(raw_val: Union[str, datetime, None]) -> Optional[datetime]:
    if raw_val is None:
        return None
    if isinstance(raw_val, datetime):
        # If passed as a naive datetime with midnight time component (00:00:00), treat as end of day
        if raw_val.hour == 0 and raw_val.minute == 0 and raw_val.second == 0 and raw_val.microsecond == 0:
            return raw_val.replace(hour=23, minute=59, second=59, microsecond=999999)
        return raw_val
    raw_str = str(raw_val).strip()
    dt = safe_parse_datetime(raw_str)
    if dt:
        if len(raw_str) <= 10 or ":" not in raw_str or (dt.hour == 0 and dt.minute == 0 and dt.second == 0):
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt


def build_analytics_query_context(
    context: Optional[TenantContext] = None,
    date_from: Union[str, datetime, None] = None,
    date_to: Union[str, datetime, None] = None,
    created_from: Union[str, datetime, None] = None,
    created_to: Union[str, datetime, None] = None,
    period: Optional[str] = None,
    agent_owner_id: Optional[str] = None,
    hubspot_owner_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    agent_owner_ids: Optional[List[str]] = None,
    service_id: Optional[int] = None,
    service_key: Optional[str] = None,
    typology_key: Optional[str] = None,
    selected_typology: Optional[str] = None,
    tipo_llamada: Optional[str] = None,
    typology_ids: Union[str, List[int], None] = None,
    direction: Optional[str] = None,
    call_direction: Optional[str] = None,
    inbound_outbound: Optional[str] = None,
    execution_source: Optional[str] = None,
    global_score_min: Optional[float] = None,
    global_score_max: Optional[float] = None,
) -> AnalyticsQueryContext:
    """
    Builds and validates a unified AnalyticsQueryContext.
    Enforces inclusive end-of-day date_to, agent parameter normalization,
    direction normalization, and tenant security scoping.
    """
    # 1. Date normalization
    p_date_from = parse_date_bound_start(date_from)
    p_date_to = parse_date_bound_end(date_to)
    
    # Single-day convenience: if date_from is given without date_to, default date_to to end of date_from
    if p_date_from and not p_date_to and date_from and isinstance(date_from, str) and (len(date_from.strip()) <= 10 or ":" not in date_from.strip()):
        p_date_to = p_date_from.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Date bounds validation
    if p_date_from and p_date_to and p_date_from > p_date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to"
        )

    p_created_from = parse_date_bound_start(created_from)
    p_created_to = parse_date_bound_end(created_to)
    if p_created_from and p_created_to and p_created_from > p_created_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="created_from cannot be greater than created_to"
        )

    # 2. Agent parameter normalization
    effective_agent = agent_owner_id or hubspot_owner_id or agent_id or owner_id
    if effective_agent:
        effective_agent = str(effective_agent).strip()
        
    effective_agents_list: Optional[List[str]] = None
    if agent_owner_ids:
        effective_agents_list = [str(a).strip() for a in agent_owner_ids if str(a).strip()]

    # 3. Typology & Direction normalization
    raw_typology = typology_key or selected_typology or tipo_llamada
    norm_typology = normalize_typology(raw_typology)

    parsed_typo_ids: Optional[List[int]] = None
    if isinstance(typology_ids, str) and typology_ids.strip():
        parsed_typo_ids = [int(x.strip()) for x in typology_ids.split(",") if x.strip().isdigit()]
    elif isinstance(typology_ids, list):
        parsed_typo_ids = [int(x) for x in typology_ids if str(x).isdigit()]

    raw_direction = direction or call_direction or inbound_outbound
    norm_d = normalize_direction(raw_direction)

    # 4. Global Score Range validation
    if global_score_min is not None and global_score_max is not None:
        if global_score_min > global_score_max:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="global_score_min cannot be greater than global_score_max"
            )

    # 5. Tenant Scoping
    company_ids: Optional[List[int]] = None
    service_ids: Optional[List[int]] = None
    allowed_agent_ids: Optional[List[str]] = None

    if context:
        if not context.is_super_admin:
            company_ids = context.allowed_company_ids
            service_ids = context.allowed_service_ids
            
            if context.normalized_role == InternalRole.AGENT:
                allowed_agent_ids = context.allowed_agent_ids or []
                if effective_agent and allowed_agent_ids and effective_agent not in allowed_agent_ids:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="No tienes permiso para consultar datos de este agente."
                    )
                if not effective_agent and allowed_agent_ids:
                    effective_agent = allowed_agent_ids[0]
            elif context.allowed_agent_ids is not None:
                allowed_agent_ids = context.allowed_agent_ids
                if effective_agent and effective_agent not in allowed_agent_ids:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="No tienes permiso para consultar datos de este agente."
                    )

    return AnalyticsQueryContext(
        date_from=p_date_from,
        date_to=p_date_to,
        created_from=p_created_from,
        created_to=p_created_to,
        date_field_used="call_timestamp",
        agent_owner_id=effective_agent,
        agent_owner_ids=effective_agents_list,
        service_id=service_id,
        service_key=service_key,
        typology_key=norm_typology,
        typology_ids=parsed_typo_ids,
        direction=norm_d,
        execution_source=execution_source,
        global_score_min=global_score_min,
        global_score_max=global_score_max,
        company_ids=company_ids,
        service_ids=service_ids,
        allowed_agent_ids=allowed_agent_ids,
    )
