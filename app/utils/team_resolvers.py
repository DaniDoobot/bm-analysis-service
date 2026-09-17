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
    company_id: Optional[int] = None,
    hubspot_owner_id: Optional[str] = None,
    typology_id: Optional[int] = None,
    typology_key: Optional[str] = None,
) -> None:
    """
    Validates that:
    1. If company_id is provided (or resolved from context), service, team, agent, and typology belong to this company.
       If mismatched, raises HTTP 400 Bad Request:
       - 'El servicio seleccionado no pertenece a la empresa indicada.'
       - 'El equipo seleccionado no pertenece a la empresa indicada.'
       - 'El agente seleccionado no pertenece a la empresa indicada.'
       - 'La tipología seleccionada no pertenece a la empresa indicada.'
    2. If service_id is provided, checks permissions and verifies agent/team/typology belong to service.
    3. If team_id is provided, team exists, checks permissions, and validates team.service_id == service_id.
    4. If hubspot_owner_id is provided, validates agent belongs to service and/or team.
    5. If typology_id or typology_key is provided, validates typology belongs to company, service, team, and agent.
    """
    eff_company_id = company_id
    if eff_company_id is None and context and not context.is_super_admin:
        eff_company_id = context.company_id

    # 1. Validate service if service_id is provided
    if service_id is not None:
        from app.models.services import Service
        stmt_s = select(Service).where(Service.service_id == service_id)
        res_s = await db.execute(stmt_s)
        service = res_s.scalar_one_or_none()
        if service is not None:
            # Check company cascade
            if eff_company_id is not None:
                svc_comp = service.company_id
                if eff_company_id == 1:
                    is_match = (svc_comp == 1 or svc_comp is None)
                else:
                    is_match = (svc_comp == eff_company_id)
                if not is_match:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="El servicio seleccionado no pertenece a la empresa indicada."
                    )
        # Service permission check
        if context and not context.is_super_admin:
            if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: No tienes permisos para este servicio."
                )

    # 2. Validate agent if hubspot_owner_id is provided
    clean_agent_id = str(hubspot_owner_id).strip() if hubspot_owner_id is not None and str(hubspot_owner_id).strip() else None
    if clean_agent_id is not None:
        from app.models.users import User
        stmt_u = select(User).where(User.hubspot_owner_id == clean_agent_id)
        res_u = await db.execute(stmt_u)
        agent_user = res_u.scalars().first()
        if agent_user is not None:
            if eff_company_id is not None:
                ag_comp = agent_user.company_id
                if eff_company_id == 1:
                    is_match = (ag_comp == 1 or ag_comp is None)
                else:
                    is_match = (ag_comp == eff_company_id)
                if not is_match:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="El agente seleccionado no pertenece a la empresa indicada."
                    )
        # Validate agent belongs to service_id if service_id is provided
        if service_id is not None:
            svc_agents = await get_service_assigned_owner_ids(db, service_id=service_id, context=context, company_id=eff_company_id)
            if clean_agent_id not in svc_agents:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El agente seleccionado no pertenece al servicio indicado."
                )

    # 3. Validate team if team_id is provided
    team = None
    if team_id is not None:
        from app.models.teams import Team

        stmt = select(Team).where(Team.team_id == team_id)
        res = await db.execute(stmt)
        team = res.scalar_one_or_none()

        if not team:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Equipo no encontrado."
            )

        # Validate team company cascade
        if eff_company_id is not None:
            tm_comp = team.company_id
            if eff_company_id == 1:
                is_match = (tm_comp == 1 or tm_comp is None)
            else:
                is_match = (tm_comp == eff_company_id)
            if not is_match:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El equipo seleccionado no pertenece a la empresa indicada."
                )

        # Tenant check on team's company, team permission, and team's service permission
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

        # Cascade check: Agent must belong to the specified team_id
        if clean_agent_id is not None:
            team_agents = await get_team_assigned_owner_ids(db, team_id=team_id, context=context, company_id=eff_company_id)
            if clean_agent_id not in team_agents:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El agente seleccionado no pertenece al equipo indicado."
                )

    # 4. Validate typology if typology_id or typology_key is provided
    if typology_id is not None or (typology_key is not None and str(typology_key).strip()):
        from app.models.typologies import Typology
        typology = None
        if typology_id is not None:
            stmt_typ = select(Typology).where(Typology.typology_id == typology_id)
            res_typ = await db.execute(stmt_typ)
            typology = res_typ.scalar_one_or_none()
            if not typology:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Tipología no encontrada."
                )
        elif typology_key is not None:
            clean_key = str(typology_key).strip()
            if clean_key and clean_key.lower() not in ("all", "todas", "total", "*"):
                stmt_typ = select(Typology).where(Typology.typology_key == clean_key, Typology.is_active == True)
                if eff_company_id is not None:
                    if eff_company_id == 1:
                        stmt_typ = stmt_typ.where(or_(Typology.company_id == 1, Typology.company_id.is_(None)))
                    else:
                        stmt_typ = stmt_typ.where(Typology.company_id == eff_company_id)
                res_typ = await db.execute(stmt_typ)
                typos = res_typ.scalars().all()
                if not typos:
                    # Check if exists in another company to raise explicit 400
                    res_any = await db.execute(select(Typology).where(Typology.typology_key == clean_key, Typology.is_active == True))
                    if res_any.scalars().first() is not None:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="La tipología seleccionada no pertenece a la empresa indicada."
                        )
                else:
                    target_sid = service_id if service_id is not None else (team.service_id if (team and team.service_id) else None)
                    match_svc = next((t for t in typos if t.service_id == target_sid), None)
                    typology = match_svc or typos[0]


        if typology is not None:
            # 4a. Typology company check
            if eff_company_id is not None:
                typ_comp = typology.company_id
                if typ_comp is None and typology.service_id is not None:
                    from app.models.services import Service
                    res_s_comp = await db.execute(select(Service.company_id).where(Service.service_id == typology.service_id))
                    typ_comp = res_s_comp.scalar_one_or_none()
                if eff_company_id == 1:
                    is_match = (typ_comp == 1 or typ_comp is None)
                else:
                    is_match = (typ_comp == eff_company_id)
                if not is_match:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="La tipología seleccionada no pertenece a la empresa indicada."
                    )

            # 4b. Typology service check
            if service_id is not None and typology.service_id != service_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="La tipología seleccionada no pertenece al servicio indicado."
                )

            # 4c. Typology team check
            if team is not None and team.service_id is not None and typology.service_id != team.service_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="La tipología seleccionada no pertenece al servicio del equipo indicado."
                )

            # 4d. Typology agent check
            if clean_agent_id is not None and typology.service_id is not None:
                typ_svc_agents = await get_service_assigned_owner_ids(db, service_id=typology.service_id, context=context, company_id=eff_company_id)
                if clean_agent_id not in typ_svc_agents:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="La tipología seleccionada no es compatible con el agente indicado."
                    )


