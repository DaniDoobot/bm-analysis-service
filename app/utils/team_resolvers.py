"""
Centralized Team Resolution and Validation Utilities.
=====================================================
Provides helpers to validate service->team cascade and resolve assigned agents
consistently across all dashboard, analytics, and listings endpoints.
"""
import logging
from typing import Any, Dict, Optional, Set
from fastapi import HTTPException, status
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_context import TenantContext

logger = logging.getLogger(__name__)


async def validate_team_service_cascade(
    db: AsyncSession,
    service_id: Optional[int] = None,
    team_id: Optional[int] = None,
    context: Optional[TenantContext] = None,
) -> None:
    """
    Validates that team_id exists, belongs to tenant context, and belongs to service_id
    if service_id is provided.
    If team does not belong to service_id, raises HTTP 400 with detail:
    'El equipo seleccionado no pertenece al servicio indicado.'
    """
    # 1. Service permission check if service_id is provided
    if service_id is not None and context and not context.is_super_admin:
        if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para este servicio."
            )

    if team_id is None:
        return

    from app.models.teams import Team

    stmt = select(Team).where(Team.team_id == team_id)
    res = await db.execute(stmt)
    team = res.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Equipo no encontrado."
        )

    # 2. Tenant check on team's company, team permission, and team's service permission
    if context and not context.is_super_admin:
        if team.company_id != context.company_id and (context.allowed_company_ids is None or team.company_id not in context.allowed_company_ids):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a equipos de otra empresa."
            )
        if context.allowed_team_ids is not None and team_id not in context.allowed_team_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para acceder a este equipo."
            )
        if context.allowed_service_ids is not None and team.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para acceder a este servicio."
            )

    # Cascade check: Team must belong to the specified service_id
    if service_id is not None and team.service_id != service_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El equipo seleccionado no pertenece al servicio indicado."
        )


async def get_team_assigned_owner_ids(
    db: AsyncSession,
    team_id: int,
    context: Optional[TenantContext] = None,
) -> Set[str]:
    """
    Returns the set of active hubspot_owner_ids assigned to team_id via:
    - User.primary_team_id == team_id
    - UserTeamAssociation (bm_user_teams) -> team_id
    - AgentTeamAssociation (bm_agent_teams) -> team_id
    Filters by User.is_active == True and User.hubspot_owner_id is not None.
    Applies tenant scoping if context is provided.
    """
    from app.models.users import User
    from app.models.teams import UserTeamAssociation, AgentTeamAssociation

    user_conds = [
        User.primary_team_id == team_id,
        User.user_id.in_(select(UserTeamAssociation.user_id).where(UserTeamAssociation.team_id == team_id)),
        User.user_id.in_(select(AgentTeamAssociation.user_id).where(AgentTeamAssociation.team_id == team_id)),
    ]

    stmt = select(User.hubspot_owner_id).where(
        User.is_active == True,
        User.hubspot_owner_id.is_not(None),
        or_(*user_conds)
    )

    if context and not context.is_super_admin:
        stmt = stmt.where(
            or_(
                User.company_id.in_(context.allowed_company_ids),
                User.company_id.is_(None)
            )
        )
        if context.allowed_agent_ids is not None:
            stmt = stmt.where(User.hubspot_owner_id.in_(context.allowed_agent_ids))

    res = await db.execute(stmt)
    return {str(oid).strip() for oid in res.scalars().all() if oid}


