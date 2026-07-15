"""FastAPI router for administrative user management and assignments."""
import logging
import re
from typing import Annotated, List, Optional
from pydantic import BaseModel, ConfigDict
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_tenant_context
from app.models.users import User
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole, normalize_role
from app.schemas.services import ServiceOut
from app.schemas.multitenancy import TeamResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/admin/users", tags=["Admin Users"])


class AdminUserResponse(BaseModel):
    user_id: int
    username: str
    email: str
    name: Optional[str] = None
    role: str
    normalized_role: str
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    is_active: bool
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None
    allowed_service_ids: List[int] = []
    allowed_team_ids: List[int] = []

    model_config = ConfigDict(from_attributes=True)


class AdminUserUpdatePayload(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[bool] = None
    company_id: Optional[int] = None
    role: Optional[str] = None


@router.get("", response_model=List[AdminUserResponse])
async def list_users(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    company_id: Optional[int] = Query(None),
    role: Optional[str] = Query(None),
    service_id: Optional[int] = Query(None),
    team_id: Optional[int] = Query(None),
    is_active: Optional[bool] = Query(None),
    limit: int = Query(100, ge=1, le=1000)
):
    """List users in the system, enforcing multi-tenant filtering based on the actor's role."""
    stmt = select(User).distinct()

    # Enforce company restrictions for non-super_admin actors
    if not context.is_super_admin:
        stmt = stmt.where(User.company_id == context.company_id)
        if company_id is not None and company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para ver usuarios de otra empresa."
            )
    else:
        if company_id is not None:
            stmt = stmt.where(User.company_id == company_id)

    # Role-based scoping of users list
    if not context.is_super_admin:
        role_actor = context.normalized_role
        if role_actor == InternalRole.COMPANY_ADMIN:
            # Can see all users in their company
            pass
        elif role_actor == InternalRole.SERVICE_MANAGER:
            # Users associated to their allowed services OR agents in teams under those services
            user_svc_sub = select(UserServiceAssociation.user_id).where(
                UserServiceAssociation.service_id.in_(context.allowed_service_ids or [])
            )
            agent_team_sub = select(AgentTeamAssociation.user_id).join(Team).where(
                Team.service_id.in_(context.allowed_service_ids or [])
            )
            stmt = stmt.where(
                User.user_id.in_(user_svc_sub) |
                User.user_id.in_(agent_team_sub) |
                (User.user_id == context.user_id)
            )
        elif role_actor == InternalRole.TEAM_COORDINATOR:
            # Agents in their allowed teams, and themselves
            agent_team_sub = select(AgentTeamAssociation.user_id).where(
                AgentTeamAssociation.team_id.in_(context.allowed_team_ids or [])
            )
            stmt = stmt.where(
                User.user_id.in_(agent_team_sub) |
                (User.user_id == context.user_id)
            )
        else:
            # Agent or other role: can only see themselves
            stmt = stmt.where(User.user_id == context.user_id)

    # Optional Filters
    if role is not None:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    if service_id is not None:
        user_svc_sub = select(UserServiceAssociation.user_id).where(UserServiceAssociation.service_id == service_id)
        stmt = stmt.where(User.user_id.in_(user_svc_sub))
    if team_id is not None:
        team_sub = select(AgentTeamAssociation.user_id).where(AgentTeamAssociation.team_id == team_id)
        coord_sub = select(UserTeamAssociation.user_id).where(UserTeamAssociation.team_id == team_id)
        stmt = stmt.where(User.user_id.in_(team_sub) | User.user_id.in_(coord_sub))

    stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    users = res.scalars().all()

    # Batch retrieve details for formatting response efficiently
    user_ids = [u.user_id for u in users]
    svc_map = {}
    team_map = {}
    comp_map = {}

    if user_ids:
        # Fetch all user service associations
        svc_res = await db.execute(
            select(UserServiceAssociation).where(UserServiceAssociation.user_id.in_(user_ids))
        )
        for sa in svc_res.scalars().all():
            svc_map.setdefault(sa.user_id, []).append(sa.service_id)

        # Fetch all user team associations (coordinators)
        coord_res = await db.execute(
            select(UserTeamAssociation).where(UserTeamAssociation.user_id.in_(user_ids))
        )
        for tc in coord_res.scalars().all():
            team_map.setdefault(tc.user_id, []).append(tc.team_id)

        # Fetch all agent team associations (agents)
        agent_res = await db.execute(
            select(AgentTeamAssociation).where(AgentTeamAssociation.user_id.in_(user_ids))
        )
        for ta in agent_res.scalars().all():
            team_map.setdefault(ta.user_id, []).append(ta.team_id)

        # Fetch companies
        comp_res = await db.execute(select(Company))
        comp_map = {c.company_id: c.company_name for c in comp_res.scalars().all()}

    res_list = []
    for u in users:
        res_list.append(AdminUserResponse(
            user_id=u.user_id,
            username=u.username,
            email=u.email,
            name=u.name,
            role=u.role,
            normalized_role=normalize_role(u.role).value,
            company_id=u.company_id,
            company_name=comp_map.get(u.company_id) if u.company_id else None,
            is_active=u.is_active,
            hubspot_owner_id=u.hubspot_owner_id,
            agent_initials=u.agent_initials,
            allowed_service_ids=list(set(svc_map.get(u.user_id, []))),
            allowed_team_ids=list(set(team_map.get(u.user_id, [])))
        ))

    return res_list


