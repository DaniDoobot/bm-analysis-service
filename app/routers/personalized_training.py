"""FastAPI router for personalized agent training, settings, admin overview, reports and simulations."""
import logging
import re
from typing import Annotated, List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, get_tenant_context
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.users import User
from app.models.personalized_training import TrainingAgentReport, TrainingAgentSetting
from app.schemas.personalized_training import (
    TrainingAgentSettingOut,
    TrainingAgentSettingUpdate,
    BulkSchedulerAgentUpdate,
    TrainingRunResponse,
    AgentOverviewItem,
    AgentDetailResponse,
    TrainingAgentReportOut,
    TrainingAgentReportBase,
    ManualGeneratePayload,
    TrainingSchedulerSettingOut,
    TrainingSchedulerSettingPatch,
    TrainingSchedulerCreate,
    TrainingSchedulerUpdate,
    TrainingSchedulerOut,
    CyclesTeamSummaryResponse,
    UpdateCycleObjectivesPayload,
    ApproveCycleResponse,
    ManualCycleCreateRequest,
)
from app.services.personalized_training_service import PersonalizedTrainingService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/training", tags=["Personalized Training"])


# ── Security Helpers ─────────────────────────────────────────────────────────

def enforce_admin_role(user: User):
    """Enforce that the logged-in user is an administrator, manager or team coordinator."""
    from app.core.roles import normalize_role, InternalRole
    norm_role = normalize_role(user.role)
    if norm_role not in [InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN, InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR]:
        logger.warning("Access denied: User ID %s does not have administrator/manager/coordinator role.", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol de administrador, responsable o coordinador para realizar esta operación."
        )


def enforce_agent_or_admin_ownership(user: User, hubspot_owner_id: str, context: Optional[TenantContext] = None):
    """
    Enforces that a user can only access their own data unless they are an admin, manager or team coordinator.
    """
    from app.core.roles import normalize_role, InternalRole
    norm_role = normalize_role(user.role)
    if norm_role in [InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN, InternalRole.SERVICE_MANAGER]:
        return  # Admins and Service Managers can see agent data in scope
    
    if norm_role == InternalRole.TEAM_COORDINATOR:
        if context and context.allowed_agent_ids is not None:
            if hubspot_owner_id in context.allowed_agent_ids:
                return
        logger.warning(
            "Access denied: Team Coordinator User ID %s tried to access agent %s outside team scope.",
            user.user_id, hubspot_owner_id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para ver el entrenamiento de agentes fuera de tus equipos."
        )

    if not user.hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID. Contacta con administración."
        )
        
    if user.hubspot_owner_id != hubspot_owner_id:
        logger.warning(
            "Access denied: User ID %s (agent %s) tried to access agent %s data.",
            user.user_id, user.hubspot_owner_id, hubspot_owner_id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para ver el entrenamiento de otros agentes."
        )


async def get_service_manager_agent_ids(db: AsyncSession, context: TenantContext) -> List[str]:
    """Fetch all hubspot_owner_ids of agents belonging to context.allowed_service_ids within context.company_id."""
    if not context.allowed_service_ids:
        return []

    from app.models.teams import UserServiceAssociation, AgentTeamAssociation, Team
    
    # 1. Agents with primary_service_id in allowed_service_ids
    stmt1 = select(User.hubspot_owner_id).where(
        User.company_id == context.company_id,
        User.primary_service_id.in_(context.allowed_service_ids),
        User.hubspot_owner_id != None,
        User.is_active == True,
        func.lower(User.role).in_(["agent", "agente"])
    )
    res1 = await db.execute(stmt1)
    agent_ids = set(res1.scalars().all())

    # 2. Agents associated via UserServiceAssociation
    stmt2 = select(User.hubspot_owner_id).join(
        UserServiceAssociation, User.user_id == UserServiceAssociation.user_id
    ).where(
        User.company_id == context.company_id,
        UserServiceAssociation.service_id.in_(context.allowed_service_ids),
        User.hubspot_owner_id != None,
        User.is_active == True,
        func.lower(User.role).in_(["agent", "agente"])
    )
    res2 = await db.execute(stmt2)
    agent_ids.update(res2.scalars().all())

    # 3. Agents associated via AgentTeamAssociation to Teams in allowed_service_ids
    stmt3 = select(User.hubspot_owner_id).join(
        AgentTeamAssociation, User.user_id == AgentTeamAssociation.user_id
    ).join(
        Team, AgentTeamAssociation.team_id == Team.team_id
    ).where(
        Team.company_id == context.company_id,
        Team.service_id.in_(context.allowed_service_ids),
        User.hubspot_owner_id != None,
        User.is_active == True,
        func.lower(User.role).in_(["agent", "agente"])
    )
    res3 = await db.execute(stmt3)
    agent_ids.update(res3.scalars().all())

    return list(agent_ids)


def sanitize_report_for_agent(report: dict) -> dict:
    """Removes sensitive prompt_text instructions from simulation prompts for agents."""
    if not report:
        return report
    
    if "prompts" in report and isinstance(report["prompts"], list):
        for p in report["prompts"]:
            if isinstance(p, dict):
                p["prompt_text"] = ""
    return report


