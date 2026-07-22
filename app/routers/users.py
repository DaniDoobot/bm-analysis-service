"""
FastAPI router for user management — CRUD protected by Bearer admin token.
All write endpoints require role='administrador'.
"""
import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, require_admin, get_tenant_context
from app.models.users import User, UserAudit, PasswordResetToken
from app.models.companies import Company
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole, normalize_role
from app.models.prompts import Prompt, PromptBaseStructure, StructurePermission
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingCallSession,
    TrainingCallEvaluation,
)
from app.models.mass_evaluations import MassEvaluationResult
from app.models.analyses import Analysis
from app.services.auth_service import log_audit
from app.services.users_service import validate_user_services, save_user_service_associations, get_user_services_info
from app.schemas.users import (
    UserOut,
    UserOutFull,
    UserCreatePayload,
    UserUpdatePayload,
    UserAdminResetPasswordPayload,
    AdminPasswordResetPayload,
    EligibleUserOut,
    PasswordSetupLinkResponse,
    UserPasswordSetupMode,
)
from app.utils.security import hash_password

logger = logging.getLogger(__name__)


async def handle_user_ownership_transfer(db: AsyncSession, user_id: int, transfer_owner_id: int | None, actor_user_id: int):
    # Fetch owned base structures
    base_res = await db.execute(select(PromptBaseStructure).where(PromptBaseStructure.owner_user_id == user_id))
    owned_bases = base_res.scalars().all()

    # Fetch owned specific structures
    spec_res = await db.execute(select(Prompt).where(Prompt.owner_user_id == user_id))
    owned_specifics = spec_res.scalars().all()

    if owned_bases or owned_specifics:
        if not transfer_owner_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El usuario es propietario de una o más estructuras. Debes especificar un nuevo propietario (transfer_owner_id) para transferir los recursos antes de la desactivación."
            )
        
        # Validate new owner
        stmt_u = select(User).where(User.user_id == transfer_owner_id)
        res_u = await db.execute(stmt_u)
        new_owner = res_u.scalars().first()
        
        if not new_owner:
            raise HTTPException(status_code=400, detail="Nuevo propietario no encontrado.")
        if not new_owner.is_active:
            raise HTTPException(status_code=400, detail="El nuevo propietario debe estar activo.")
        if new_owner.role == "agent":
            raise HTTPException(status_code=400, detail="El nuevo propietario no puede ser un agente.")
            
        # Transfer bases
        for b in owned_bases:
            old_owner = b.owner_user_id
            b.owner_user_id = transfer_owner_id
            db.add(b)
            # Remove redundant manual permission
            await db.execute(delete(StructurePermission).where(
                StructurePermission.structure_type == "base",
                StructurePermission.structure_id == b.id,
                StructurePermission.user_id == transfer_owner_id
            ))
            # Log audit
            await log_audit(db, actor_user_id, "transfer", "base", b.id, affected_user_id=transfer_owner_id, details={"previous_owner_id": old_owner})

        # Transfer specifics
        for s in owned_specifics:
            old_owner = s.owner_user_id
            s.owner_user_id = transfer_owner_id
            db.add(s)
            # Remove redundant manual permission
            await db.execute(delete(StructurePermission).where(
                StructurePermission.structure_type == "specific",
                StructurePermission.structure_id == s.prompt_id,
                StructurePermission.user_id == transfer_owner_id
            ))
            # Log audit
            await log_audit(db, actor_user_id, "transfer", "specific", s.prompt_id, affected_user_id=transfer_owner_id, details={"previous_owner_id": old_owner})
            
        await db.commit()


