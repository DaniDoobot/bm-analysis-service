"""
Admin Teams Router — /bm/admin/teams

Provides CRUD and membership management for teams with full hierarchical permission enforcement.

Permissions:
  - super_admin:          full access to all teams
  - company_admin:        full access to teams in their company
  - service_manager:      read-only access to teams in their allowed services
  - team_coordinator:     read-only access to their assigned teams; can manage agents in their teams
  - agent/user:           no access (403)
"""
import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_tenant_context
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import AgentTeamAssociation, Team, UserTeamAssociation
from app.models.users import User
from app.core.roles import InternalRole, normalize_role
from app.core.tenant_context import TenantContext
from app.schemas.multitenancy import (
    AdminTeamCreate,
    AdminTeamMemberResponse,
    AdminTeamResponse,
    TeamUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/admin/teams", tags=["Admin Teams"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_admin_team_response(team: Team, db: AsyncSession) -> AdminTeamResponse:
    """Enrich a Team ORM object with company_name, service_name and member counts."""
    company_name: Optional[str] = None
    service_name: Optional[str] = None

    if team.company_id:
        comp_res = await db.execute(
            select(Company.company_name).where(Company.company_id == team.company_id)
        )
        company_name = comp_res.scalar()

    if team.service_id:
        svc_res = await db.execute(
            select(Service.service_name).where(Service.service_id == team.service_id)
        )
        service_name = svc_res.scalar()

    agent_count_res = await db.execute(
        select(func.count()).select_from(AgentTeamAssociation).where(AgentTeamAssociation.team_id == team.team_id)
    )
    agent_count = agent_count_res.scalar() or 0

    coord_count_res = await db.execute(
        select(func.count()).select_from(UserTeamAssociation).where(UserTeamAssociation.team_id == team.team_id)
    )
    coordinator_count = coord_count_res.scalar() or 0

    return AdminTeamResponse(
        team_id=team.team_id,
        team_name=team.team_name,
        company_id=team.company_id,
        company_name=company_name,
        service_id=team.service_id,
        service_name=service_name,
        is_active=team.is_active,
        agent_count=agent_count,
        coordinator_count=coordinator_count,
        created_at=team.created_at,
        updated_at=team.updated_at,
    )


def _check_write_permission(context: TenantContext, team: Optional[Team] = None):
    """Raises 403 if the actor cannot write teams (create/update)."""
    role = context.normalized_role
    if role not in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de Administrador de Empresa o Super Administrador.",
        )
    if team is not None and not context.is_super_admin:
        if team.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: el equipo pertenece a otra empresa.",
            )


def _check_read_permission(context: TenantContext, team: Team) -> bool:
    """Returns True if actor can read this team, raises 403 otherwise."""
    if context.is_super_admin:
        return True

    role = context.normalized_role

    if role == InternalRole.COMPANY_ADMIN:
        if team.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: el equipo pertenece a otra empresa.",
            )
        return True

    if role == InternalRole.SERVICE_MANAGER:
        if context.allowed_service_ids is not None and team.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este equipo no pertenece a tus servicios.",
            )
        return True

    if role == InternalRole.TEAM_COORDINATOR:
        if context.allowed_team_ids is not None and team.team_id not in context.allowed_team_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: no eres coordinador de este equipo.",
            )
        return True

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Acceso denegado: los agentes no pueden gestionar equipos.",
    )


def _check_agent_management_permission(context: TenantContext, team: Team):
    """Only super_admin, company_admin, or team_coordinator (of this team) can manage agents."""
    role = context.normalized_role
    if context.is_super_admin:
        return
    if role == InternalRole.COMPANY_ADMIN:
        if team.company_id != context.company_id:
            raise HTTPException(status_code=403, detail="Acceso denegado: equipo de otra empresa.")
        return
    if role == InternalRole.TEAM_COORDINATOR:
        if context.allowed_team_ids is not None and team.team_id not in context.allowed_team_ids:
            raise HTTPException(status_code=403, detail="Acceso denegado: no eres coordinador de este equipo.")
        return
    raise HTTPException(status_code=403, detail="Acceso denegado para gestionar agentes.")


# ---------------------------------------------------------------------------
# GET /bm/admin/teams — List teams
# ---------------------------------------------------------------------------