async def get_team_assigned_users(
    db: AsyncSession,
    team_id: int,
    context: Optional[TenantContext] = None,
    company_id: Optional[int] = None,
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

    if company_id is not None:
        if company_id == 1:
            stmt = stmt.where(or_(User.company_id == 1, User.company_id.is_(None)))
        else:
            stmt = stmt.where(User.company_id == company_id)
        if context and not context.is_super_admin and company_id not in context.allowed_company_ids:
            return {}
        if context and context.allowed_agent_ids is not None:
            stmt = stmt.where(User.hubspot_owner_id.in_(context.allowed_agent_ids))
    elif context and not context.is_super_admin:
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


async def get_team_assigned_owner_ids(
    db: AsyncSession,
    team_id: int,
    context: Optional[TenantContext] = None,
    company_id: Optional[int] = None,
) -> Set[str]:
    """
    Returns the set of active hubspot_owner_ids assigned to team_id.
    """
    users_map = await get_team_assigned_users(db, team_id=team_id, context=context, company_id=company_id)
    return set(users_map.keys())


async def get_service_assigned_users(
    db: AsyncSession,
    service_id: Optional[int] = None,
    context: Optional[TenantContext] = None,
    company_id: Optional[int] = None,
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
        if company_id is not None:
            if company_id == 1:
                stmt = stmt.where(or_(User.company_id == 1, User.company_id.is_(None)))
            else:
                stmt = stmt.where(User.company_id == company_id)
            if context and not context.is_super_admin and company_id not in context.allowed_company_ids:
                return {}
            if context and context.allowed_agent_ids is not None:
                stmt = stmt.where(User.hubspot_owner_id.in_(context.allowed_agent_ids))
        elif context and not context.is_super_admin:
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
    if company_id is not None:
        team_stmt = team_stmt.where(Team.company_id == company_id)
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
    if company_id is not None:
        if company_id == 1:
            stmt = stmt.where(or_(User.company_id == 1, User.company_id.is_(None)))
        else:
            stmt = stmt.where(User.company_id == company_id)
        if context and not context.is_super_admin and company_id not in context.allowed_company_ids:
            return {}
        if context and context.allowed_agent_ids is not None:
            stmt = stmt.where(User.hubspot_owner_id.in_(context.allowed_agent_ids))
    elif context and not context.is_super_admin:
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
    company_id: Optional[int] = None,
) -> Set[str]:
    """
    Returns the set of active hubspot_owner_ids assigned to service_id.
    """
    users_map = await get_service_assigned_users(db, service_id=service_id, context=context, company_id=company_id)
    return set(users_map.keys())
