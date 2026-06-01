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
    BootstrapPayload,
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
    """Authenticate user and return a Bearer access token.
    
    Accepts payload with 'username' OR 'email' field (both supported).
    Searches bm_users by username OR email column.
    """
    identifier = payload.login_identifier
    logger.info("Login attempt for identifier: '%s'", identifier)
    
    # Search by username or email (whichever the client sent)
    stmt = select(User).where(
        (User.username == identifier) | (User.email == identifier)
    )
    res = await db.execute(stmt)
    user = res.scalars().first()
    
    if not user or not verify_password(payload.password, user.password_hash):
        logger.warning("Invalid credentials for identifier: '%s'", identifier)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nombre de usuario o contrase\u00f1a incorrectos."
        )
        
    if not user.is_active:
        logger.warning("Inactive user login attempt: '%s'", identifier)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La cuenta de usuario est\u00e1 desactivada."
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


@router.post("/auth/bootstrap", status_code=status.HTTP_201_CREATED)
async def bootstrap_first_admin(
    payload: BootstrapPayload,
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """
    Bootstrap endpoint: create the very first administrator.
    Only works when bm_users is completely empty.
    Self-disables as soon as any user exists.
    """
    from sqlalchemy import func as sa_func
    count_res = await db.execute(select(sa_func.count()).select_from(User))
    count = count_res.scalar()
    if count > 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bootstrap deshabilitado: ya existen usuarios en el sistema."
        )

    username = (payload.username or payload.email.split("@")[0]).strip()
    admin = User(
        username=username,
        email=payload.email,
        role="administrador",
        is_active=True,
        agent_initials=payload.agent_initials,
        password_hash=hash_password(payload.password),
        password_plain_dev=payload.password,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)

    token_data = {"user_id": admin.user_id, "username": admin.username}
    token = create_access_token(token_data)

    logger.info("BOOTSTRAP: created first admin %s (id=%s)", admin.email, admin.user_id)
    return {
        "ok": True,
        "message": "Primer administrador creado exitosamente.",
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "user_id": admin.user_id,
            "username": admin.username,
            "email": admin.email,
            "role": admin.role,
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
