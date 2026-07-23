"""FastAPI router for administrative user management and assignments."""
import logging
import re
from typing import Annotated, List, Optional
from pydantic import BaseModel, ConfigDict, Field, AliasChoices
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_tenant_context
from app.models.users import User
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole, normalize_role, ROLE_MAPPINGS
from app.schemas.services import ServiceOut
from app.schemas.multitenancy import TeamResponse
from app.services.users_service import (
    validate_user_services, save_user_service_associations, get_user_services_info,
    validate_user_teams, save_user_team_associations, get_user_teams_info
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/admin/users", tags=["Admin Users"])

EMAIL_REGEX = re.compile(r"^[^@]+@[^@]+\.[^@]+$")


class AdminUserResponse(BaseModel):
    user_id: int
    username: str
    email: str
    name: Optional[str] = None
    role: str
    normalized_role: str
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    primary_service_id: Optional[int] = None
    primary_service_name: Optional[str] = None
    primary_team_id: Optional[int] = None
    primary_team_name: Optional[str] = None
    is_active: bool
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None
    allowed_service_ids: List[int] = []
    allowed_services: List[dict] = []
    allowed_team_ids: List[int] = []
    allowed_teams: List[dict] = []

    model_config = ConfigDict(from_attributes=True)


class AdminUserCreatePayload(BaseModel):
    username: str
    email: str
    name: Optional[str] = None
    role: str
    company_id: Optional[int] = None
    primary_service_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("primary_service_id", "service_id"))
    allowed_service_ids: Optional[List[int]] = Field(default=None, validation_alias=AliasChoices("allowed_service_ids", "service_ids"))
    primary_team_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("primary_team_id", "team_id"))
    allowed_team_ids: Optional[List[int]] = Field(default=None, validation_alias=AliasChoices("allowed_team_ids", "team_ids"))
    is_active: bool = True
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None


class AdminUserUpdatePayload(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[bool] = None
    company_id: Optional[int] = None
    role: Optional[str] = None
    primary_service_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("primary_service_id", "service_id"))
    allowed_service_ids: Optional[List[int]] = Field(default=None, validation_alias=AliasChoices("allowed_service_ids", "service_ids"))
    primary_team_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("primary_team_id", "team_id"))
    allowed_team_ids: Optional[List[int]] = Field(default=None, validation_alias=AliasChoices("allowed_team_ids", "team_ids"))


