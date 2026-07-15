import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import (
    get_db,
    get_tenant_context,
    require_company_admin_or_super_admin,
    RequireTeamAccess,
)
from app.models.teams import Team, AgentTeamAssociation
from app.models.users import User
from app.models.services import Service
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole, normalize_role
from app.schemas.multitenancy import TeamResponse, TeamCreate, TeamUpdate, UserSummaryResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Multi-tenancy Teams"])


@router.get("/teams", response_model=List[TeamResponse])
async def list_teams(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    company_id: Optional[int] = Query(None),
    service_id: Optional[int] = Query(None)
):
    """List all teams accessible by the authenticated user's context, with optional filters."""
    stmt = select(Team)
    
    if not context.is_super_admin:
        # Constrain to user's assigned company
        stmt = stmt.where(Team.company_id == context.company_id)
        if company_id is not None and company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail="Acceso denegado a equipos de otra empresa."
            )
        
        # Limit to allowed services/teams
        if context.allowed_team_ids is not None:
            stmt = stmt.where(Team.team_id.in_(context.allowed_team_ids))
        if context.allowed_service_ids is not None:
            stmt = stmt.where(Team.service_id.in_(context.allowed_service_ids))
    else:
        if company_id is not None:
            stmt = stmt.where(Team.company_id == company_id)
            
    if service_id is not None:
        stmt = stmt.where(Team.service_id == service_id)
        
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.get("/teams/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: int,
    context: Annotated[TenantContext, Depends(RequireTeamAccess())],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Retrieve details of a specific team by ID."""
    stmt = select(Team).where(Team.team_id == team_id)
    res = await db.execute(stmt)
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipo no encontrado.")
    return team


@router.post("/teams", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    payload: TeamCreate,
    context: Annotated[TenantContext, Depends(require_company_admin_or_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Create a new team (Company Admin or Super Admin only)."""
    if not context.is_super_admin and payload.company_id != context.company_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="No tienes permisos para crear equipos en otra empresa."
        )
    
    # Validate service exists in the target company
    svc_stmt = select(Service).where(
        (Service.service_id == payload.service_id) & 
        (Service.company_id == payload.company_id)
    )
    svc_res = await db.execute(svc_stmt)
    if not svc_res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="El servicio especificado no existe o no está registrado en esa empresa."
        )
    
    # Check duplicate team name in that service
    dup_stmt = select(Team).where(
        (Team.service_id == payload.service_id) & 
        (Team.team_name == payload.team_name)
    )
    dup_res = await db.execute(dup_stmt)
    if dup_res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Ya existe un equipo con ese nombre en este servicio."
        )
    
    team = Team(
        team_name=payload.team_name,
        company_id=payload.company_id,
        service_id=payload.service_id
    )
    db.add(team)
    await db.commit()
    await db.refresh(team)
    return team


@router.patch("/teams/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: int,
    payload: TeamUpdate,
    context: Annotated[TenantContext, Depends(RequireTeamAccess(check_write=True))],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Update team details (Company Admin or Super Admin only)."""
    stmt = select(Team).where(Team.team_id == team_id)
    res = await db.execute(stmt)
    team = res.scalar()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipo no encontrado.")
    
    # Check duplicate team name
    dup_stmt = select(Team).where(
        (Team.service_id == team.service_id) & 
        (Team.team_name == payload.team_name)
    )
    dup_res = await db.execute(dup_stmt)
    existing = dup_res.scalar()
    if existing and existing.team_id != team_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Ya existe otro equipo con ese nombre en este servicio."
        )
    
    team.team_name = payload.team_name
    await db.commit()
    await db.refresh(team)
    return team


@router.post("/teams/{team_id}/agents/{user_id}", status_code=status.HTTP_201_CREATED)
async def add_agent_to_team(
    team_id: int,
    user_id: int,
    context: Annotated[TenantContext, Depends(RequireTeamAccess())],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Associate an agent to a team (Super Admin, Company Admin, or Team Coordinator)."""
    is_authorized = (
        context.is_super_admin or
        context.normalized_role in (InternalRole.COMPANY_ADMIN, InternalRole.TEAM_COORDINATOR)
    )
    if not is_authorized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="No tienes permisos para agregar agentes a este equipo."
        )

    # Load team to verify it exists and get company_id
    team_stmt = select(Team).where(Team.team_id == team_id)
    team_res = await db.execute(team_stmt)
    team = team_res.scalar()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipo no encontrado.")

    # Load target user
    user_stmt = select(User).where(User.user_id == user_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")

    # Validate target user is indeed an agent
    if normalize_role(user.role) != InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="El usuario especificado no tiene un rol de Agente."
        )

    # Validate target user and team belong to the same company
    if user.company_id != team.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="El usuario y el equipo pertenecen a empresas distintas."
        )

    # Insert association if it does not exist (idempotent)
    stmt = select(AgentTeamAssociation).where(
        (AgentTeamAssociation.user_id == user_id) & 
        (AgentTeamAssociation.team_id == team_id)
    )
    res = await db.execute(stmt)
    assoc = res.scalar()
    if not assoc:
        new_assoc = AgentTeamAssociation(user_id=user_id, team_id=team_id)
        db.add(new_assoc)
        await db.commit()
    
    return {"ok": True, "message": "Agente asociado al equipo con éxito."}


@router.delete("/teams/{team_id}/agents/{user_id}")
async def remove_agent_from_team(
    team_id: int,
    user_id: int,
    context: Annotated[TenantContext, Depends(RequireTeamAccess())],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Disassociate an agent from a team (Super Admin, Company Admin, or Team Coordinator)."""
    is_authorized = (
        context.is_super_admin or
        context.normalized_role in (InternalRole.COMPANY_ADMIN, InternalRole.TEAM_COORDINATOR)
    )
    if not is_authorized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="No tienes permisos para remover agentes de este equipo."
        )

    stmt = select(AgentTeamAssociation).where(
        (AgentTeamAssociation.user_id == user_id) & 
        (AgentTeamAssociation.team_id == team_id)
    )
    res = await db.execute(stmt)
    assoc = res.scalar()
    if assoc:
        await db.delete(assoc)
        await db.commit()
    
    return {"ok": True, "message": "Agente desasociado del equipo con éxito."}


@router.get("/teams/{team_id}/agents", response_model=List[UserSummaryResponse])
async def list_team_agents(
    team_id: int,
    context: Annotated[TenantContext, Depends(RequireTeamAccess())],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """List all agents assigned to a specific team."""
    stmt = select(User).join(AgentTeamAssociation).where(AgentTeamAssociation.team_id == team_id)
    res = await db.execute(stmt)
    return list(res.scalars().all())
