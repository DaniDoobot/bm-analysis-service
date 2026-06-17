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

from app.dependencies import get_db, get_current_user, require_admin
from app.models.users import User
from app.models.prompts import Prompt, PromptBaseStructure, StructurePermission
from app.services.auth_service import log_audit
from app.schemas.users import (
    UserOut,
    UserOutFull,
    UserCreatePayload,
    UserUpdatePayload,
    UserAdminResetPasswordPayload,
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
router = APIRouter(prefix="/bm/users", tags=["User Management"])


@router.get("/sharing/eligible-users")
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


def _user_to_full(u: User) -> dict:
    return {
        "user_id": u.user_id,
        "username": u.username,
        "email": u.email,
        "role": u.role,
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
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    List all users.
    Accessible to any authenticated user (for agent selectors, comparatives, etc.).
    Returns full detail for admins, basic info for others.
    """
    stmt = select(User).order_by(User.user_id.asc())
    res = await db.execute(stmt)
    users = res.scalars().all()

    if current_user.role in {"administrador", "admin"}:
        return {"ok": True, "total": len(users), "users": [_user_to_full(u) for u in users]}

    # Non-admin: return basic public info only
    return {
        "ok": True,
        "total": len(users),
        "users": [
            {
                "user_id": u.user_id,
                "username": u.username,
                "email": u.email,
                "role": u.role,
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
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get a single user by ID. Admins see full detail; others can only see their own."""
    if current_user.role not in {"administrador", "admin"} and current_user.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo puedes ver tu propio perfil.",
        )
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado.")
    return {"ok": True, "user": _user_to_full(user)}


# ── POST /bm/users ─────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreatePayload,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Create a new user in bm_users.
    Requires role='administrador'.
    This is the endpoint Lovable must call when creating a user in Administración → Usuarios.
    """
    username = (body.username or body.email.split("@")[0]).strip()

    # Check email uniqueness
    stmt_email = select(User).where(User.email == body.email)
    if (await db.execute(stmt_email)).scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un usuario con email '{body.email}'.",
        )

    # Check username uniqueness
    stmt_uname = select(User).where(User.username == username)
    if (await db.execute(stmt_uname)).scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un usuario con username '{username}'.",
        )

    token = None
    if body.must_reset_password:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        # Generate a secure placeholder hash since password_hash cannot be NULL
        temp_pass = secrets.token_urlsafe(32)
        pass_hash = hash_password(temp_pass)
    else:
        pass_hash = hash_password(body.password)
        expires_at = None

    new_user = User(
        username=username,
        email=body.email,
        role=body.role,
        is_active=body.is_active,
        hubspot_owner_id=body.hubspot_owner_id,
        agent_initials=body.agent_initials,
        password_hash=pass_hash,
        password_plain_dev=None,
        must_reset_password=body.must_reset_password,
        reset_token=token,
        reset_token_expires_at=expires_at,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    logger.info("Admin %s CREATED user %s (id=%s)", admin.email, new_user.email, new_user.user_id)
    
    resp = {"ok": True, "action": "created", "user": _user_to_full(new_user)}
    if body.must_reset_password:
        resp["reset_token"] = token
        resp["reset_url"] = f"https://speechbm.doobot.ai/reset-password?token={token}"
    return resp


# ── PATCH /bm/users/{user_id} ──────────────────────────────────────────────────

@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    body: UserUpdatePayload,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    transfer_owner_id: Annotated[int | None, Query(description="ID of the new owner to transfer structures to if deactivating")] = None,
):
    """
    Update email, username, role, is_active, hubspot_owner_id, or agent_initials.
    Requires role='administrador'.
    Used by Lovable for role changes, deactivation, and profile edits.
    """
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado.")

    if body.email is not None and body.email != user.email:
        dup = (await db.execute(select(User).where(User.email == body.email))).scalars().first()
        if dup:
            raise HTTPException(status_code=409, detail=f"Email '{body.email}' ya está en uso.")
        user.email = body.email

    if body.username is not None and body.username != user.username:
        dup = (await db.execute(select(User).where(User.username == body.username))).scalars().first()
        if dup:
            raise HTTPException(status_code=409, detail=f"Username '{body.username}' ya está en uso.")
        user.username = body.username

    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        if body.is_active is False and user.is_active is True:
            await handle_user_ownership_transfer(db, user_id, transfer_owner_id, admin.user_id)
        user.is_active = body.is_active
    if body.hubspot_owner_id is not None:
        user.hubspot_owner_id = body.hubspot_owner_id
    if body.agent_initials is not None:
        user.agent_initials = body.agent_initials
    if body.must_reset_password is not None:
        if body.must_reset_password and not user.must_reset_password:
            user.reset_token = secrets.token_urlsafe(32)
            user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        user.must_reset_password = body.must_reset_password

    await db.commit()
    await db.refresh(user)

    logger.info("Admin %s UPDATED user %s (id=%s)", admin.email, user.email, user.user_id)
    return {"ok": True, "action": "updated", "user": _user_to_full(user)}


# ── POST /bm/users/{user_id}/reset-password ────────────────────────────────────

@router.post("/{user_id}/reset-password")
async def admin_reset_password(
    user_id: int,
    body: UserAdminResetPasswordPayload,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Reset another user's password (no current_password required).
    Requires role='administrador'.
    Used by Lovable when admin resets a user's password.
    """
    stmt = select(User).where(User.user_id == user_id)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado.")

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
    db: Annotated[AsyncSession, Depends(get_db)],
    transfer_owner_id: Annotated[int | None, Query(description="ID of the new owner to transfer structures to")] = None,
):
    """
    Soft-delete: set is_active=False. Does NOT physically delete the user.
    Requires role='administrador'.
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

    # Check for owned structures and handle transfer
    if user.is_active:
        await handle_user_ownership_transfer(db, user_id, transfer_owner_id, admin.user_id)

    user.is_active = False
    await db.commit()

    logger.info("Admin %s DEACTIVATED user %s (id=%s)", admin.email, user.email, user.user_id)
    return {"ok": True, "action": "deactivated", "user_id": user_id, "email": user.email}


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
    return {
        "ok": True,
        "reset_token": token,
        "reset_url": f"https://speechbm.doobot.ai/reset-password?token={token}"
    }