async def check_hubspot_owner_id_exists(db: AsyncSession, owner_id: str) -> bool:
    owner_id = str(owner_id).strip()
    if not owner_id:
        return False
        
    # Check settings
    stmt1 = select(1).select_from(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == owner_id).limit(1)
    if (await db.execute(stmt1)).scalar():
        return True
        
    # Check reports
    stmt2 = select(1).select_from(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id == owner_id).limit(1)
    if (await db.execute(stmt2)).scalar():
        return True
        
    # Check mass evaluations
    stmt3 = select(1).select_from(MassEvaluationResult).where(MassEvaluationResult.hubspot_owner_id == owner_id).limit(1)
    if (await db.execute(stmt3)).scalar():
        return True

    # Check analyses
    stmt4 = select(1).select_from(Analysis).where(Analysis.hubspot_owner_id == owner_id).limit(1)
    if (await db.execute(stmt4)).scalar():
        return True

    # Check call sessions
    stmt5 = select(1).select_from(TrainingCallSession).where(TrainingCallSession.agent_id == owner_id).limit(1)
    if (await db.execute(stmt5)).scalar():
        return True

    # Check call evaluations
    stmt6 = select(1).select_from(TrainingCallEvaluation).where(TrainingCallEvaluation.agent_id == owner_id).limit(1)
    if (await db.execute(stmt6)).scalar():
        return True

    return False


async def validate_hubspot_owner_id(
    db: AsyncSession,
    role: str | None,
    hubspot_owner_id: str | None,
    user_id: int | None = None,
    allow_unverified: bool = False
):
    if hubspot_owner_id is not None:
        hubspot_owner_id = str(hubspot_owner_id).strip()
        if hubspot_owner_id == "":
            hubspot_owner_id = None

    is_agent = role in ["agent", "agente"]

    if is_agent and not hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El ID de HubSpot es obligatorio para los usuarios con rol agente."
        )

    if hubspot_owner_id:
        # Check uniqueness
        stmt = select(User).where(User.hubspot_owner_id == hubspot_owner_id)
        if user_id is not None:
            stmt = stmt.where(User.user_id != user_id)
        existing_user = (await db.execute(stmt)).scalars().first()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Este agente de HubSpot ya está asignado a otro usuario."
            )

        # Check existence in inventory
        if not allow_unverified:
            exists = await check_hubspot_owner_id_exists(db, hubspot_owner_id)
            if not exists:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No existe ningún agente conocido con ese ID de HubSpot."
                )


router = APIRouter(prefix="/bm/users", tags=["User Management"])


@router.get("/sharing/eligible-users", response_model=list[EligibleUserOut])
async def list_eligible_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")

    stmt = select(User).where(User.is_active == True, User.role != "agent")
    res = await db.execute(stmt)
    users = res.scalars().all()
    
    return [
        {
            "user_id": u.user_id,
            "username": u.username,
            "email": u.email,
            "role": u.role
        }
        for u in users
    ]


def _user_to_full(
    u: User,
    comp_map: dict | None = None,
    allowed_service_ids_map: dict | None = None,
    allowed_services_map: dict | None = None,
    primary_service_map: dict | None = None
) -> dict:
    c_name = None
    if comp_map and u.company_id is not None:
        c_name = comp_map.get(u.company_id)

    p_id = u.primary_service_id
    p_name = None
    if primary_service_map and u.user_id in primary_service_map:
        p_id, p_name = primary_service_map[u.user_id]

    svc_ids = (allowed_service_ids_map or {}).get(u.user_id, [])
    if p_id is not None and p_id not in svc_ids:
        svc_ids = sorted(list(set(svc_ids + [p_id])))

    svcs = (allowed_services_map or {}).get(u.user_id, [])

    return {
        "id": u.user_id,
        "user_id": u.user_id,
        "name": u.name,
        "username": u.username,
        "email": u.email,
        "role": u.role,
        "normalized_role": normalize_role(u.role).value,
        "company_id": u.company_id,
        "company_name": c_name,
        "primary_service_id": p_id,
        "primary_service_name": p_name,
        "allowed_service_ids": svc_ids,
        "allowed_services": svcs,
        "is_active": u.is_active,
        "hubspot_owner_id": u.hubspot_owner_id,
        "agent_initials": u.agent_initials,
        "password_masked": "********",
        "must_reset_password": u.must_reset_password,
        "password_set_at": u.password_set_at.isoformat() if u.password_set_at else None,
        "reset_token_expires_at": u.reset_token_expires_at.isoformat() if u.reset_token_expires_at else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "updated_at": u.updated_at.isoformat() if u.updated_at else None,
    }


