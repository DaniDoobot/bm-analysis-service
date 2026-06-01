"""
FastAPI router for user management — CRUD protected by Bearer admin token.
All write endpoints require role='administrador'.
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, require_admin
from app.models.users import User
from app.schemas.users import (
    UserOut,
    UserOutFull,
    UserCreatePayload,
    UserUpdatePayload,
    UserAdminResetPasswordPayload,
)
from app.utils.security import hash_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/users", tags=["User Management"])


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

    if current_user.role == "administrador":
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
    if current_user.role != "administrador" and current_user.user_id != user_id:
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

    new_user = User(
        username=username,
        email=body.email,
        role=body.role,
        is_active=body.is_active,
        hubspot_owner_id=body.hubspot_owner_id,
        agent_initials=body.agent_initials,
        password_hash=hash_password(body.password),
        password_plain_dev=body.password,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    logger.info("Admin %s CREATED user %s (id=%s)", admin.email, new_user.email, new_user.user_id)
    return {"ok": True, "action": "created", "user": _user_to_full(new_user)}


# ── PATCH /bm/users/{user_id} ──────────────────────────────────────────────────

@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    body: UserUpdatePayload,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
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
        user.is_active = body.is_active
    if body.hubspot_owner_id is not None:
        user.hubspot_owner_id = body.hubspot_owner_id
    if body.agent_initials is not None:
        user.agent_initials = body.agent_initials

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
    user.password_plain_dev = body.new_password
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

    user.is_active = False
    await db.commit()

    logger.info("Admin %s DEACTIVATED user %s (id=%s)", admin.email, user.email, user.user_id)
    return {"ok": True, "action": "deactivated", "user_id": user_id, "email": user.email}