class RoleOption(BaseModel):
    value: str
    label: str
    requires_company: bool
    global_field: bool = Field(default=False, serialization_alias="global")

    model_config = ConfigDict(populate_by_name=True)


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
            # Can see all users in their company except super_admins
            super_admin_roles = ["admin", "administrador", "superadmin", "super_admin"]
            stmt = stmt.where(~User.role.in_(super_admin_roles))
            stmt = stmt.where(User.company_id.is_not(None))
        elif role_actor == InternalRole.SERVICE_MANAGER:
            super_admin_roles = ["admin", "administrador", "superadmin", "super_admin"]
            stmt = stmt.where(~User.role.in_(super_admin_roles))
            stmt = stmt.where(User.company_id == context.company_id)

            allowed_svcs = context.allowed_service_ids or []
            user_svc_sub = select(UserServiceAssociation.user_id).where(
                UserServiceAssociation.service_id.in_(allowed_svcs)
            )
            agent_team_sub = select(AgentTeamAssociation.user_id).join(Team).where(
                Team.service_id.in_(allowed_svcs)
            )
            management_roles = [
                "company_admin", "administrador_empresa", "administrador_de_empresa", "administrador de empresa",
                "service_manager", "responsable_servicio", "responsable_de_servicio", "responsable de servicio",
                "team_coordinator", "coordinador_equipo", "coordinador_de_equipo", "coordinador de equipo"
            ]
            stmt = stmt.where(
                User.primary_service_id.in_(allowed_svcs) |
                User.user_id.in_(user_svc_sub) |
                User.user_id.in_(agent_team_sub) |
                func.lower(User.role).in_(management_roles) |
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

    allowed_service_ids_map, allowed_services_map, primary_service_map = await get_user_services_info(db, user_ids)
    allowed_team_ids_map, allowed_teams_map, primary_team_map = await get_user_teams_info(db, user_ids)

    res_list = []
    for u in users:
        p_id, p_name = primary_service_map.get(u.user_id, (u.primary_service_id, None))
        pt_id, pt_name = primary_team_map.get(u.user_id, (u.primary_team_id, None))
        res_list.append(AdminUserResponse(
            user_id=u.user_id,
            username=u.username,
            email=u.email,
            name=u.name,
            role=u.role,
            normalized_role=normalize_role(u.role).value,
            company_id=u.company_id,
            company_name=comp_map.get(u.company_id) if u.company_id else None,
            primary_service_id=p_id,
            primary_service_name=p_name,
            primary_team_id=pt_id,
            primary_team_name=pt_name,
            is_active=u.is_active,
            hubspot_owner_id=u.hubspot_owner_id,
            agent_initials=u.agent_initials,
            allowed_service_ids=allowed_service_ids_map.get(u.user_id, []),
            allowed_services=allowed_services_map.get(u.user_id, []),
            allowed_team_ids=allowed_team_ids_map.get(u.user_id, []),
            allowed_teams=allowed_teams_map.get(u.user_id, [])
        ))

    return res_list



@router.post("", response_model=AdminUserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreatePayload,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Create a new administrative user with hierarchical roles and tenant compliance."""
    import secrets
    from app.utils.security import hash_password
    from datetime import datetime, timezone, timedelta

    actor_role = context.normalized_role
    if actor_role not in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN, InternalRole.SERVICE_MANAGER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de gestión."
        )

    val = payload.role.strip().lower()
    if val not in ROLE_MAPPINGS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Rol no válido: '{payload.role}'."
        )
    target_role_norm = ROLE_MAPPINGS[val]

    if actor_role == InternalRole.COMPANY_ADMIN and target_role_norm == InternalRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para crear un Super Administrador."
        )
    if actor_role == InternalRole.SERVICE_MANAGER and target_role_norm in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para crear un Administrador de Empresa o Superadministrador."
        )

    company_id = payload.company_id
    if target_role_norm == InternalRole.SUPER_ADMIN:
        company_id = None
    else:
        if company_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Se requiere especificar un company_id para roles que no sean super_admin."
            )
        
        if actor_role in (InternalRole.COMPANY_ADMIN, InternalRole.SERVICE_MANAGER) and company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Solo puedes crear usuarios dentro de tu propia empresa."
            )
        
        comp_stmt = select(Company).where(Company.company_id == company_id)
        comp_res = await db.execute(comp_stmt)
        if not comp_res.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La empresa especificada no existe."
            )

    val_primary_id, val_allowed_ids = await validate_user_services(
        db,
        role=payload.role,
        company_id=company_id,
        primary_service_id=payload.primary_service_id,
        allowed_service_ids=payload.allowed_service_ids,
        context=context
    )

    val_primary_team_id, val_allowed_team_ids = await validate_user_teams(
        db,
        role=payload.role,
        company_id=company_id,
        primary_team_id=payload.primary_team_id,
        allowed_team_ids=payload.allowed_team_ids,
        context=context
    )

    username = payload.username.strip()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El nombre de usuario (username) es requerido y no puede estar vacío."
        )

    email = payload.email.strip().lower()
    if not EMAIL_REGEX.match(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El correo electrónico especificado no es válido."
        )

    uname_stmt = select(User).where(User.username == username)
    uname_res = await db.execute(uname_stmt)
    if uname_res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"El nombre de usuario '{username}' ya está en uso."
        )

    email_stmt = select(User).where(User.email == email)
    email_res = await db.execute(email_stmt)
    if email_res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"El correo electrónico '{email}' ya está en uso."
        )

    temp_pass = secrets.token_urlsafe(32)
    pass_hash = hash_password(temp_pass)
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    new_user = User(
        username=username,
        email=email,
        name=payload.name.strip() if payload.name else None,
        role=payload.role,
        is_active=payload.is_active,
        company_id=company_id,
        primary_service_id=val_primary_id,
        primary_team_id=val_primary_team_id,
        hubspot_owner_id=payload.hubspot_owner_id.strip() if payload.hubspot_owner_id else None,
        agent_initials=payload.agent_initials.strip() if payload.agent_initials else None,
        password_hash=pass_hash,
        must_reset_password=True,
        reset_token=token,
        reset_token_expires_at=expires_at
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    await save_user_service_associations(db, new_user.user_id, val_allowed_ids)
    await save_user_team_associations(db, new_user.user_id, val_allowed_team_ids)
    await db.commit()

    logger.info("Actor (user_id=%s) CREATED administrative user %s (id=%s)", context.user_id, new_user.email, new_user.user_id)

    comp_name = None
    if new_user.company_id:
        comp_stmt = select(Company.company_name).where(Company.company_id == new_user.company_id)
        comp_res = await db.execute(comp_stmt)
        comp_name = comp_res.scalar()

    allowed_service_ids_map, allowed_services_map, primary_service_map = await get_user_services_info(db, [new_user.user_id])
    p_id, p_name = primary_service_map.get(new_user.user_id, (val_primary_id, None))
    allowed_team_ids_map, allowed_teams_map, primary_team_map = await get_user_teams_info(db, [new_user.user_id])
    pt_id, pt_name = primary_team_map.get(new_user.user_id, (val_primary_team_id, None))

    return AdminUserResponse(
        user_id=new_user.user_id,
        username=new_user.username,
        email=new_user.email,
        name=new_user.name,
        role=new_user.role,
        normalized_role=target_role_norm.value,
        company_id=new_user.company_id,
        company_name=comp_name,
        primary_service_id=p_id,
        primary_service_name=p_name,
        primary_team_id=pt_id,
        primary_team_name=pt_name,
        is_active=new_user.is_active,
        hubspot_owner_id=new_user.hubspot_owner_id,
        agent_initials=new_user.agent_initials,
        allowed_service_ids=allowed_service_ids_map.get(new_user.user_id, []),
        allowed_services=allowed_services_map.get(new_user.user_id, []),
        allowed_team_ids=allowed_team_ids_map.get(new_user.user_id, []),
        allowed_teams=allowed_teams_map.get(new_user.user_id, [])
    )



@router.get("/role-options", response_model=List[RoleOption])
async def get_role_options(
    context: Annotated[TenantContext, Depends(get_tenant_context)]
):
    """Retrieve available role options for the UI based on actor permissions."""
    actor_role = context.normalized_role
    if actor_role not in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN, InternalRole.SERVICE_MANAGER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado."
        )

    options = [
        {"value": "super_admin", "label": "Superadministrador", "requires_company": False, "global_field": True},
        {"value": "company_admin", "label": "Administrador de empresa", "requires_company": True, "global_field": False},
        {"value": "responsable_servicio", "label": "Responsable de servicio", "requires_company": True, "global_field": False},
        {"value": "coordinador_equipo", "label": "Coordinador de equipo", "requires_company": True, "global_field": False},
        {"value": "agente", "label": "Agente", "requires_company": True, "global_field": False},
        {"value": "usuario", "label": "Usuario", "requires_company": True, "global_field": False}
    ]

    if actor_role == InternalRole.COMPANY_ADMIN:
        options = [opt for opt in options if opt["value"] != "super_admin"]
    elif actor_role == InternalRole.SERVICE_MANAGER:
        options = [opt for opt in options if opt["value"] in ("coordinador_equipo", "agente", "usuario")]

    return [
        RoleOption(
            value=opt["value"],
            label=opt["label"],
            requires_company=opt["requires_company"],
            global_field=opt["global_field"]
        ) for opt in options
    ]


@router.patch("/{user_id}", response_model=AdminUserResponse)
async def update_user(
    user_id: int,
    payload: AdminUserUpdatePayload,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Update details of a user, enforcing multi-tenant security rules."""
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado."
        )

    if context.normalized_role not in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Administrador de Empresa o Super Administrador."
        )

    # 1. Determine target role and company after update to validate constraints
    target_role = payload.role if payload.role is not None else user.role
    val = target_role.strip().lower()
    if val not in ROLE_MAPPINGS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Rol no válido: '{target_role}'."
        )
    target_role_norm = ROLE_MAPPINGS[val]

    # Prevent deactivating/degrading the last active super_admin
    is_target_active_super = user.is_active and normalize_role(user.role) == InternalRole.SUPER_ADMIN
    would_deactivate = payload.is_active is False
    would_degrade = is_target_active_super and payload.role is not None and normalize_role(payload.role) != InternalRole.SUPER_ADMIN
    if is_target_active_super and (would_deactivate or would_degrade):
        all_users_stmt = select(User).where(User.is_active == True)
        all_res = await db.execute(all_users_stmt)
        active_users = all_res.scalars().all()
        active_supers_count = sum(1 for u in active_users if normalize_role(u.role) == InternalRole.SUPER_ADMIN)
        if active_supers_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se puede desactivar o degradar al único Super Administrador activo del sistema."
            )

    # If actor is company_admin or service_manager:
    if context.normalized_role == InternalRole.COMPANY_ADMIN:
        if user.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para modificar usuarios de otra empresa."
            )
        if payload.company_id is not None and payload.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para cambiar la empresa del usuario."
            )
        if target_role_norm == InternalRole.SUPER_ADMIN:
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
    elif context.normalized_role == InternalRole.SERVICE_MANAGER:
        if user.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para modificar usuarios de otra empresa."
            )
        if normalize_role(user.role) in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para modificar a un Administrador de Empresa o Superadministrador."
            )
        if target_role_norm in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para asignar el rol de Administrador de Empresa o Superadministrador."
            )
        if payload.company_id is not None and payload.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para cambiar la empresa del usuario."
            )
    else:
        if payload.company_id is not None:
            comp_stmt = select(Company).where(Company.company_id == payload.company_id)
            comp_res = await db.execute(comp_stmt)
            if not comp_res.scalars().first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="La empresa especificada no existe."
                )

    # Determine final company_id to write
    if target_role_norm == InternalRole.SUPER_ADMIN:
        user.company_id = None
        target_company_id = None
    else:
        resolved_company_id = payload.company_id if payload.company_id is not None else user.company_id
        if resolved_company_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Se requiere especificar un company_id para roles que no sean super_admin."
            )
        user.company_id = resolved_company_id
        target_company_id = resolved_company_id

    target_primary_id = payload.primary_service_id if payload.primary_service_id is not None else user.primary_service_id
    target_allowed_ids = payload.allowed_service_ids

    val_primary_id, val_allowed_ids = await validate_user_services(
        db,
        role=target_role,
        company_id=target_company_id,
        primary_service_id=target_primary_id,
        allowed_service_ids=target_allowed_ids,
        context=context,
        is_update=True,
        existing_user=user
    )

    user.primary_service_id = val_primary_id
    if payload.allowed_service_ids is not None or payload.primary_service_id is not None or payload.role is not None:
        await save_user_service_associations(db, user.user_id, val_allowed_ids)

    # Validate and save team associations
    val_primary_team_id, val_allowed_team_ids = await validate_user_teams(
        db,
        role=target_role,
        company_id=target_company_id,
        primary_team_id=payload.primary_team_id,
        allowed_team_ids=payload.allowed_team_ids,
        context=context,
        is_update=True,
        existing_user=user
    )
    user.primary_team_id = val_primary_team_id
    if payload.allowed_team_ids is not None or payload.primary_team_id is not None or payload.role is not None:
        await save_user_team_associations(db, user.user_id, val_allowed_team_ids)

    # Perform update fields
    if payload.name is not None:
        user.name = payload.name.strip()

    if payload.email is not None:
        email_str = payload.email.strip().lower()
        if email_str != user.email:
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

    if payload.role is not None:
        user.role = payload.role

    await db.commit()
    await db.refresh(user)

    # Fetch fresh company name and associations to format response
    comp_stmt = select(Company.company_name).where(Company.company_id == user.company_id)
    comp_res = await db.execute(comp_stmt)
    comp_name = comp_res.scalar()

    allowed_service_ids_map, allowed_services_map, primary_service_map = await get_user_services_info(db, [user.user_id])
    p_id, p_name = primary_service_map.get(user.user_id, (val_primary_id, None))
    allowed_team_ids_map, allowed_teams_map, primary_team_map = await get_user_teams_info(db, [user.user_id])
    pt_id, pt_name = primary_team_map.get(user.user_id, (user.primary_team_id, None))

    return AdminUserResponse(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        name=user.name,
        role=user.role,
        normalized_role=normalize_role(user.role).value,
        company_id=user.company_id,
        company_name=comp_name,
        primary_service_id=p_id,
        primary_service_name=p_name,
        primary_team_id=pt_id,
        primary_team_name=pt_name,
        is_active=user.is_active,
        hubspot_owner_id=user.hubspot_owner_id,
        agent_initials=user.agent_initials,
        allowed_service_ids=allowed_service_ids_map.get(user.user_id, []),
        allowed_services=allowed_services_map.get(user.user_id, []),
        allowed_team_ids=allowed_team_ids_map.get(user.user_id, []),
        allowed_teams=allowed_teams_map.get(user.user_id, [])
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