async def get_team_assigned_users(
    db: AsyncSession,
    team_id: int,
    context: Optional[TenantContext] = None,
) -> Dict[str, Any]:
    """
    Returns dict mapping hubspot_owner_id (str) -> User object for active users in team_id.
    """
    from app.models.users import User
    from app.models.teams import UserTeamAssociation, AgentTeamAssociation

    user_conds = [
        User.primary_team_id == team_id,
        User.user_id.in_(select(UserTeamAssociation.user_id).where(UserTeamAssociation.team_id == team_id)),
        User.user_id.in_(select(AgentTeamAssociation.user_id).where(AgentTeamAssociation.team_id == team_id)),
    ]

    stmt = select(User).where(
        User.is_active == True,
        User.hubspot_owner_id.is_not(None),
        or_(*user_conds)
    )

    if context and not context.is_super_admin:
        stmt = stmt.where(
            or_(
                User.company_id.in_(context.allowed_company_ids),
                User.company_id.is_(None)
            )
        )
        if context.allowed_agent_ids is not None:
            stmt = stmt.where(User.hubspot_owner_id.in_(context.allowed_agent_ids))

    res = await db.execute(stmt)
    return {str(u.hubspot_owner_id).strip(): u for u in res.scalars().all() if u.hubspot_owner_id}


async def get_service_assigned_users(
    db: AsyncSession,
    service_id: Optional[int] = None,
    context: Optional[TenantContext] = None,
) -> Dict[str, Any]:
    """
    Query active users from bm_users with a valid hubspot_owner_id.
    If service_id is provided, filters for users assigned to service_id via:
    - User.primary_service_id == service_id
    - User.primary_team_id -> Team.service_id == service_id
    - UserServiceAssociation (bm_user_services) -> service_id
    - UserTeamAssociation (bm_user_teams) -> Team.service_id == service_id
    - AgentTeamAssociation (bm_agent_teams) -> Team.service_id == service_id
    Returns dict mapping hubspot_owner_id (str) -> User object.
    """
    from app.models.users import User
    from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation

    if service_id is None:
        stmt = select(User).where(User.is_active == True, User.hubspot_owner_id.is_not(None))
        if context and not context.is_super_admin:
            stmt = stmt.where(
                or_(
                    User.company_id.in_(context.allowed_company_ids),
                    User.company_id.is_(None)
                )
            )
            if context.allowed_agent_ids is not None:
                stmt = stmt.where(User.hubspot_owner_id.in_(context.allowed_agent_ids))
        res = await db.execute(stmt)
        return {str(u.hubspot_owner_id).strip(): u for u in res.scalars().all() if u.hubspot_owner_id}

    # Find team IDs belonging to this service_id
    team_stmt = select(Team.team_id).where(Team.service_id == service_id)
    team_res = await db.execute(team_stmt)
    team_ids = [t for t in team_res.scalars().all()]

    user_conds = [
        User.primary_service_id == service_id,
        User.user_id.in_(select(UserServiceAssociation.user_id).where(UserServiceAssociation.service_id == service_id))
    ]
    if team_ids:
        user_conds.extend([
            User.primary_team_id.in_(team_ids),
            User.user_id.in_(select(UserTeamAssociation.user_id).where(UserTeamAssociation.team_id.in_(team_ids))),
            User.user_id.in_(select(AgentTeamAssociation.user_id).where(AgentTeamAssociation.team_id.in_(team_ids))),
        ])

    stmt = select(User).where(
        User.is_active == True,
        User.hubspot_owner_id.is_not(None),
        or_(*user_conds)
    )
    if context and not context.is_super_admin:
        stmt = stmt.where(
            or_(
                User.company_id.in_(context.allowed_company_ids),
                User.company_id.is_(None)
            )
        )
        if context.allowed_agent_ids is not None:
            stmt = stmt.where(User.hubspot_owner_id.in_(context.allowed_agent_ids))

    res = await db.execute(stmt)
    return {str(u.hubspot_owner_id).strip(): u for u in res.scalars().all() if u.hubspot_owner_id}


async def get_service_assigned_owner_ids(
    db: AsyncSession,
    service_id: Optional[int] = None,
    context: Optional[TenantContext] = None,
) -> Set[str]:
    """
    Returns the set of active hubspot_owner_ids assigned to service_id.
    """
    users_map = await get_service_assigned_users(db, service_id=service_id, context=context)
    return set(users_map.keys())