async def verify_report_write_scope(
    db: AsyncSession,
    training_report_id: int,
    context: TenantContext
) -> TrainingAgentReport:
    """Verifies that a report exists and the user has permission to write/mutate it."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración o coordinación."
        )

    stmt_rep = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == training_report_id)
    res_rep = await db.execute(stmt_rep)
    report = res_rep.scalars().first()
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado."
        )

    if not context.is_super_admin:
        # Check company scoping
        if report.company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: El informe pertenece a otra empresa."
            )
        # Check agent scoping (Service Managers)
        if context.allowed_agent_ids is not None:
            if report.hubspot_owner_id not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: No tienes permisos sobre el agente de este informe."
                )

    return report


# ── Admin Endpoints ──────────────────────────────────────────────────────────

async def _resolve_training_agent_scope(
    db: AsyncSession,
    context: TenantContext,
    service_id: Optional[int] = None,
    team_id: Optional[int] = None,
    company_id: Optional[int] = None,
) -> Optional[List[str]]:
    """
    Validates cascade and resolves the allowed hubspot_owner_ids based on role, service_id, and team_id.
    Returns:
      - None if all agents in company are allowed (no agent-level restriction)
      - List[str] if scoped to specific agents (empty list [] if empty team/service or no agents in scope)
    """
    from app.utils.team_resolvers import (
        validate_team_service_cascade,
        get_team_assigned_owner_ids,
        get_service_assigned_owner_ids,
    )

    base_role_set: Optional[set[str]] = None
    if context.normalized_role == InternalRole.SERVICE_MANAGER:
        manager_agents = await get_service_manager_agent_ids(db, context)
        base_role_set = set(manager_agents)
    elif context.normalized_role == InternalRole.TEAM_COORDINATOR:
        coord_agents = context.allowed_agent_ids or []
        base_role_set = set(coord_agents)

    if service_id is not None or team_id is not None or company_id is not None:
        await validate_team_service_cascade(db, service_id=service_id, team_id=team_id, context=context, company_id=company_id)

    target_filter_set: Optional[set[str]] = None
    if team_id is not None:
        target_filter_set = await get_team_assigned_owner_ids(db, team_id=team_id, context=context, company_id=company_id)
    elif service_id is not None:
        target_filter_set = await get_service_assigned_owner_ids(db, service_id=service_id, context=context, company_id=company_id)

    if base_role_set is not None and target_filter_set is not None:
        return list(base_role_set.intersection(target_filter_set))
    elif base_role_set is not None:
        return list(base_role_set)
    elif target_filter_set is not None:
        return list(target_filter_set)
    return None


@router.get("/admin/settings", response_model=List[TrainingAgentSettingOut])
async def list_agent_settings(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    company_id: Annotated[Optional[int], Query(description="Filter by company ID")] = None,
    service_id: Annotated[Optional[int], Query(description="Filter by service ID")] = None,
    team_id: Annotated[Optional[int], Query(description="Filter by team ID")] = None,
    is_enabled: Annotated[Optional[bool], Query(description="Filter by is_enabled")] = None,
    include_in_scheduler: Annotated[Optional[bool], Query(description="Filter by include_in_scheduler")] = None,
    db: AsyncSession = Depends(get_db)
):
    """List all personalized training settings for agents (Admin/Company Admin/Service Manager/Team Coordinator)."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    if company_id is not None and not context.is_super_admin:
        if company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a otra empresa."
            )
    eff_company_id = company_id if company_id is not None else (None if context.is_super_admin else context.company_id)
    company_ids = [eff_company_id] if eff_company_id is not None else (None if context.is_super_admin else context.allowed_company_ids)

    allowed_agent_ids = await _resolve_training_agent_scope(
        db, context=context, service_id=service_id, team_id=team_id, company_id=eff_company_id
    )

    return await PersonalizedTrainingService.get_agent_settings(
        db,
        company_ids=company_ids,
        allowed_agent_ids=allowed_agent_ids,
        is_enabled=is_enabled,
        include_in_scheduler=include_in_scheduler
    )


