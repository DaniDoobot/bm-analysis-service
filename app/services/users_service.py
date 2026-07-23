"""
Service helper module for user management, service assignment validation, and response enrichment.
"""
from typing import List, Optional, Tuple, Dict, Any
from fastapi import HTTPException, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.users import User
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.core.roles import normalize_role, InternalRole
from app.core.tenant_context import TenantContext


async def validate_user_services(
    db: AsyncSession,
    role: str,
    company_id: Optional[int],
    primary_service_id: Optional[int],
    allowed_service_ids: Optional[List[int]],
    context: TenantContext,
    is_update: bool = False,
    existing_user: Optional[User] = None
) -> Tuple[Optional[int], List[int]]:
    """
    Validate primary_service_id and allowed_service_ids based on user role and company scoping.
    Returns (validated_primary_service_id, validated_allowed_service_ids).
    """
    norm_role = normalize_role(role)
    target_company_id = company_id if company_id is not None else (existing_user.company_id if existing_user else context.company_id)
    
    # 1. Role requirements validation
    if norm_role == InternalRole.SERVICE_MANAGER:
        target_primary = primary_service_id
        if is_update and target_primary is None and existing_user is not None:
            target_primary = existing_user.primary_service_id
            
        if target_primary is None and allowed_service_ids:
            target_primary = allowed_service_ids[0]

        if target_primary is None and target_company_id is not None:
            fb_stmt = select(Service.service_id).where(
                Service.company_id == target_company_id,
                Service.is_active == True
            ).order_by(Service.service_id.asc()).limit(1)
            fb_res = await db.execute(fb_stmt)
            target_primary = fb_res.scalar()

        if target_primary is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Se requiere 'primary_service_id' para el rol Responsable de Servicio."
            )
        primary_service_id = target_primary

    # Collect all service IDs to validate
    all_service_ids = set()
    if primary_service_id is not None:
        all_service_ids.add(primary_service_id)
    if allowed_service_ids is not None:
        all_service_ids.update(allowed_service_ids)
    
    if not all_service_ids:
        return primary_service_id, []

    # 2. Query services and validate existence & company scoping
    stmt = select(Service).where(Service.service_id.in_(all_service_ids))
    res = await db.execute(stmt)
    services_found = {s.service_id: s for s in res.scalars().all()}

    missing = all_service_ids - set(services_found.keys())
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Los siguientes servicios no existen: {sorted(list(missing))}."
        )

    for s_id in all_service_ids:
        svc = services_found[s_id]
        if target_company_id is not None and svc.company_id != target_company_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El servicio {s_id} ('{svc.service_name}') no pertenece a la empresa {target_company_id} del usuario."
            )
        if not context.is_super_admin and context.company_id is not None and svc.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Acceso denegado: No tienes permisos sobre el servicio {s_id}."
            )

    # Ensure primary_service_id is included in allowed_service_ids if provided
    final_allowed = set(allowed_service_ids) if allowed_service_ids is not None else set()
    if primary_service_id is not None:
        final_allowed.add(primary_service_id)

    return primary_service_id, sorted(list(final_allowed))


async def save_user_service_associations(
    db: AsyncSession,
    user_id: int,
    allowed_service_ids: List[int]
) -> None:
    """
    Sync UserServiceAssociation records for the given user_id.
    """
    existing_stmt = select(UserServiceAssociation.service_id).where(UserServiceAssociation.user_id == user_id)
    existing_res = await db.execute(existing_stmt)
    existing_svc_ids = set(existing_res.scalars().all())

    target_svc_ids = set(allowed_service_ids)

    to_remove = existing_svc_ids - target_svc_ids
    to_add = target_svc_ids - existing_svc_ids

    if to_remove:
        await db.execute(
            delete(UserServiceAssociation).where(
                (UserServiceAssociation.user_id == user_id) &
                (UserServiceAssociation.service_id.in_(to_remove))
            )
        )
    for s_id in to_add:
        db.add(UserServiceAssociation(user_id=user_id, service_id=s_id))


