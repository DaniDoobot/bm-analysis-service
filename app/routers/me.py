"""FastAPI router for User profile, auth, and development password reveal."""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user
from app.models.users import User
from app.schemas.users import (
    UserOut,
    LoginPayload,
    RevealPasswordPayload,
    MeUpdatePayload,
    MePasswordUpdatePayload,
)
from app.utils.security import (
    verify_password,
    hash_password,
    create_access_token,
)
from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["User Profile & Auth"])


@router.post("/auth/login")
async def login(
    payload: LoginPayload,
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Authenticate user and return a Bearer access token."""
    logger.info("Login attempt for username: '%s'", payload.username)
    
    # Search by username or email
    stmt = select(User).where(
        (User.username == payload.username) | (User.email == payload.username)
    )
    res = await db.execute(stmt)
    user = res.scalars().first()
    
    if not user or not verify_password(payload.password, user.password_hash):
        logger.warning("Invalid credentials for username: '%s'", payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nombre de usuario o contraseña incorrectos."
        )
        
    if not user.is_active:
        logger.warning("Inactive user login attempt: '%s'", payload.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La cuenta de usuario está desactivada."
        )
        
    # Generate Bearer Token
    token_data = {"user_id": user.user_id, "username": user.username}
    token = create_access_token(token_data)
    
    return {
        "ok": True,
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "role": user.role
        }
    }


@router.get("/me", response_model=UserOut)
async def get_my_profile(
    current_user: Annotated[User, Depends(get_current_user)]
):
    """Retrieve profile details of the authenticated user."""
    return current_user


@router.post("/me/reveal-password")
async def reveal_my_password(
    payload: RevealPasswordPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Reveal the authenticated user's own password, if allowed in development."""
    settings = get_settings()
    
    # 1. Enforce safety flag
    if not settings.allow_password_reveal:
        logger.warning("Blocked reveal-password request: functionality is disabled.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La funcionalidad de visualización de contraseña está deshabilitada en este entorno."
        )
        
    # 2. Verify current password
    if not verify_password(payload.current_password, current_user.password_hash):
        logger.warning("Failed reveal-password verification for user: '%s'", current_user.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Contraseña actual incorrecta."
        )
        
    # 3. Retrieve and return plain dev password
    plain_password = current_user.password_plain_dev
    if not plain_password:
        return {
            "ok": True,
            "password": None,
            "message": "No hay contraseña en claro registrada. Restablezca su contraseña para poblarla."
        }
        
    return {
        "ok": True,
        "password": plain_password
    }


@router.patch("/me/password")
async def change_my_password(
    payload: MePasswordUpdatePayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Update password for the authenticated user, synchronizing dev plain field."""
    # 1. Verify confirmation matches
    if payload.new_password != payload.new_password_confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La nueva contraseña y su confirmación no coinciden."
        )
        
    # 2. Verify current password
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Contraseña actual incorrecta."
        )
        
    # 3. Update hashes and plain dev password
    current_user.password_hash = hash_password(payload.new_password)
    current_user.password_plain_dev = payload.new_password
    
    await db.commit()
    return {"ok": True, "detail": "Contraseña actualizada exitosamente."}


@router.patch("/me", response_model=UserOut)
async def update_my_profile(
    payload: MeUpdatePayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Update username and/or email, requiring password confirmation."""
    # 1. Verify current password
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Contraseña actual incorrecta para realizar cambios de perfil."
        )
        
    # 2. Update editable fields
    if payload.username is not None:
        # Check uniqueness if changed
        if payload.username != current_user.username:
            u_stmt = select(User).where(User.username == payload.username)
            u_res = await db.execute(u_stmt)
            if u_res.scalars().first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"El nombre de usuario '{payload.username}' ya está en uso."
                )
        current_user.username = payload.username
        
    if payload.email is not None:
        # Check uniqueness if changed
        if payload.email != current_user.email:
            e_stmt = select(User).where(User.email == payload.email)
            e_res = await db.execute(e_stmt)
            if e_res.scalars().first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"El email '{payload.email}' ya está registrado."
                )
        current_user.email = payload.email

    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.get("/users", response_model=list[UserOut])
async def list_users(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Retrieve all users. Enforces strict schema to exclude password hashes and dev plain fields."""
    # Only allow authenticated users to retrieve catalog
    stmt = select(User).order_by(User.user_id.asc())
    res = await db.execute(stmt)
    return res.scalars().all()