@router.patch("/admin/settings/{hubspot_owner_id}", response_model=TrainingAgentSettingOut)
async def update_agent_setting(
    hubspot_owner_id: str,
    payload: TrainingAgentSettingUpdate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Update training setting for an agent (enable/disable, include_in_scheduler, codes) (Admin/Company Admin/Service Manager)."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración o coordinación."
        )

    # 1. Enforce agent-level restriction if manager/coordinator
    if context.normalized_role == InternalRole.SERVICE_MANAGER:
        sm_agents = await get_service_manager_agent_ids(db, context)
        if hubspot_owner_id not in sm_agents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para gestionar este agente."
            )
    elif context.allowed_agent_ids is not None:
        if hubspot_owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para gestionar este agente."
            )

    # 2. Resolve existing setting or agent's company to validate company scoping
    from app.models.personalized_training import TrainingAgentSetting
    stmt = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == hubspot_owner_id)
    res = await db.execute(stmt)
    existing = res.scalars().first()

    resolved_company_id = None
    if existing:
        resolved_company_id = existing.company_id
    else:
        # Resolve company from User table
        stmt_u = select(User.company_id).where(User.hubspot_owner_id == hubspot_owner_id)
        res_u = await db.execute(stmt_u)
        resolved_company_id = res_u.scalar()

    if resolved_company_id is None and not context.is_super_admin:
        resolved_company_id = context.company_id

    # Enforce company scoping
    if not context.is_super_admin:
        if resolved_company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: El agente pertenece a otra empresa."
            )

    try:
        setting = await PersonalizedTrainingService.update_agent_setting(
            db=db,
            hubspot_owner_id=hubspot_owner_id,
            is_enabled=payload.is_enabled,
            include_in_scheduler=payload.include_in_scheduler,
            agent_name=payload.agent_name,
            agent_initials=payload.agent_initials,
            training_code=payload.training_code,
            training_numeric_code=payload.training_numeric_code,
            training_code_enabled=payload.training_code_enabled,
            company_id=resolved_company_id
        )
        if not setting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No se encontró configuración para el agente {hubspot_owner_id}"
            )
        return setting
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.post("/admin/settings/bulk-scheduler")
async def bulk_update_scheduler_settings(
    payload: BulkSchedulerAgentUpdate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Bulk update include_in_scheduler for multiple agents (Admin/Company Admin/Service Manager/Team Coordinator)."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración o coordinación."
        )

    if not payload.hubspot_owner_ids:
        return {"updated_count": 0, "hubspot_owner_ids": []}

    # Validate agent-level restrictions
    if context.normalized_role == InternalRole.SERVICE_MANAGER:
        sm_agents = await get_service_manager_agent_ids(db, context)
        for oid in payload.hubspot_owner_ids:
            if oid not in sm_agents:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acceso denegado: No tienes permisos para gestionar el agente {oid}."
                )
    elif context.allowed_agent_ids is not None:
        for oid in payload.hubspot_owner_ids:
            if oid not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acceso denegado: No tienes permisos para gestionar el agente {oid}."
                )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_agent_ids = context.allowed_agent_ids if not context.is_super_admin else None

    count = await PersonalizedTrainingService.bulk_update_scheduler_settings(
        db=db,
        hubspot_owner_ids=payload.hubspot_owner_ids,
        include_in_scheduler=payload.include_in_scheduler,
        company_ids=company_ids,
        allowed_agent_ids=allowed_agent_ids
    )

    return {
        "updated_count": count,
        "include_in_scheduler": payload.include_in_scheduler,
        "hubspot_owner_ids": payload.hubspot_owner_ids
    }


@router.get("/admin/agents-overview", response_model=List[AgentOverviewItem])
async def list_agents_overview(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: Annotated[Optional[int], Query(description="Filter by service ID")] = None,
    team_id: Annotated[Optional[int], Query(description="Filter by team ID")] = None,
    company_id: Annotated[Optional[int], Query(description="Filter by company ID")] = None,
    db: AsyncSession = Depends(get_db)
):
    """Overview list of all active agents and their current training statuses."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    if company_id is not None and not context.is_super_admin:
        if company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a otra empresa."
            )
    eff_company_id = company_id if company_id is not None else (None if context.is_super_admin else context.company_id)
    company_ids = [eff_company_id] if eff_company_id is not None else (None if context.is_super_admin else context.allowed_company_ids)

    allowed_agent_ids = await _resolve_training_agent_scope(
        db, context=context, service_id=service_id, team_id=team_id, company_id=eff_company_id
    )

    return await PersonalizedTrainingService.get_agent_overview(
        db,
        company_ids=company_ids,
        allowed_agent_ids=allowed_agent_ids
    )


@router.get("/admin/cycles-summary", response_model=CyclesTeamSummaryResponse)
async def get_team_cycles_summary(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: Annotated[Optional[int], Query(description="Filter by service ID")] = None,
    team_id: Annotated[Optional[int], Query(description="Filter by team ID")] = None,
    company_id: Annotated[Optional[int], Query(description="Filter by company ID")] = None,
    db: AsyncSession = Depends(get_db)
):
    """Get team-wide training metrics, aggregates and priority targets."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    if company_id is not None and not context.is_super_admin:
        if company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a otra empresa."
            )
    eff_company_id = company_id if company_id is not None else (None if context.is_super_admin else context.company_id)
    company_ids = [eff_company_id] if eff_company_id is not None else (None if context.is_super_admin else context.allowed_company_ids)

    allowed_agent_ids = await _resolve_training_agent_scope(
        db, context=context, service_id=service_id, team_id=team_id, company_id=eff_company_id
    )

    return await PersonalizedTrainingService.get_cycles_team_summary(
        db,
        company_ids=company_ids,
        allowed_agent_ids=allowed_agent_ids
    )