@router.patch("/{user_id}", response_model=AdminUserResponse)
async def update_user(
    user_id: int,
    payload: AdminUserUpdatePayload,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Update details of a user, enforcing multi-tenant security rules."""
    # Fetch target user
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado."
        )

    # Actor must be super_admin or company_admin
    if context.normalized_role not in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Administrador de Empresa o Super Administrador."
        )

    # If actor is company_admin:
    if context.normalized_role == InternalRole.COMPANY_ADMIN:
        # User must belong to actor's company
        if user.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para modificar usuarios de otra empresa."
            )
        # Cannot change company_id
        if payload.company_id is not None and payload.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para cambiar la empresa del usuario."
            )
        # Cannot assign super_admin role
        if payload.role is not None and normalize_role(payload.role) == InternalRole.SUPER_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No puedes asignar el rol de Super Administrador."
            )
        # Cannot modify own company or role (prevent self-demotion or self-removal)
        if user.user_id == context.user_id:
            if payload.company_id is not None and payload.company_id != context.company_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No puedes cambiar tu propia empresa."
                )
            if payload.role is not None and normalize_role(payload.role) != InternalRole.COMPANY_ADMIN:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No puedes cambiar o degradar tu propio rol."
                )
    else:
        # Superadmin can do everything, but validate target company exists if provided
        if payload.company_id is not None:
            comp_stmt = select(Company).where(Company.company_id == payload.company_id)
            comp_res = await db.execute(comp_stmt)
            if not comp_res.scalars().first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="La empresa especificada no existe."
                )

    # Perform update fields
    if payload.name is not None:
        user.name = payload.name.strip()

    if payload.email is not None:
        email_str = payload.email.strip().lower()
        if email_str != user.email:
            # Check unique email constraint
            email_stmt = select(User).where(User.email == email_str)
            email_res = await db.execute(email_stmt)
            if email_res.scalars().first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El correo electrónico ya está en uso."
                )
            user.email = email_str

    if payload.is_active is not None:
        user.is_active = payload.is_active

    if payload.company_id is not None:
        user.company_id = payload.company_id

    if payload.role is not None:
        user.role = payload.role

    await db.commit()
    await db.refresh(user)

    # Fetch fresh company name and associations to format response
    comp_stmt = select(Company.company_name).where(Company.company_id == user.company_id)
    comp_res = await db.execute(comp_stmt)
    comp_name = comp_res.scalar()

    # Fetch user services
    svc_stmt = select(UserServiceAssociation.service_id).where(UserServiceAssociation.user_id == user.user_id)
    svc_res = await db.execute(svc_stmt)
    svc_ids = list(svc_res.scalars().all())

    # Fetch user teams
    t_stmt1 = select(UserTeamAssociation.team_id).where(UserTeamAssociation.user_id == user.user_id)
    t_res1 = await db.execute(t_stmt1)
    team_ids = list(t_res1.scalars().all())

    t_stmt2 = select(AgentTeamAssociation.team_id).where(AgentTeamAssociation.user_id == user.user_id)
    t_res2 = await db.execute(t_stmt2)
    team_ids.extend(t_res2.scalars().all())
    team_ids = list(set(team_ids))

    return AdminUserResponse(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        name=user.name,
        role=user.role,
        normalized_role=normalize_role(user.role).value,
        company_id=user.company_id,
        company_name=comp_name,
        is_active=user.is_active,
        hubspot_owner_id=user.hubspot_owner_id,
        agent_initials=user.agent_initials,
        allowed_service_ids=svc_ids,
        allowed_team_ids=team_ids
    )


# ── Parte 3 — Asignaciones usuario-servicio ─────────────────────────────────

@router.get("/{user_id}/services", response_model=List[ServiceOut])
async def list_user_services(
    user_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """List services associated with a specific user."""
    # Check permissions: actor must be super_admin, company_admin of user company, or user themselves
    user_stmt = select(User).where(User.user_id == user_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")

    if not context.is_super_admin:
        if user_id != context.user_id and user.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a datos de otra empresa."
            )

    stmt = select(Service).join(UserServiceAssociation).where(UserServiceAssociation.user_id == user_id)
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.post("/{user_id}/services/{service_id}", status_code=status.HTTP_201_CREATED)
async def assign_user_service(
    user_id: int,
    service_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Associate a user with a service (Super Admin or Company Admin only)."""
    # Verify actor permission
    if not context.is_super_admin and context.normalized_role != InternalRole.COMPANY_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Administrador de Empresa o Super Administrador."
        )

    # Load target user
    user_stmt = select(User).where(User.user_id == user_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")

    # Load target service
    svc_stmt = select(Service).where(Service.service_id == service_id)
    svc_res = await db.execute(svc_stmt)
    service = svc_res.scalar()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Servicio no encontrado.")

    # Validate cross-company constraint
    if not context.is_super_admin:
        if user.company_id != context.company_id or service.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No puedes realizar asignaciones cruzadas de otra empresa."
            )
    else:
        if user.company_id != service.company_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El usuario y el servicio deben pertenecer a la misma empresa."
            )

    # Idempotent insert
    stmt = select(UserServiceAssociation).where(
        (UserServiceAssociation.user_id == user_id) &
        (UserServiceAssociation.service_id == service_id)
    )
    res = await db.execute(stmt)
    assoc = res.scalar()
    if not assoc:
        new_assoc = UserServiceAssociation(user_id=user_id, service_id=service_id)
        db.add(new_assoc)
        await db.commit()

    return {"ok": True, "message": "Servicio asignado con éxito."}