# ── GET /bm/users ──────────────────────────────────────────────────────────────

@router.get("")
async def list_users(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    List users enforcing multi-tenant scoping:
    - super_admin: sees all users.
    - company_admin: sees users of their company_id, excluding super_admins or global NULL company users.
    - non-admin: basic public info of users in their company/scope.
    """
    stmt = select(User).order_by(User.user_id.asc())

    if context.is_super_admin:
        pass
    elif context.normalized_role == InternalRole.COMPANY_ADMIN:
        stmt = stmt.where(
            (User.company_id == context.company_id) &
            (User.company_id.is_not(None))
        )
        super_admin_roles = ["admin", "administrador", "superadmin", "super_admin"]
        stmt = stmt.where(~User.role.in_(super_admin_roles))
    else:
        if context.company_id:
            stmt = stmt.where(User.company_id == context.company_id)

    res = await db.execute(stmt)
    users = res.scalars().all()

    user_ids = [u.user_id for u in users]
    comp_ids = list({u.company_id for u in users if u.company_id is not None})
    comp_map = {}
    if comp_ids:
        c_res = await db.execute(select(Company).where(Company.company_id.in_(comp_ids)))
        comp_map = {c.company_id: c.company_name for c in c_res.scalars().all()}

    allowed_service_ids_map, allowed_services_map, primary_service_map = await get_user_services_info(db, user_ids)

    is_admin_user = context.is_super_admin or context.normalized_role == InternalRole.COMPANY_ADMIN
    if is_admin_user:
        return {
            "ok": True,
            "total": len(users),
            "users": [
                _user_to_full(
                    u,
                    comp_map=comp_map,
                    allowed_service_ids_map=allowed_service_ids_map,
                    allowed_services_map=allowed_services_map,
                    primary_service_map=primary_service_map
                )
                for u in users
            ]
        }

    return {
        "ok": True,
        "total": len(users),
        "users": [
            {
                "id": u.user_id,
                "user_id": u.user_id,
                "name": u.name,
                "username": u.username,
                "email": u.email,
                "role": u.role,
                "normalized_role": normalize_role(u.role).value,
                "company_id": u.company_id,
                "company_name": comp_map.get(u.company_id),
                "primary_service_id": primary_service_map.get(u.user_id, (None, None))[0],
                "primary_service_name": primary_service_map.get(u.user_id, (None, None))[1],
                "allowed_service_ids": allowed_service_ids_map.get(u.user_id, []),
                "allowed_services": allowed_services_map.get(u.user_id, []),
                "is_active": u.is_active,
                "hubspot_owner_id": u.hubspot_owner_id,
                "agent_initials": u.agent_initials,
            }
            for u in users
        ],
    }


# ── GET /bm/users/{user_id} ────────────────────────────────────────────────────

@router.get("/{user_id}")
async def get_user(
    user_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get a single user by ID. Admins see full detail; others can only see their own."""
    is_admin_user = context.is_super_admin or context.normalized_role == InternalRole.COMPANY_ADMIN
    if not is_admin_user and current_user.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo puedes ver tu propio perfil.",
        )
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado.")

    if not context.is_super_admin and context.normalized_role == InternalRole.COMPANY_ADMIN:
        if user.company_id != context.company_id or normalize_role(user.role) == InternalRole.SUPER_ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: No tienes permisos sobre este usuario.")

    comp_map = {}
    if user.company_id:
        c_res = await db.execute(select(Company.company_name).where(Company.company_id == user.company_id))
        c_name = c_res.scalar()
        comp_map[user.company_id] = c_name

    allowed_service_ids_map, allowed_services_map, primary_service_map = await get_user_services_info(db, [user_id])
    user_data = _user_to_full(
        user,
        comp_map=comp_map,
        allowed_service_ids_map=allowed_service_ids_map,
        allowed_services_map=allowed_services_map,
        primary_service_map=primary_service_map
    )
    return {
        **user_data,
        "ok": True,
        "user": user_data
    }