@router.get("/admin/scheduler-settings", response_model=TrainingSchedulerSettingOut)
async def get_scheduler_settings(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the persistent personalized training scheduler configuration (Super Admin only)."""
    if not context.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Super Administrador para ver los ajustes globales del scheduler."
        )
    
    from app.config import get_settings
    settings = get_settings()
    
    db_settings = await PersonalizedTrainingService.get_or_create_scheduler_settings(db)
    
    # Inject runtime override status dynamically
    runtime_enabled = settings.enable_training_scheduler
    
    return TrainingSchedulerSettingOut(
        is_enabled=db_settings.is_enabled if runtime_enabled else False,
        interval_days=db_settings.interval_days,
        lookback_days=db_settings.lookback_days,
        last_run_at=db_settings.last_run_at,
        next_run_at=db_settings.next_run_at if runtime_enabled else None,
        last_status=db_settings.last_status,
        updated_at=db_settings.updated_at,
        runtime_enabled=runtime_enabled,
        reason=None if runtime_enabled else "Scheduler deshabilitado por variable de entorno"
    )


@router.patch("/admin/scheduler-settings", response_model=TrainingSchedulerSettingOut)
async def update_scheduler_settings(
    payload: TrainingSchedulerSettingPatch,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Modify the persistent personalized training scheduler configuration (Super Admin only)."""
    if not context.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Super Administrador para editar los ajustes globales del scheduler."
        )
    
    from app.config import get_settings
    settings = get_settings()
    
    db_settings = await PersonalizedTrainingService.update_scheduler_settings(
        db=db,
        is_enabled=payload.is_enabled,
        interval_days=payload.interval_days,
        lookback_days=payload.lookback_days,
        updated_by_email=context.user_email
    )
    
    runtime_enabled = settings.enable_training_scheduler
    
    return TrainingSchedulerSettingOut(
        is_enabled=db_settings.is_enabled if runtime_enabled else False,
        interval_days=db_settings.interval_days,
        lookback_days=db_settings.lookback_days,
        last_run_at=db_settings.last_run_at,
        next_run_at=db_settings.next_run_at if runtime_enabled else None,
        last_status=db_settings.last_status,
        updated_at=db_settings.updated_at,
        runtime_enabled=runtime_enabled,
        reason=None if runtime_enabled else "Scheduler deshabilitado por variable de entorno"
    )


# ── Granular Training Schedulers Endpoints ────────────────────────────────────