async def get_user_services_info(
    db: AsyncSession,
    user_ids: List[int]
) -> Tuple[Dict[int, List[int]], Dict[int, List[Dict[str, Any]]], Dict[int, Tuple[Optional[int], Optional[str]]]]:
    """
    Fetch user service information for a list of user IDs.
    Returns:
    - allowed_service_ids_map: {user_id: [service_id, ...]}
    - allowed_services_map: {user_id: [{"service_id": id, "service_name": name}, ...]}
    - primary_service_map: {user_id: (primary_service_id, primary_service_name)}
    """
    if not user_ids:
        return {}, {}, {}

    users_res = await db.execute(select(User).where(User.user_id.in_(user_ids)))
    users = users_res.scalars().all()

    primary_service_ids = {u.primary_service_id for u in users if u.primary_service_id is not None}
    
    assoc_res = await db.execute(
        select(UserServiceAssociation).where(UserServiceAssociation.user_id.in_(user_ids))
    )
    assocs = assoc_res.scalars().all()

    user_assoc_ids: Dict[int, set] = {}
    for sa in assocs:
        user_assoc_ids.setdefault(sa.user_id, set()).add(sa.service_id)

    # Fallback para service_manager / team_coordinator sin primary_service_id ni asociaciones
    comp_needing_fallback = {
        u.company_id for u in users
        if normalize_role(u.role) in (InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR)
        and u.primary_service_id is None
        and u.company_id is not None
        and u.user_id not in user_assoc_ids
    }
    fallback_service_map: Dict[int, Tuple[int, str]] = {}
    if comp_needing_fallback:
        fb_stmt = select(Service.company_id, Service.service_id, Service.service_name).where(
            Service.company_id.in_(comp_needing_fallback),
            Service.is_active == True
        ).order_by(Service.service_id.asc())
        fb_res = await db.execute(fb_stmt)
        for row in fb_res.all():
            if row.company_id not in fallback_service_map:
                fallback_service_map[row.company_id] = (row.service_id, row.service_name)

    all_service_ids = primary_service_ids | {sa.service_id for sa in assocs} | {fb[0] for fb in fallback_service_map.values()}

    service_name_map = {}
    if all_service_ids:
        svc_res = await db.execute(select(Service).where(Service.service_id.in_(all_service_ids)))
        service_name_map = {s.service_id: s.service_name for s in svc_res.scalars().all()}

    allowed_service_ids_map: Dict[int, List[int]] = {}
    allowed_services_map: Dict[int, List[Dict[str, Any]]] = {}
    primary_service_map: Dict[int, Tuple[Optional[int], Optional[str]]] = {}

    for u in users:
        norm_r = normalize_role(u.role)
        p_id = u.primary_service_id
        p_name = service_name_map.get(p_id) if p_id is not None else None

        if p_id is None and norm_r in (InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR) and u.company_id in fallback_service_map:
            p_id, p_name = fallback_service_map[u.company_id]

        primary_service_map[u.user_id] = (p_id, p_name)

    for u in users:
        uid = u.user_id
        s_ids = set(user_assoc_ids.get(uid, set()))
        p_id, p_name = primary_service_map.get(uid, (None, None))
        if p_id is not None:
            s_ids.add(p_id)
            if p_id not in service_name_map and p_name is not None:
                service_name_map[p_id] = p_name
        
        sorted_ids = sorted(list(s_ids))
        allowed_service_ids_map[uid] = sorted_ids
        allowed_services_map[uid] = [
            {"service_id": s_id, "service_name": service_name_map.get(s_id, f"Servicio {s_id}")}
            for s_id in sorted_ids
        ]

    return allowed_service_ids_map, allowed_services_map, primary_service_map