@router.delete("/{user_id}/services/{service_id}")
async def unassign_user_service(
    user_id: int,
    service_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Remove service association from a user (Super Admin or Company Admin only)."""
    if not context.is_super_admin and context.normalized_role != InternalRole.COMPANY_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Administrador de Empresa o Super Administrador."
        )

    # Load target user
    user_stmt = select(User).where(User.user_id == user_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")

    if not context.is_super_admin and user.company_id != context.company_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para modificar usuarios de otra empresa."
        )

    stmt = select(UserServiceAssociation).where(
        (UserServiceAssociation.user_id == user_id) &
        (UserServiceAssociation.service_id == service_id)
    )
    res = await db.execute(stmt)
    assoc = res.scalar()
    if assoc:
        await db.delete(assoc)
        await db.commit()

    return {"ok": True, "message": "Asignación de servicio eliminada con éxito."}


# ── Parte 4 — Asignaciones usuario-equipo ────────────────────────────────────

@router.get("/{user_id}/teams", response_model=List[TeamResponse])
async def list_user_teams(
    user_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """List teams coordinated by a specific user."""
    # Check permissions: actor must be super_admin, company_admin of user company, or user themselves
    user_stmt = select(User).where(User.user_id == user_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")

    if not context.is_super_admin:
        if user_id != context.user_id and user.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado a datos de otra empresa."
            )

    stmt = select(Team).join(UserTeamAssociation).where(UserTeamAssociation.user_id == user_id)
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.post("/{user_id}/teams/{team_id}", status_code=status.HTTP_201_CREATED)
async def assign_user_team(
    user_id: int,
    team_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Associate a user as coordinator for a team (Super Admin or Company Admin only)."""
    if not context.is_super_admin and context.normalized_role != InternalRole.COMPANY_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Administrador de Empresa o Super Administrador."
        )

    # Load target user
    user_stmt = select(User).where(User.user_id == user_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")

    # Load target team
    team_stmt = select(Team).where(Team.team_id == team_id)
    team_res = await db.execute(team_stmt)
    team = team_res.scalar()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Equipo no encontrado.")

    # Validate cross-company constraint
    if not context.is_super_admin:
        if user.company_id != context.company_id or team.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No puedes realizar asignaciones cruzadas de otra empresa."
            )
    else:
        if user.company_id != team.company_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El usuario y el equipo deben pertenecer a la misma empresa."
            )

    # Idempotent insert
    stmt = select(UserTeamAssociation).where(
        (UserTeamAssociation.user_id == user_id) &
        (UserTeamAssociation.team_id == team_id)
    )
    res = await db.execute(stmt)
    assoc = res.scalar()
    if not assoc:
        new_assoc = UserTeamAssociation(user_id=user_id, team_id=team_id)
        db.add(new_assoc)
        await db.commit()

    return {"ok": True, "message": "Asignación de coordinador de equipo realizada con éxito."}


@router.delete("/{user_id}/teams/{team_id}")
async def unassign_user_team(
    user_id: int,
    team_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Remove team coordinator association from a user (Super Admin or Company Admin only)."""
    if not context.is_super_admin and context.normalized_role != InternalRole.COMPANY_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Administrador de Empresa o Super Administrador."
        )

    # Load target user
    user_stmt = select(User).where(User.user_id == user_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")

    if not context.is_super_admin and user.company_id != context.company_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para modificar usuarios de otra empresa."
        )

    stmt = select(UserTeamAssociation).where(
        (UserTeamAssociation.user_id == user_id) &
        (UserTeamAssociation.team_id == team_id)
    )
    res = await db.execute(stmt)
    assoc = res.scalar()
    if assoc:
        await db.delete(assoc)
        await db.commit()

    return {"ok": True, "message": "Asignación de coordinador de equipo eliminada con éxito."}