@router.get("/admin/schedulers", response_model=List[TrainingSchedulerOut])
async def list_schedulers(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    company_id: Annotated[Optional[int], Query(description="Filter by company ID")] = None,
    service_id: Annotated[Optional[int], Query(description="Filter by service ID")] = None,
    team_id: Annotated[Optional[int], Query(description="Filter by team ID")] = None,
    is_active: Annotated[Optional[bool], Query(description="Filter by active status")] = None,
    db: AsyncSession = Depends(get_db)
):
    """List all training schedulers within user's multitenant scope."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    if company_id is not None and not context.is_super_admin:
        if company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a otra empresa."
            )
    eff_company_id = company_id if company_id is not None else (None if context.is_super_admin else context.company_id)
    company_ids = [eff_company_id] if eff_company_id is not None else (None if context.is_super_admin else context.allowed_company_ids)

    if service_id is not None or team_id is not None or eff_company_id is not None:
        from app.utils.team_resolvers import validate_team_service_cascade
        await validate_team_service_cascade(
            db,
            service_id=service_id,
            team_id=team_id,
            context=context,
            company_id=eff_company_id
        )

    schedulers = await PersonalizedTrainingService.list_schedulers(
        db,
        company_ids=company_ids,
        service_id=service_id,
        team_id=team_id,
        is_active=is_active
    )
    return [TrainingSchedulerOut(**s) for s in schedulers]


@router.post("/admin/schedulers", response_model=TrainingSchedulerOut)
async def create_scheduler(
    payload: TrainingSchedulerCreate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Create a new training scheduler with optional agent assignments and hierarchy scope."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    target_company_id = payload.company_id
    if not context.is_super_admin:
        if target_company_id is not None and target_company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a otra empresa."
            )
        if target_company_id is None:
            target_company_id = context.company_id

    if payload.service_id is not None or payload.team_id is not None or target_company_id is not None:
        from app.utils.team_resolvers import validate_team_service_cascade
        await validate_team_service_cascade(
            db,
            service_id=payload.service_id,
            team_id=payload.team_id,
            context=context,
            company_id=target_company_id
        )

    if payload.hubspot_owner_ids:
        scoped_agent_ids = await _resolve_training_agent_scope(
            db,
            context=context,
            service_id=payload.service_id,
            team_id=payload.team_id,
            company_id=target_company_id
        )
        if scoped_agent_ids is not None:
            invalid_scope = set(payload.hubspot_owner_ids) - set(scoped_agent_ids)
            if invalid_scope:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acceso denegado: Agentes fuera del ámbito permitido: {sorted(list(invalid_scope))}"
                )

        if target_company_id is not None:
            stmt_chk = select(TrainingAgentSetting.hubspot_owner_id).where(
                TrainingAgentSetting.hubspot_owner_id.in_(payload.hubspot_owner_ids),
                TrainingAgentSetting.company_id == target_company_id
            )
            res_chk = await db.execute(stmt_chk)
            found_ids = set(res_chk.scalars().all())
            invalid_comp = set(payload.hubspot_owner_ids) - found_ids
            if invalid_comp:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Los siguientes agentes no pertenecen a la empresa indicada: {sorted(list(invalid_comp))}"
                )

    try:
        sch = await PersonalizedTrainingService.create_scheduler(
            db,
            name=payload.name,
            company_id=target_company_id,
            service_id=payload.service_id,
            team_id=payload.team_id,
            interval_days=payload.interval_days,
            lookback_days=payload.lookback_days,
            is_active=payload.is_active,
            hubspot_owner_ids=payload.hubspot_owner_ids
        )
        return TrainingSchedulerOut(**sch)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.patch("/admin/schedulers/{scheduler_id}", response_model=TrainingSchedulerOut)
async def update_scheduler(
    scheduler_id: int,
    payload: TrainingSchedulerUpdate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Update a training scheduler, interval, active status or assigned agents."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    sch = await PersonalizedTrainingService.get_scheduler_by_id(db, scheduler_id)
    if not sch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planificador no encontrado.")

    if not context.is_super_admin:
        sch_comp = sch.get("company_id")
        if sch_comp is not None and sch_comp not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a este planificador."
            )

    eff_srv = payload.service_id if payload.service_id is not None else sch.get("service_id")
    eff_team = payload.team_id if payload.team_id is not None else sch.get("team_id")
    eff_comp = sch.get("company_id")

    if payload.service_id is not None or payload.team_id is not None:
        from app.utils.team_resolvers import validate_team_service_cascade
        await validate_team_service_cascade(
            db,
            service_id=eff_srv,
            team_id=eff_team,
            context=context,
            company_id=eff_comp
        )

    if payload.hubspot_owner_ids is not None:
        scoped_agent_ids = await _resolve_training_agent_scope(
            db,
            context=context,
            service_id=eff_srv,
            team_id=eff_team,
            company_id=eff_comp
        )
        if scoped_agent_ids is not None:
            invalid_scope = set(payload.hubspot_owner_ids) - set(scoped_agent_ids)
            if invalid_scope:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acceso denegado: Agentes fuera del ámbito permitido: {sorted(list(invalid_scope))}"
                )

        if eff_comp is not None:
            stmt_chk = select(TrainingAgentSetting.hubspot_owner_id).where(
                TrainingAgentSetting.hubspot_owner_id.in_(payload.hubspot_owner_ids),
                TrainingAgentSetting.company_id == eff_comp
            )
            res_chk = await db.execute(stmt_chk)
            found_ids = set(res_chk.scalars().all())
            invalid_comp = set(payload.hubspot_owner_ids) - found_ids
            if invalid_comp:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Los siguientes agentes no pertenecen a la empresa del planificador: {sorted(list(invalid_comp))}"
                )

    try:
        updated = await PersonalizedTrainingService.update_scheduler(
            db,
            scheduler_id=scheduler_id,
            name=payload.name,
            service_id=payload.service_id,
            team_id=payload.team_id,
            interval_days=payload.interval_days,
            lookback_days=payload.lookback_days,
            is_active=payload.is_active,
            hubspot_owner_ids=payload.hubspot_owner_ids
        )
        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planificador no encontrado.")
        return TrainingSchedulerOut(**updated)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/admin/schedulers/{scheduler_id}")
async def delete_scheduler(
    scheduler_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Delete a training scheduler and remove its agent associations."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    sch = await PersonalizedTrainingService.get_scheduler_by_id(db, scheduler_id)
    if not sch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planificador no encontrado.")

    if not context.is_super_admin:
        sch_comp = sch.get("company_id")
        if sch_comp is not None and sch_comp not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a este planificador."
            )

    deleted = await PersonalizedTrainingService.delete_scheduler(db, scheduler_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planificador no encontrado.")
    return {"status": "deleted", "scheduler_id": scheduler_id}


@router.post("/admin/schedulers/{scheduler_id}/run")
async def run_scheduler_manually(
    scheduler_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Force an immediate execution of a training scheduler."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    sch = await PersonalizedTrainingService.get_scheduler_by_id(db, scheduler_id)
    if not sch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planificador no encontrado.")

    if not context.is_super_admin:
        sch_comp = sch.get("company_id")
        if sch_comp is not None and sch_comp not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a este planificador."
            )

    res = await PersonalizedTrainingService.execute_scheduler(db, scheduler=scheduler_id, force=True)
    return res or {"triggered": False, "scheduler_id": scheduler_id, "reason": "Execution skipped or no agents"}


@router.get("/admin/agents/{hubspot_owner_id}", response_model=AgentDetailResponse)
async def get_agent_detail_admin(
    hubspot_owner_id: str,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db)
):
    """Full detail of an agent's training, objectives, prompts, history and progress (Admin only)."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    # Validamos permisos sobre el hubspot_owner_id solicitado
    if context.allowed_agent_ids is not None:
        if hubspot_owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para acceder a este agente."
            )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    try:
        detail = await PersonalizedTrainingService.get_agent_detail(
            db, 
            hubspot_owner_id=hubspot_owner_id,
            include_archived=include_archived,
            include_pending_approval=True,
            company_ids=company_ids
        )
        if not detail:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agente {hubspot_owner_id} no encontrado en configuraciones o sin acceso."
            )
        # Manually validate with Pydantic to catch serialization errors before returning
        AgentDetailResponse.model_validate(detail)
        return detail
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed GET detail for agent %s: %s", hubspot_owner_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_message": "Fallo de serialización en el endpoint de detalle.",
                "exception_type": type(e).__name__,
                "exception_message": str(e)
            }
        )