async def validate_user_teams(
    db: AsyncSession,
    role: str,
    company_id: Optional[int],
    primary_team_id: Optional[int],
    allowed_team_ids: Optional[List[int]],
    context: TenantContext,
    is_update: bool = False,
    existing_user: Optional[User] = None
) -> Tuple[Optional[int], List[int]]:
    """
    Validate primary_team_id and allowed_team_ids based on user role, company, and service manager scoping.
    Returns (validated_primary_team_id, validated_allowed_team_ids).
    """
    norm_role = normalize_role(role)
    target_company_id = company_id if company_id is not None else (existing_user.company_id if existing_user else context.company_id)

    # 1. Role requirements validation for TEAM_COORDINATOR
    if norm_role == InternalRole.TEAM_COORDINATOR:
        target_primary = primary_team_id
        if is_update and target_primary is None and existing_user is not None:
            target_primary = existing_user.primary_team_id

        if target_primary is None and allowed_team_ids:
            target_primary = allowed_team_ids[0]

        if target_primary is None and target_company_id is not None:
            fb_stmt = select(Team.team_id).where(
                Team.company_id == target_company_id,
                Team.is_active == True
            ).order_by(Team.team_id.asc()).limit(1)
            fb_res = await db.execute(fb_stmt)
            target_primary = fb_res.scalar()

        if target_primary is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Se requiere 'primary_team_id' para el rol Coordinador de Equipo."
            )
        primary_team_id = target_primary

    # Collect all team IDs to validate
    all_team_ids = set()
    if primary_team_id is not None:
        all_team_ids.add(primary_team_id)
    if allowed_team_ids is not None:
        all_team_ids.update(allowed_team_ids)

    if not all_team_ids:
        return primary_team_id, []

    # 2. Query teams and validate existence & scoping
    stmt = select(Team).where(Team.team_id.in_(all_team_ids))
    res = await db.execute(stmt)
    teams_found = {t.team_id: t for t in res.scalars().all()}

    missing = all_team_ids - set(teams_found.keys())
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Los siguientes equipos no existen: {sorted(list(missing))}."
        )

    for t_id in all_team_ids:
        t = teams_found[t_id]
        if target_company_id is not None and t.company_id != target_company_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El equipo {t_id} ('{t.team_name}') no pertenece a la empresa {target_company_id} del usuario."
            )
        if not context.is_super_admin and context.company_id is not None and t.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Acceso denegado: No tienes permisos sobre el equipo {t_id}."
            )
        # If actor is service_manager, team must belong to allowed_service_ids
        if context.normalized_role == InternalRole.SERVICE_MANAGER and context.allowed_service_ids is not None:
            if t.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acceso denegado: El equipo {t_id} ('{t.team_name}') pertenece al servicio {t.service_id}, sobre el cual no tienes permisos."
                )
        # If actor is team_coordinator, team must belong to context.allowed_team_ids
        if context.normalized_role == InternalRole.TEAM_COORDINATOR and context.allowed_team_ids is not None:
            if t_id not in context.allowed_team_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acceso denegado: El equipo {t_id} ('{t.team_name}') no pertenece a tus equipos asignados."
                )

    # Ensure primary_team_id is included in allowed_team_ids if provided
    final_allowed = set(allowed_team_ids) if allowed_team_ids is not None else set()
    if primary_team_id is not None:
        final_allowed.add(primary_team_id)

    return primary_team_id, sorted(list(final_allowed))


async def save_user_team_associations(
    db: AsyncSession,
    user_id: int,
    allowed_team_ids: List[int],
    role: str = "agente"
) -> None:
    """
    Sync UserTeamAssociation or AgentTeamAssociation records for the given user_id depending on role.
    """
    norm_r = normalize_role(role)
    assoc_model = AgentTeamAssociation if norm_r == InternalRole.AGENT else UserTeamAssociation

    existing_stmt = select(assoc_model.team_id).where(assoc_model.user_id == user_id)
    existing_res = await db.execute(existing_stmt)
    existing_team_ids = set(existing_res.scalars().all())

    target_team_ids = set(allowed_team_ids)

    to_remove = existing_team_ids - target_team_ids
    to_add = target_team_ids - existing_team_ids

    if to_remove:
        await db.execute(
            delete(assoc_model).where(
                (assoc_model.user_id == user_id) &
                (assoc_model.team_id.in_(to_remove))
            )
        )
    for t_id in to_add:
        db.add(assoc_model(user_id=user_id, team_id=t_id))