# ── POST /bm/users ─────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreatePayload,
    admin: Annotated[User, Depends(require_admin)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    allow_unverified_hubspot_id: bool = Query(False, description="Permite omitir la comprobación de existencia del ID de HubSpot en el inventario real")
):
    """Create a new user in bm_users enforcing company_admin scoping."""
    target_role_norm = normalize_role(body.role)

    # 1. Company admin permissions check
    if not context.is_super_admin:
        if target_role_norm == InternalRole.SUPER_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para crear un Super Administrador."
            )
        if body.company_id is not None and body.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Solo puedes crear usuarios en tu propia empresa."
            )
        company_id = context.company_id
    else:
        if target_role_norm == InternalRole.SUPER_ADMIN:
            company_id = None
        else:
            company_id = body.company_id or context.company_id

    val_primary_id, val_allowed_ids = await validate_user_services(
        db,
        role=body.role,
        company_id=company_id,
        primary_service_id=body.primary_service_id,
        allowed_service_ids=body.allowed_service_ids,
        context=context
    )

    username = (body.username or body.name or body.email.split("@")[0]).strip()

    clean_hs_id = body.hubspot_owner_id
    if clean_hs_id is not None:
        clean_hs_id = str(clean_hs_id).strip()
        if clean_hs_id == "":
            clean_hs_id = None

    await validate_hubspot_owner_id(
        db,
        role=body.role,
        hubspot_owner_id=clean_hs_id,
        allow_unverified=allow_unverified_hubspot_id
    )

    stmt_email = select(User).where(User.email == body.email)
    if (await db.execute(stmt_email)).scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un usuario con email '{body.email}'.",
        )

    stmt_uname = select(User).where(User.username == username)
    if (await db.execute(stmt_uname)).scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un usuario con username '{username}'.",
        )

    must_reset_password = body.must_reset_password
    if body.password_setup in {UserPasswordSetupMode.invite_link, UserPasswordSetupMode.temporary_password}:
        must_reset_password = True

    token = None
    expires_at = None
    pass_hash = None

    if body.password_setup == UserPasswordSetupMode.invite_link:
        temp_pass = secrets.token_urlsafe(32)
        pass_hash = hash_password(temp_pass)
    elif body.password_setup == UserPasswordSetupMode.temporary_password:
        if body.password:
            pass_hash = hash_password(body.password)
        else:
            temp_pass = secrets.token_urlsafe(12)
            pass_hash = hash_password(temp_pass)
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    else:
        if must_reset_password:
            token = secrets.token_urlsafe(32)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
            temp_pass = secrets.token_urlsafe(32)
            pass_hash = hash_password(temp_pass)
        else:
            pass_hash = hash_password(body.password)

    new_user = User(
        username=username,
        email=body.email,
        name=body.name,
        role=body.role,
        company_id=company_id,
        primary_service_id=val_primary_id,
        is_active=body.is_active,
        hubspot_owner_id=clean_hs_id,
        agent_initials=body.agent_initials,
        password_hash=pass_hash,
        password_plain_dev=None,
        must_reset_password=must_reset_password,
        reset_token=token,
        reset_token_expires_at=expires_at,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    await save_user_service_associations(db, new_user.user_id, val_allowed_ids)
    await db.commit()

    logger.info("Admin %s CREATED user %s (id=%s, company_id=%s)", admin.email, new_user.email, new_user.user_id, company_id)
    
    allowed_service_ids_map, allowed_services_map, primary_service_map = await get_user_services_info(db, [new_user.user_id])
    user_data = _user_to_full(
        new_user,
        allowed_service_ids_map=allowed_service_ids_map,
        allowed_services_map=allowed_services_map,
        primary_service_map=primary_service_map
    )
    resp = {"ok": True, "action": "created", "user": user_data}
    if token:
        from app.config import get_settings
        settings = get_settings()
        resp["reset_token"] = token
        resp["reset_url"] = f"{settings.frontend_public_url.rstrip('/')}/reset-password?token={token}"
    return resp


# ── PATCH /bm/users/{user_id} ──────────────────────────────────────────────────

@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    body: UserUpdatePayload,
    admin: Annotated[User, Depends(require_admin)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    transfer_owner_id: Annotated[int | None, Query(description="ID of the new owner to transfer structures to if deactivating")] = None,
    allow_unverified_hubspot_id: bool = Query(False, description="Permite omitir la comprobación de existencia del ID de HubSpot en el inventario real")
):
    """
    Update email, username, role, is_active, hubspot_owner_id, or agent_initials.
    Requires administrative role (super_admin or company_admin).
    Used by Lovable for role changes, deactivation, and profile edits.
    """
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado.")

    # Scoping check for company_admin
    if not context.is_super_admin:
        if user.company_id != context.company_id or normalize_role(user.role) == InternalRole.SUPER_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para modificar este usuario."
            )
        if body.role is not None and normalize_role(body.role) == InternalRole.SUPER_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para asignar el rol Super Administrador."
            )
        if body.company_id is not None and body.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para cambiar la empresa del usuario."
            )

    target_role = body.role if body.role is not None else user.role
    target_company_id = body.company_id if body.company_id is not None else user.company_id
    target_primary_id = body.primary_service_id if "primary_service_id" in body.model_fields_set else user.primary_service_id
    target_allowed_ids = body.allowed_service_ids if "allowed_service_ids" in body.model_fields_set else None

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

    # 1. Protect against deactivating oneself
    if body.is_active is False and admin.user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No puedes desactivar tu propio usuario."
        )

    # 2. Protect against deactivating or degrading the last active admin
    is_target_active_admin = user.is_active and normalize_role(user.role) in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN)
    would_deactivate = body.is_active is False
    would_degrade = is_target_active_admin and body.role is not None and normalize_role(body.role) not in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN)

    if is_target_active_admin and (would_deactivate or would_degrade):
        active_admins_stmt = select(User).where(
            User.is_active == True,
            User.company_id == user.company_id if not context.is_super_admin else True
        )
        active_users = (await db.execute(active_admins_stmt)).scalars().all()
        admin_count = sum(1 for u in active_users if normalize_role(u.role) in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN))
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se puede desactivar, eliminar o degradar al único administrador activo del sistema o empresa."
            )

    # 3. Validate HubSpot Owner ID & Role transition logic
    has_role_update = body.role is not None
    is_target_agent = target_role in ["agent", "agente"]

    # If role changes from agent to non-agent, release hubspot_owner_id
    if user.role in ["agent", "agente"] and not is_target_agent:
        clean_hs_id = None
    else:
        if "hubspot_owner_id" in body.model_fields_set:
            clean_hs_id = body.hubspot_owner_id
            if clean_hs_id is not None:
                clean_hs_id = str(clean_hs_id).strip()
                if clean_hs_id == "":
                    clean_hs_id = None
        else:
            clean_hs_id = user.hubspot_owner_id

    if is_target_agent or clean_hs_id is not None:
        await validate_hubspot_owner_id(
            db,
            role=target_role,
            hubspot_owner_id=clean_hs_id,
            user_id=user_id,
            allow_unverified=allow_unverified_hubspot_id
        )

    # 4. Perform updates and track changes
    changes = {}

    if body.email is not None:
        email_clean = body.email.strip().lower()
        if email_clean != user.email:
            import re
            EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
            if not EMAIL_REGEX.match(email_clean):
                raise HTTPException(status_code=400, detail="Formato de correo electrónico inválido.")
            
            dup = (await db.execute(
                select(User).where(func.lower(User.email) == email_clean).where(User.user_id != user_id)
            )).scalars().first()
            if dup:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Ya existe otro usuario con este correo electrónico."
                )
            changes["email"] = {"old": user.email, "new": email_clean}
            user.email = email_clean

    if body.username is not None:
        username_clean = body.username.strip()
        if username_clean != user.username:
            dup_uname = (await db.execute(
                select(User).where(User.username == username_clean).where(User.user_id != user_id)
            )).scalars().first()
            if dup_uname:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="El nombre de usuario ya está en uso."
                )
            changes["username"] = {"old": user.username, "new": username_clean}
            user.username = username_clean

    if "name" in body.model_fields_set:
        new_name = body.name
        if new_name != user.name:
            changes["name"] = {"old": user.name, "new": new_name}
            user.name = new_name

    if body.role is not None and body.role != user.role:
        changes["role"] = {"old": user.role, "new": body.role}
        user.role = body.role

    if body.company_id is not None and body.company_id != user.company_id:
        changes["company_id"] = {"old": user.company_id, "new": body.company_id}
        user.company_id = body.company_id

    if user.primary_service_id != val_primary_id:
        changes["primary_service_id"] = {"old": user.primary_service_id, "new": val_primary_id}
        user.primary_service_id = val_primary_id

    if "allowed_service_ids" in body.model_fields_set or "primary_service_id" in body.model_fields_set or body.role is not None:
        await save_user_service_associations(db, user_id, val_allowed_ids)
        changes["allowed_service_ids"] = {"new": val_allowed_ids}

    if clean_hs_id != user.hubspot_owner_id:
        changes["hubspot_owner_id"] = {"old": user.hubspot_owner_id, "new": clean_hs_id}
        user.hubspot_owner_id = clean_hs_id

    if body.agent_initials is not None and body.agent_initials != user.agent_initials:
        changes["agent_initials"] = {"old": user.agent_initials, "new": body.agent_initials}
        user.agent_initials = body.agent_initials

    if body.is_active is not None and body.is_active != user.is_active:
        if body.is_active is False and user.is_active is True:
            await handle_user_ownership_transfer(db, user_id, transfer_owner_id, admin.user_id)
        changes["is_active"] = {"old": user.is_active, "new": body.is_active}
        user.is_active = body.is_active

    if body.must_reset_password is not None and body.must_reset_password != user.must_reset_password:
        if body.must_reset_password and not user.must_reset_password:
            user.reset_token = secrets.token_urlsafe(32)
            user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        changes["must_reset_password"] = {"old": user.must_reset_password, "new": body.must_reset_password}
        user.must_reset_password = body.must_reset_password

    # 5. Log audit if there are any changes
    if changes:
        action = "update"
        if "is_active" in changes:
            action = "deactivate" if not user.is_active else "activate"
        
        audit = UserAudit(
            admin_user_id=admin.user_id,
            target_user_id=user_id,
            action=action,
            changes_json=changes
        )
        db.add(audit)

    await db.commit()
    await db.refresh(user)

    logger.info("Admin %s UPDATED user %s (id=%s). Changes: %s", admin.email, user.email, user.user_id, changes)
    allowed_service_ids_map, allowed_services_map, primary_service_map = await get_user_services_info(db, [user_id])
    user_data = _user_to_full(
        user,
        allowed_service_ids_map=allowed_service_ids_map,
        allowed_services_map=allowed_services_map,
        primary_service_map=primary_service_map
    )
    return {
        **user_data,
        "ok": True,
        "user": user_data
    }