@router.post("/admin/generate", response_model=TrainingRunResponse)
async def trigger_manual_generation(
    payload: ManualGeneratePayload,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Trigger manual personalized training generation for one or more agents."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración o coordinación."
        )

    target_owner_ids = payload.hubspot_owner_ids
    if not target_owner_ids and (payload.service_id is not None or payload.team_id is not None or payload.company_id is not None):
        eff_cid = payload.company_id if payload.company_id is not None else (None if context.is_super_admin else context.company_id)
        scoped_ids = await _resolve_training_agent_scope(
            db, context=context, service_id=payload.service_id, team_id=payload.team_id, company_id=eff_cid
        )
        if scoped_ids is not None:
            target_owner_ids = scoped_ids

    # Validate agent scoping if owner IDs were explicitly requested
    if target_owner_ids:
        # Enforce agent ID restrictions (Service Managers)
        if context.allowed_agent_ids is not None:
            for oid in target_owner_ids:
                if oid not in context.allowed_agent_ids:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"No tienes permiso para generar entrenamiento para el agente {oid}."
                    )
        # Enforce company restrictions (Company Admins)
        if not context.is_super_admin:
            stmt_u = select(User.company_id).where(User.hubspot_owner_id.in_(target_owner_ids))
            res_u = await db.execute(stmt_u)
            companies = list(res_u.scalars().all())
            for cid in companies:
                if cid not in context.allowed_company_ids:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Acceso denegado: Uno o más agentes pertenecen a otra empresa."
                    )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_agent_ids = context.allowed_agent_ids if not context.is_super_admin else None

    try:
        run = await PersonalizedTrainingService.run_personalized_training_pass(
            db=db,
            hubspot_owner_ids=target_owner_ids,
            period_start=payload.period_start,
            period_end=payload.period_end,
            triggered_by="manual",
            created_by_email=context.user_email,
            force_regenerate=payload.force_regenerate,
            company_ids=company_ids,
            allowed_agent_ids=allowed_agent_ids
        )
        return run
    except Exception as e:
        logger.exception("Failed manual generation pass: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo al ejecutar la generación manual: {str(e)}"
        )


@router.post("/admin/manual-cycle", response_model=List[TrainingAgentReportOut])
async def create_manual_cycle(
    payload: ManualCycleCreateRequest,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """
    Crea un ciclo de entrenamiento manual para uno o varios agentes.
    """
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Los agentes no pueden crear ciclos manuales."
        )

    if not payload.hubspot_owner_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debe proporcionar al menos un ID de agente."
        )

    # Atomic scope validation against TenantContext before creating any DB objects
    if context.normalized_role == InternalRole.SERVICE_MANAGER:
        sm_agents = await get_service_manager_agent_ids(db, context)
        for oid in payload.hubspot_owner_ids:
            if oid not in sm_agents:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acceso denegado: No tienes permiso para crear ciclos manuales para el agente {oid}."
                )
    elif context.allowed_agent_ids is not None:
        for oid in payload.hubspot_owner_ids:
            if oid not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acceso denegado: No tienes permiso para crear ciclos manuales para el agente {oid}."
                )

    if not context.is_super_admin and context.allowed_company_ids:
        from app.models.personalized_training import TrainingAgentSetting
        stmt_sets = select(TrainingAgentSetting.company_id, TrainingAgentSetting.hubspot_owner_id).where(
            TrainingAgentSetting.hubspot_owner_id.in_(payload.hubspot_owner_ids)
        )
        res_sets = await db.execute(stmt_sets)
        found_settings = {row.hubspot_owner_id: row.company_id for row in res_sets.all()}

        stmt_users = select(User.company_id, User.hubspot_owner_id).where(
            User.hubspot_owner_id.in_(payload.hubspot_owner_ids)
        )
        res_users = await db.execute(stmt_users)
        found_users = {row.hubspot_owner_id: row.company_id for row in res_users.all()}

        for oid in payload.hubspot_owner_ids:
            cid = found_settings.get(oid) or found_users.get(oid)
            if cid is not None and cid not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acceso denegado: El agente {oid} pertenece a otra empresa."
                )

    try:
        # Resolve general_objectives / specific_objectives with legacy fallback
        # If neither general_objectives nor specific_objectives are provided,
        # treat legacy 'objectives' as specific_objectives for backward compatibility.
        use_general = payload.general_objectives
        use_specific = payload.specific_objectives
        if use_general is None and use_specific is None and payload.objectives:
            use_specific = payload.objectives

        reports = await PersonalizedTrainingService.create_manual_cycles(
            db=db,
            hubspot_owner_ids=payload.hubspot_owner_ids,
            title=payload.title,
            general_objectives=use_general,
            specific_objectives=use_specific,
            service_id=payload.service_id,
            approved_by_user_id=context.user_id,
            created_by_email=context.user_email
        )
        return reports
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error al crear ciclo manual: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al crear ciclo manual: {str(e)}"
        )


# ── Agent / Self Endpoints (Me) ──────────────────────────────────────────────