async def get_user_teams_info(
    db: AsyncSession,
    user_ids: List[int]
) -> Tuple[Dict[int, List[int]], Dict[int, List[Dict[str, Any]]], Dict[int, Tuple[Optional[int], Optional[str]]]]:
    """
    Fetch user team information for a list of user IDs.
    Returns:
    - allowed_team_ids_map: {user_id: [team_id, ...]}
    - allowed_teams_map: {user_id: [{"team_id": id, "team_name": name, "service_id": svc_id}, ...]}
    - primary_team_map: {user_id: (primary_team_id, primary_team_name)}
    """
    if not user_ids:
        return {}, {}, {}

    users_res = await db.execute(select(User).where(User.user_id.in_(user_ids)))
    users = users_res.scalars().all()

    primary_team_ids = {u.primary_team_id for u in users if u.primary_team_id is not None}

    assoc_res = await db.execute(
        select(UserTeamAssociation).where(UserTeamAssociation.user_id.in_(user_ids))
    )
    assocs = assoc_res.scalars().all()

    user_assoc_ids: Dict[int, set] = {}
    for ta in assocs:
        user_assoc_ids.setdefault(ta.user_id, set()).add(ta.team_id)

    all_team_ids = primary_team_ids | {ta.team_id for ta in assocs}

    team_obj_map = {}
    if all_team_ids:
        team_res = await db.execute(select(Team).where(Team.team_id.in_(all_team_ids)))
        team_obj_map = {t.team_id: t for t in team_res.scalars().all()}

    allowed_team_ids_map: Dict[int, List[int]] = {}
    allowed_teams_map: Dict[int, List[Dict[str, Any]]] = {}
    primary_team_map: Dict[int, Tuple[Optional[int], Optional[str]]] = {}

    for u in users:
        p_id = u.primary_team_id
        t_obj = team_obj_map.get(p_id) if p_id is not None else None
        p_name = t_obj.team_name if t_obj else None
        primary_team_map[u.user_id] = (p_id, p_name)

        t_ids = set(user_assoc_ids.get(u.user_id, set()))
        if p_id is not None:
            t_ids.add(p_id)
        sorted_ids = sorted(list(t_ids))
        allowed_team_ids_map[u.user_id] = sorted_ids
        allowed_teams_map[u.user_id] = [
            {
                "team_id": t_id,
                "team_name": team_obj_map[t_id].team_name if t_id in team_obj_map else f"Equipo {t_id}",
                "service_id": team_obj_map[t_id].service_id if t_id in team_obj_map else None
            }
            for t_id in sorted_ids
        ]

    return allowed_team_ids_map, allowed_teams_map, primary_team_map


def check_can_manage_target_user(context: TenantContext, target_user: User) -> None:
    """
    Verify if context user has permission to manage (e.g. generate password links, edit, reset) target_user.
    Raises HTTPException(403) if forbidden.
    """
    if context.is_super_admin:
        return

    # Non-superadmin must be in the same company
    if target_user.company_id != context.company_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: No tienes permisos sobre usuarios de otra empresa."
        )

    actor_role = context.normalized_role
    target_role = normalize_role(target_user.role)

    if actor_role == InternalRole.COMPANY_ADMIN:
        if target_role == InternalRole.SUPER_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para gestionar a un Super Administrador."
            )
        return

    if actor_role == InternalRole.SERVICE_MANAGER:
        if target_role in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN) and target_user.user_id != context.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para gestionar a Administradores de Empresa o Superadministradores."
            )
        if context.allowed_service_ids is not None and target_user.primary_service_id is not None:
            if target_user.primary_service_id not in context.allowed_service_ids and target_user.user_id != context.user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: El usuario no pertenece a tus servicios permitidos."
                )
        return

    if actor_role == InternalRole.TEAM_COORDINATOR:
        if target_role != InternalRole.AGENT and target_user.user_id != context.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: Un coordinador de equipo solo puede gestionar agentes de sus equipos."
            )
        if context.allowed_team_ids is not None and target_user.primary_team_id is not None:
            if target_user.primary_team_id not in context.allowed_team_ids and target_user.user_id != context.user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: El usuario no pertenece a tus equipos permitidos."
                )
        return

    if target_user.user_id != context.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requieren permisos de gestión."
        )