@router.get("", response_model=List[AdminTeamResponse])
async def list_admin_teams(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    company_id: Optional[int] = Query(None, description="Filtrar por empresa (solo super_admin puede filtrar otras empresas)"),
    service_id: Optional[int] = Query(None, description="Filtrar por servicio"),
    is_active: Optional[bool] = Query(None, description="Filtrar por estado activo/inactivo"),
):
    """List teams accessible by the actor with optional filters. Returns enriched response."""
    role = context.normalized_role

    if role == InternalRole.AGENT:
        raise HTTPException(status_code=403, detail="Acceso denegado: los agentes no pueden ver equipos.")

    stmt = select(Team)

    if context.is_super_admin:
        if company_id is not None:
            stmt = stmt.where(Team.company_id == company_id)
    elif role == InternalRole.COMPANY_ADMIN:
        if company_id is not None and company_id != context.company_id:
            raise HTTPException(status_code=403, detail="Acceso denegado a equipos de otra empresa.")
        stmt = stmt.where(Team.company_id == context.company_id)
    elif role == InternalRole.SERVICE_MANAGER:
        stmt = stmt.where(Team.company_id == context.company_id)
        if context.allowed_service_ids is not None:
            stmt = stmt.where(Team.service_id.in_(context.allowed_service_ids))
    elif role == InternalRole.TEAM_COORDINATOR:
        if context.allowed_team_ids is not None:
            stmt = stmt.where(Team.team_id.in_(context.allowed_team_ids))

    if service_id is not None:
        stmt = stmt.where(Team.service_id == service_id)
    if is_active is not None:
        stmt = stmt.where(Team.is_active == is_active)

    stmt = stmt.order_by(Team.team_name)
    res = await db.execute(stmt)
    teams = list(res.scalars().all())

    results = []
    for team in teams:
        results.append(await _build_admin_team_response(team, db))
    return results


# ---------------------------------------------------------------------------
# GET /bm/admin/teams/{team_id}
# ---------------------------------------------------------------------------