@router.get("/me/current", response_model=TrainingAgentReportOut)
async def get_my_current_training(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the current training report, prompts and progress for the authenticated agent."""
    hubspot_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
    if not hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
        )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    detail = await PersonalizedTrainingService.get_agent_detail(
        db,
        hubspot_owner_id=hubspot_owner_id,
        company_ids=company_ids
    )
    if not detail or not detail.get("current_report"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontró ningún informe de entrenamiento actual disponible para tu usuario."
        )
    return sanitize_report_for_agent(detail["current_report"])


@router.get("/me/history", response_model=List[TrainingAgentReportBase])
async def get_my_training_history(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the list of historical training reports for the authenticated agent."""
    hubspot_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
    if not hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
        )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    detail = await PersonalizedTrainingService.get_agent_detail(
        db,
        hubspot_owner_id=hubspot_owner_id,
        include_archived=include_archived,
        company_ids=company_ids
    )
    if not detail:
        return []
    return detail["history"]


@router.get("/me/reports/{training_report_id}", response_model=TrainingAgentReportOut)
async def get_my_historical_report(
    training_report_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a specific historical training report for the authenticated agent."""
    hubspot_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
    if not hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
        )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    report_details = await PersonalizedTrainingService.get_report_by_id(
        db,
        report_id=training_report_id,
        company_ids=company_ids,
        allowed_agent_ids=[hubspot_owner_id]
    )
    if not report_details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado o sin acceso."
        )

    return sanitize_report_for_agent(report_details)


# ── Generic/Agent Parameterized Endpoints ─────────────────────────────────────

@router.get("/agents/{hubspot_owner_id}/current", response_model=TrainingAgentReportOut)
async def get_agent_current_training(
    hubspot_owner_id: str,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the current training report, prompts and progress for a specific agent."""
    if context.allowed_agent_ids is not None:
        if hubspot_owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para ver el entrenamiento de otros agentes."
            )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    detail = await PersonalizedTrainingService.get_agent_detail(
        db,
        hubspot_owner_id=hubspot_owner_id,
        company_ids=company_ids
    )
    if not detail or not detail.get("current_report"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró ningún informe de entrenamiento actual para el agente {hubspot_owner_id}."
        )
    report_data = detail["current_report"]
    if context.normalized_role == InternalRole.AGENT:
        report_data = sanitize_report_for_agent(report_data)
    return report_data


@router.get("/agents/{hubspot_owner_id}/history", response_model=List[TrainingAgentReportBase])
async def get_agent_training_history(
    hubspot_owner_id: str,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve all historical training reports for a specific agent."""
    if context.allowed_agent_ids is not None:
        if hubspot_owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para ver el entrenamiento de otros agentes."
            )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    detail = await PersonalizedTrainingService.get_agent_detail(
        db,
        hubspot_owner_id=hubspot_owner_id,
        include_archived=include_archived,
        company_ids=company_ids
    )
    if not detail:
        return []
    return detail["history"]


@router.get("/reports/{training_report_id}", response_model=TrainingAgentReportOut)
async def get_report_by_id(
    training_report_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve detail of a specific training report by ID."""
    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_agent_ids = context.allowed_agent_ids if not context.is_super_admin else None

    report_details = await PersonalizedTrainingService.get_report_by_id(
        db,
        report_id=training_report_id,
        company_ids=company_ids,
        allowed_agent_ids=allowed_agent_ids
    )
    if not report_details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado o sin acceso."
        )

    if context.normalized_role == InternalRole.AGENT:
        report_details = sanitize_report_for_agent(report_details)
    return report_details


@router.post("/admin/reports/{training_report_id}/archive", response_model=TrainingAgentReportOut)
async def archive_training_report(
    training_report_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Soft-delete / archive a training report so it no longer counts in active/pending stats (Admin/Company Admin/Service Manager)."""
    await verify_report_write_scope(db, training_report_id, context)
    report = await PersonalizedTrainingService.archive_report(db, report_id=training_report_id)
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado."
        )
    return report


@router.patch("/admin/reports/{training_report_id}/objectives", response_model=TrainingAgentReportOut)
async def update_cycle_objectives(
    training_report_id: int,
    payload: UpdateCycleObjectivesPayload,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Edit general and/or specific objectives of a training cycle in 'pending_approval' status (Admin/Company Admin/Service Manager)."""
    await verify_report_write_scope(db, training_report_id, context)
    try:
        report = await PersonalizedTrainingService.update_cycle_objectives(
            db=db,
            report_id=training_report_id,
            general_objectives_json=payload.general_objectives_json,
            specific_objectives_json=payload.specific_objectives_json,
        )
        report_dict = await PersonalizedTrainingService.get_report_by_id(db, report_id=training_report_id)
        if not report_dict:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Informe no encontrado tras actualizar.")
        return report_dict
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update objectives for report %d: %s", training_report_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al actualizar los objetivos: {str(e)}"
        )