# ── POST /bm/users/{user_id}/reset-password ────────────────────────────────────

@router.post("/{user_id}/reset-password")
async def admin_reset_password(
    user_id: int,
    body: UserAdminResetPasswordPayload,
    admin: Annotated[User, Depends(require_admin)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Reset another user's password (no current_password required).
    Requires administrative role (super_admin or company_admin).
    """
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado.")

    if not context.is_super_admin:
        if user.company_id != context.company_id or normalize_role(user.role) == InternalRole.SUPER_ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tienes permisos sobre este usuario.")

    user.password_hash = hash_password(body.new_password)
    user.password_plain_dev = None
    await db.commit()

    logger.info(
        "Admin %s RESET password for user %s (id=%s)", admin.email, user.email, user.user_id
    )
    return {"ok": True, "message": "Contraseña actualizada correctamente.", "user_id": user_id}


# ── DELETE /bm/users/{user_id} (soft delete) ──────────────────────────────────

@router.delete("/{user_id}")
async def deactivate_user(
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    transfer_owner_id: Annotated[int | None, Query(description="ID of the new owner to transfer structures to")] = None,
):
    """
    Soft-delete: set is_active=False. Does NOT physically delete the user.
    Requires administrative role (super_admin or company_admin).
    An admin cannot deactivate themselves.
    """
    if admin.user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No puedes desactivar tu propio usuario.",
        )

    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado.")

    if not context.is_super_admin:
        if user.company_id != context.company_id or normalize_role(user.role) == InternalRole.SUPER_ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tienes permisos sobre este usuario.")

    # Check if target is an active admin and protect last active admin
    if user.is_active and normalize_role(user.role) in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN):
        active_users = (await db.execute(select(User).where(User.is_active == True, User.company_id == user.company_id if not context.is_super_admin else True))).scalars().all()
        admin_count = sum(1 for u in active_users if normalize_role(u.role) in (InternalRole.SUPER_ADMIN, InternalRole.COMPANY_ADMIN))
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se puede desactivar, eliminar o degradar al único administrador activo del sistema o empresa."
            )

    # Check for owned structures and handle transfer
    if user.is_active:
        await handle_user_ownership_transfer(db, user_id, transfer_owner_id, admin.user_id)

    user.is_active = False

    # Audit log
    audit = UserAudit(
        admin_user_id=admin.user_id,
        target_user_id=user_id,
        action="deactivate",
        changes_json={"is_active": {"old": True, "new": False}}
    )
    db.add(audit)

    await db.commit()

    logger.info("Admin %s DEACTIVATED user %s (id=%s)", admin.email, user.email, user.user_id)
    return {"ok": True, "action": "deactivated", "user_id": user_id, "email": user.email}


# ── POST /bm/users/{user_id}/password-reset ────────────────────────────────────

@router.post("/{user_id}/password-reset")
async def administrative_password_reset(
    user_id: int,
    body: AdminPasswordResetPayload,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Administrative reset password endpoint.
    Forces must_reset_password=True.
    Generates a secure temporary password if none is provided.
    """
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado.")

    temp_pass = body.temp_password
    if not temp_pass:
        temp_pass = secrets.token_urlsafe(9)  # ~12 characters

    user.password_hash = hash_password(temp_pass)
    user.password_plain_dev = None
    user.must_reset_password = True
    user.reset_token = secrets.token_urlsafe(32)
    user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    # Audit log
    audit = UserAudit(
        admin_user_id=admin.user_id,
        target_user_id=user_id,
        action="password_reset",
        changes_json={"password_reset": True}
    )
    db.add(audit)
    await db.commit()

    logger.info("Admin %s reset password for user %s (id=%s)", admin.email, user.email, user.user_id)
    return {
        "ok": True,
        "message": "Contraseña restablecida con éxito. Se requerirá cambio en el próximo inicio de sesión.",
        "temp_password": temp_pass,
        "must_reset_password": True
    }


@router.post("/{user_id}/force-password-reset")
async def force_password_reset(
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Force password reset for a user.
    Generates a secure reset token expiring in 24 hours and sets must_reset_password = True.
    """
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado.")

    token = secrets.token_urlsafe(32)
    user.must_reset_password = True
    user.reset_token = token
    user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    
    await db.commit()
    
    logger.info("Admin %s FORCED password reset for user %s (id=%s)", admin.email, user.email, user.user_id)
    from app.config import get_settings
    settings = get_settings()
    return {
        "ok": True,
        "reset_token": token,
        "reset_url": f"{settings.frontend_public_url}/reset-password?token={token}"
    }


@router.post("/{user_id}/password-reset-link")
async def generate_password_reset_link(
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """
    Generate a secure password reset link for a user.
    Only accessible to administrators.
    Invalidates any active tokens for this user.
    """
    import hashlib
    
    # 1. Fetch user and verify active
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Usuario {user_id} no encontrado.")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se puede generar un enlace para un usuario inactivo.")
        
    # 2. Invalidate previous active tokens
    stmt_tokens = select(PasswordResetToken).where(
        PasswordResetToken.user_id == user_id,
        PasswordResetToken.used_at == None,
        PasswordResetToken.revoked_at == None,
        PasswordResetToken.expires_at > datetime.now(timezone.utc)
    )
    active_tokens = (await db.execute(stmt_tokens)).scalars().all()
    for t in active_tokens:
        t.revoked_at = datetime.now(timezone.utc)
        db.add(t)
        
    # 3. Generate token
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    
    # 4. Save new token
    new_token_record = PasswordResetToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        created_by_admin_id=admin.user_id
    )
    db.add(new_token_record)
    
    # 5. Set user must_reset_password = True
    user.must_reset_password = True
    db.add(user)
    
    # 6. Log audit
    audit = UserAudit(
        admin_user_id=admin.user_id,
        target_user_id=user_id,
        action="password_reset_link_created",
        changes_json={
            "description": "Enlace de restablecimiento de contraseña generado.",
            "expires_at": expires_at.isoformat()
        }
    )
    db.add(audit)
    
    await db.commit()
    
    # 7. Get settings and return URL
    from app.config import get_settings
    settings = get_settings()
    reset_url = f"{settings.frontend_public_url}/reset-password?token={token}"
    
    logger.info("Admin %s generated password reset link for user %s", admin.email, user.email)
    
    return {
        "ok": True,
        "user_id": user_id,
        "expires_at": expires_at.isoformat(),
        "reset_url": reset_url
    }


@router.post(
    "/{user_id}/password-setup-link",
    response_model=PasswordSetupLinkResponse,
    responses={
        200: {"description": "Enlace de restablecimiento generado con éxito"},
        401: {"description": "No autenticado"},
        403: {"description": "No autorizado (requiere rol de administrador)"},
        404: {"description": "Usuario no encontrado"},
        409: {"description": "Conflicto o usuario inactivo"},
        422: {"description": "Error de validación"}
    }
)
async def generate_password_setup_link(
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """
    Generate a secure password setup/reset link for a user.
    Only accessible to administrators.
    Invalidates any active reset tokens for this user.
    """
    import hashlib
    
    # 1. Fetch user and verify active
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Usuario {user_id} no encontrado."
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede generar un enlace para un usuario inactivo."
        )
        
    # 2. Invalidate previous active tokens
    stmt_tokens = select(PasswordResetToken).where(
        PasswordResetToken.user_id == user_id,
        PasswordResetToken.used_at == None,
        PasswordResetToken.revoked_at == None,
        PasswordResetToken.expires_at > datetime.now(timezone.utc)
    )
    active_tokens = (await db.execute(stmt_tokens)).scalars().all()
    for t in active_tokens:
        t.revoked_at = datetime.now(timezone.utc)
        db.add(t)
        
    # 3. Generate secure token
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    
    # 4. Save token to db
    new_token_record = PasswordResetToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        created_by_admin_id=admin.user_id
    )
    db.add(new_token_record)
    
    # 5. Force must_reset_password = True on user
    user.must_reset_password = True
    db.add(user)
    
    # 6. Log audit
    audit = UserAudit(
        admin_user_id=admin.user_id,
        target_user_id=user_id,
        action="password_reset_link_created",
        changes_json={
            "description": "Enlace de configuración de contraseña administrativa generado.",
            "expires_at": expires_at.isoformat()
        }
    )
    db.add(audit)
    
    await db.commit()
    
    # 7. Get settings and build URL
    from app.config import get_settings
    settings = get_settings()
    url = f"{settings.frontend_public_url.rstrip('/')}/reset-password?token={token}"
    
    logger.info("Admin %s generated password setup link for user %s (id=%s)", admin.email, user.email, user_id)
    
    return PasswordSetupLinkResponse(
        url=url,
        expires_at=expires_at
    )