@router.get("/{team_id}", response_model=AdminTeamResponse)
async def get_admin_team(
    team_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get detail of a specific team (enriched)."""
    res = await db.execute(select(Team).where(Team.team_id == team_id))
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado.")
    _check_read_permission(context, team)
    return await _build_admin_team_response(team, db)


# ---------------------------------------------------------------------------
# POST /bm/admin/teams — Create team
# ---------------------------------------------------------------------------

@router.post("", response_model=AdminTeamResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_team(
    payload: AdminTeamCreate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new team. Requires company_admin or super_admin."""
    _check_write_permission(context)

    # Company admins can only create in their own company
    if not context.is_super_admin and payload.company_id != context.company_id:
        raise HTTPException(status_code=403, detail="No tienes permisos para crear equipos en otra empresa.")

    # Validate company exists
    comp_res = await db.execute(select(Company).where(Company.company_id == payload.company_id))
    if not comp_res.scalar():
        raise HTTPException(status_code=400, detail="La empresa especificada no existe.")

    # Validate service exists and belongs to that company
    svc_res = await db.execute(
        select(Service).where(
            (Service.service_id == payload.service_id) &
            (Service.company_id == payload.company_id)
        )
    )
    if not svc_res.scalar():
        raise HTTPException(
            status_code=400,
            detail="El servicio especificado no existe o no pertenece a la empresa indicada.",
        )

    # Duplicate name check within same service
    dup_res = await db.execute(
        select(Team).where(
            (Team.service_id == payload.service_id) &
            (Team.team_name == payload.team_name)
        )
    )
    if dup_res.scalar():
        raise HTTPException(status_code=400, detail="Ya existe un equipo con ese nombre en este servicio.")

    team = Team(
        team_name=payload.team_name,
        company_id=payload.company_id,
        service_id=payload.service_id,
        is_active=True,
    )
    db.add(team)
    await db.commit()
    await db.refresh(team)

    logger.info("Actor (user_id=%s) CREATED team '%s' (id=%s) in company_id=%s",
                context.user_id, team.team_name, team.team_id, team.company_id)
    return await _build_admin_team_response(team, db)


# ---------------------------------------------------------------------------
# PATCH /bm/admin/teams/{team_id} — Update team
# ---------------------------------------------------------------------------

@router.patch("/{team_id}", response_model=AdminTeamResponse)
async def update_admin_team(
    team_id: int,
    payload: TeamUpdate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update team name and/or is_active status. Requires company_admin or super_admin."""
    res = await db.execute(select(Team).where(Team.team_id == team_id))
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado.")

    _check_write_permission(context, team)

    if payload.team_name is not None:
        if payload.team_name.strip() == "":
            raise HTTPException(status_code=400, detail="El nombre del equipo no puede estar vacío.")
        # Check duplicate name within same service (excluding self)
        dup_res = await db.execute(
            select(Team).where(
                (Team.service_id == team.service_id) &
                (Team.team_name == payload.team_name) &
                (Team.team_id != team_id)
            )
        )
        if dup_res.scalar():
            raise HTTPException(status_code=400, detail="Ya existe otro equipo con ese nombre en este servicio.")
        team.team_name = payload.team_name

    if payload.is_active is not None:
        team.is_active = payload.is_active

    await db.commit()
    await db.refresh(team)
    logger.info("Actor (user_id=%s) UPDATED team id=%s", context.user_id, team_id)
    return await _build_admin_team_response(team, db)


# ---------------------------------------------------------------------------
# GET /bm/admin/teams/{team_id}/agents
# ---------------------------------------------------------------------------

@router.get("/{team_id}/agents", response_model=List[AdminTeamMemberResponse])
async def list_team_agents(
    team_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List agents assigned to a team."""
    res = await db.execute(select(Team).where(Team.team_id == team_id))
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado.")
    _check_read_permission(context, team)

    stmt = (
        select(User)
        .join(AgentTeamAssociation, AgentTeamAssociation.user_id == User.user_id)
        .where(AgentTeamAssociation.team_id == team_id)
        .order_by(User.username)
    )
    agents_res = await db.execute(stmt)
    agents = list(agents_res.scalars().all())
    return [
        AdminTeamMemberResponse(
            user_id=u.user_id,
            username=u.username,
            email=u.email,
            name=u.name,
            role=u.role,
            normalized_role=normalize_role(u.role).value,
            hubspot_owner_id=u.hubspot_owner_id,
            agent_initials=u.agent_initials,
        )
        for u in agents
    ]


# ---------------------------------------------------------------------------
# POST /bm/admin/teams/{team_id}/agents/{user_id}
# ---------------------------------------------------------------------------

@router.post("/{team_id}/agents/{user_id}", status_code=status.HTTP_201_CREATED)
async def add_agent_to_team(
    team_id: int,
    user_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Add an agent to a team. super_admin, company_admin, or team_coordinator."""
    res = await db.execute(select(Team).where(Team.team_id == team_id))
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado.")
    _check_agent_management_permission(context, team)

    user_res = await db.execute(select(User).where(User.user_id == user_id))
    user = user_res.scalar()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    # User must be in the same company as the team
    if user.company_id != team.company_id:
        raise HTTPException(status_code=403, detail="El agente pertenece a otra empresa.")

    # User must be an agent-level role
    if normalize_role(user.role) not in (InternalRole.AGENT,):
        raise HTTPException(
            status_code=400,
            detail="Solo se pueden añadir usuarios con rol 'agente' a un equipo como agentes.",
        )

    # Idempotent insert
    existing_res = await db.execute(
        select(AgentTeamAssociation).where(
            (AgentTeamAssociation.user_id == user_id) &
            (AgentTeamAssociation.team_id == team_id)
        )
    )
    if not existing_res.scalar():
        db.add(AgentTeamAssociation(user_id=user_id, team_id=team_id))
        await db.commit()

    logger.info("Actor (user_id=%s) ADDED agent %s to team %s", context.user_id, user_id, team_id)
    return {"ok": True, "message": "Agente asociado al equipo con éxito."}


# ---------------------------------------------------------------------------
# DELETE /bm/admin/teams/{team_id}/agents/{user_id}
# ---------------------------------------------------------------------------

@router.delete("/{team_id}/agents/{user_id}")
async def remove_agent_from_team(
    team_id: int,
    user_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remove an agent from a team. super_admin, company_admin, or team_coordinator."""
    res = await db.execute(select(Team).where(Team.team_id == team_id))
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado.")
    _check_agent_management_permission(context, team)

    assoc_res = await db.execute(
        select(AgentTeamAssociation).where(
            (AgentTeamAssociation.user_id == user_id) &
            (AgentTeamAssociation.team_id == team_id)
        )
    )
    assoc = assoc_res.scalar()
    if assoc:
        await db.delete(assoc)
        await db.commit()

    logger.info("Actor (user_id=%s) REMOVED agent %s from team %s", context.user_id, user_id, team_id)
    return {"ok": True, "message": "Agente eliminado del equipo."}


# ---------------------------------------------------------------------------
# GET /bm/admin/teams/{team_id}/coordinators
# ---------------------------------------------------------------------------

@router.get("/{team_id}/coordinators", response_model=List[AdminTeamMemberResponse])
async def list_team_coordinators(
    team_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List coordinators assigned to a team."""
    res = await db.execute(select(Team).where(Team.team_id == team_id))
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado.")
    _check_read_permission(context, team)

    stmt = (
        select(User)
        .join(UserTeamAssociation, UserTeamAssociation.user_id == User.user_id)
        .where(UserTeamAssociation.team_id == team_id)
        .order_by(User.username)
    )
    coor_res = await db.execute(stmt)
    coordinators = list(coor_res.scalars().all())
    return [
        AdminTeamMemberResponse(
            user_id=u.user_id,
            username=u.username,
            email=u.email,
            name=u.name,
            role=u.role,
            normalized_role=normalize_role(u.role).value,
            hubspot_owner_id=u.hubspot_owner_id,
            agent_initials=u.agent_initials,
        )
        for u in coordinators
    ]


# ---------------------------------------------------------------------------
# POST /bm/admin/teams/{team_id}/coordinators/{user_id}
# ---------------------------------------------------------------------------

@router.post("/{team_id}/coordinators/{user_id}", status_code=status.HTTP_201_CREATED)
async def add_coordinator_to_team(
    team_id: int,
    user_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Assign a coordinator to a team. super_admin or company_admin only."""
    _check_write_permission(context)

    res = await db.execute(select(Team).where(Team.team_id == team_id))
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado.")

    if not context.is_super_admin and team.company_id != context.company_id:
        raise HTTPException(status_code=403, detail="Acceso denegado: equipo de otra empresa.")

    user_res = await db.execute(select(User).where(User.user_id == user_id))
    user = user_res.scalar()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    if user.company_id != team.company_id:
        raise HTTPException(status_code=403, detail="El coordinador pertenece a otra empresa.")

    allowed_coordinator_roles = (
        InternalRole.TEAM_COORDINATOR,
        InternalRole.SERVICE_MANAGER,
        InternalRole.COMPANY_ADMIN,
    )
    if normalize_role(user.role) not in allowed_coordinator_roles:
        raise HTTPException(
            status_code=400,
            detail="Solo se pueden asignar como coordinadores usuarios con rol coordinador_equipo, responsable_servicio o company_admin.",
        )

    # Idempotent insert
    existing_res = await db.execute(
        select(UserTeamAssociation).where(
            (UserTeamAssociation.user_id == user_id) &
            (UserTeamAssociation.team_id == team_id)
        )
    )
    if not existing_res.scalar():
        db.add(UserTeamAssociation(user_id=user_id, team_id=team_id))
        await db.commit()

    logger.info("Actor (user_id=%s) ADDED coordinator %s to team %s", context.user_id, user_id, team_id)
    return {"ok": True, "message": "Coordinador asignado al equipo con éxito."}


# ---------------------------------------------------------------------------
# DELETE /bm/admin/teams/{team_id}/coordinators/{user_id}
# ---------------------------------------------------------------------------

@router.delete("/{team_id}/coordinators/{user_id}")
async def remove_coordinator_from_team(
    team_id: int,
    user_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remove a coordinator from a team. super_admin or company_admin only."""
    _check_write_permission(context)

    res = await db.execute(select(Team).where(Team.team_id == team_id))
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=404, detail="Equipo no encontrado.")

    if not context.is_super_admin and team.company_id != context.company_id:
        raise HTTPException(status_code=403, detail="Acceso denegado: equipo de otra empresa.")

    assoc_res = await db.execute(
        select(UserTeamAssociation).where(
            (UserTeamAssociation.user_id == user_id) &
            (UserTeamAssociation.team_id == team_id)
        )
    )
    assoc = assoc_res.scalar()
    if assoc:
        await db.delete(assoc)
        await db.commit()

    logger.info("Actor (user_id=%s) REMOVED coordinator %s from team %s", context.user_id, user_id, team_id)
    return {"ok": True, "message": "Coordinador eliminado del equipo."}