@router.post("/admin/reports/{training_report_id}/approve", response_model=ApproveCycleResponse)
async def approve_training_cycle(
    training_report_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Approve a training cycle in 'pending_approval' status (Admin/Company Admin/Service Manager)."""
    await verify_report_write_scope(db, training_report_id, context)
    try:
        report = await PersonalizedTrainingService.approve_training_cycle(
            db=db,
            report_id=training_report_id,
            approved_by_user_id=context.user_id,
        )
        # Count prompts created
        from sqlalchemy import func as sqlfunc
        from app.models.personalized_training import TrainingSimulationPrompt
        stmt_count = select(sqlfunc.count()).where(
            TrainingSimulationPrompt.training_report_id == training_report_id
        )
        res_count = await db.execute(stmt_count)
        prompts_count = res_count.scalar() or 0

        return ApproveCycleResponse(
            training_report_id=report.training_report_id,
            status=report.status,
            approved_at=report.approved_at,
            approved_by_user_id=report.approved_by_user_id,
            prompts_generated=prompts_count,
            message=(
                f"Ciclo ID {training_report_id} aprobado correctamente. "
                f"{prompts_count} prompts de simulación generados. "
                f"El ciclo es ahora visible para el agente."
            )
        )
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to approve cycle %d: %s", training_report_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al aprobar el ciclo: {str(e)}"
        )


@router.delete("/admin/reports/{training_report_id}/hard-delete")
async def hard_delete_training_report(
    training_report_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Hard-delete a training report and all its prompts and completions from the database (Admin/Company Admin/Service Manager)."""
    await verify_report_write_scope(db, training_report_id, context)
    success = await PersonalizedTrainingService.hard_delete_report(db, report_id=training_report_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado."
        )
    return {"message": f"Informe de entrenamiento ID {training_report_id} eliminado físicamente con éxito."}


@router.get("/admin/evaluations/{evaluation_id}")
async def get_evaluation_detail(
    evaluation_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the full detail of a training call evaluation, including score,
    feedback, transcription (formatted as conversation turns), and the
    result_json criteria checklist.
    Accessible by authorized administrators, managers, coordinators, and the owner agent.
    """
    from app.models.personalized_training import TrainingCallEvaluation, TrainingCallSession

    stmt = select(TrainingCallEvaluation).where(TrainingCallEvaluation.evaluation_id == evaluation_id)
    res = await db.execute(stmt)
    ev = res.scalars().first()

    if not ev:
        raise HTTPException(status_code=404, detail=f"Evaluación {evaluation_id} no encontrada.")

    # Ownership check: admin can see all; agents can only see their own
    if not context.is_super_admin:
        stmt_sess = select(TrainingCallSession).where(TrainingCallSession.session_id == ev.session_id)
        res_sess = await db.execute(stmt_sess)
        session = res_sess.scalars().first()
        if not session:
            raise HTTPException(status_code=403, detail="No tienes acceso a esta evaluación.")
        
        if context.allowed_agent_ids is not None:
            if session.agent_id not in context.allowed_agent_ids:
                raise HTTPException(status_code=403, detail="No tienes acceso a esta evaluación.")

        if context.allowed_company_ids:
            # Check company
            stmt_rep = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == session.cycle_id)
            res_rep = await db.execute(stmt_rep)
            rep = res_rep.scalars().first()
            if not rep or rep.company_id not in context.allowed_company_ids:
                raise HTTPException(status_code=403, detail="No tienes acceso a esta evaluación.")

    # Parse transcription into conversation turns for easy rendering
    turns = []
    if ev.transcription:
        for line in ev.transcription.split("\n"):
            line = line.strip()
            if not line:
                continue
            cleaned_line = re.sub(r"^\[Turno\s*\d+\]\s*", "", line, flags=re.IGNORECASE).strip()
            cleaned_line = re.sub(r"^Turno\s*\d+\s*[:-]\s*", "", cleaned_line, flags=re.IGNORECASE).strip()
            if cleaned_line.startswith("Agente:"):
                turns.append({"role": "agent", "text": cleaned_line[len("Agente:"):].strip()})
            elif cleaned_line.startswith("Paciente:"):
                turns.append({"role": "patient", "text": cleaned_line[len("Paciente:"):].strip()})
            elif cleaned_line.startswith("Cliente:"):
                turns.append({"role": "patient", "text": cleaned_line[len("Cliente:"):].strip()})
            elif cleaned_line.startswith("Usuario:"):
                turns.append({"role": "patient", "text": cleaned_line[len("Usuario:"):].strip()})
            else:
                turns.append({"role": "unknown", "text": cleaned_line})

    # Extract criteria checklist from result_json (handles nested structure)
    criteria = {}
    criteria_evaluations = None
    if ev.result_json and isinstance(ev.result_json, dict):
        raw = ev.result_json
        # Handle nested: { ..., result_json: { key: bool } }
        inner = raw.get("result_json") if isinstance(raw.get("result_json"), dict) else None
        if isinstance(inner, dict):
            criteria = inner
        else:
            # Flat structure — pick only boolean values as criteria
            criteria = {k: v for k, v in raw.items() if isinstance(v, bool)}

        crit_evals = raw.get("criteria_evaluations")
        if isinstance(crit_evals, list):
            criteria_evaluations = crit_evals

    strengths, weaknesses = PersonalizedTrainingService._extract_strengths_weaknesses(ev.result_json or {})

    return {
        "evaluation_id": ev.evaluation_id,
        "session_id": ev.session_id,
        "cycle_id": ev.cycle_id,
        "score": float(ev.score) if ev.score is not None else None,
        "feedback": ev.feedback,
        "transcription_raw": ev.transcription,
        "transcription_turns": turns,
        "criteria": criteria,
        "criteria_evaluations": criteria_evaluations,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "objectives_met": strengths,
        "areas_for_improvement": weaknesses,
        "result_json": ev.result_json,
        "created_at": ev.created_at,
    }

