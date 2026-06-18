import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user
from app.models.users import User, PasswordResetToken, UserAudit
from app.schemas.users import (
    UserOut,
    LoginPayload,
    BootstrapPayload,
    RevealPasswordPayload,
    MeUpdatePayload,
    MePasswordUpdatePayload,
    RequestPasswordResetPayload,
    ResetPasswordPayload,
    PasswordResetConfirmPayload,
)
from app.utils.security import (
    verify_password,
    hash_password,
    create_access_token,
)
from app.config import get_settings
from app.schemas.personalized_training import TrainingAgentReportOut
from app.services.personalized_training_service import PersonalizedTrainingService


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
    
    if not user:
        logger.warning("Invalid credentials for identifier: '%s'", identifier)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nombre de usuario o contraseña incorrectos."
        )
        
    if not user.is_active:
        logger.warning("Inactive user login attempt: '%s'", identifier)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La cuenta de usuario está desactivada."
        )

    if user.must_reset_password:
        logger.info("Login blocked: user %s must reset password.", user.email)
        return {
            "ok": False,
            "requires_password_reset": True,
            "email": user.email,
            "message": "Debes establecer una nueva contraseña para continuar."
        }
        
    if not verify_password(payload.password, user.password_hash):
        logger.warning("Invalid credentials for identifier: '%s'", identifier)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nombre de usuario o contraseña incorrectos."
        )
        
    # Update last_login_at
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    # Generate Bearer Token
    token_data = {"user_id": user.user_id, "username": user.username, "email": user.email}
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
        password_plain_dev=None,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)

    token_data = {"user_id": admin.user_id, "username": admin.username, "email": admin.email}
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


@router.post("/auth/request-password-reset")
async def request_password_reset(
    payload: RequestPasswordResetPayload,
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """
    Request a password reset token.
    Safe neutral response, but returns the token/url in the JSON in development/testing mode
    if the email exists and is active.
    """
    email_clean = payload.email.strip().lower()
    
    stmt = select(User).where(User.email == email_clean)
    res = await db.execute(stmt)
    user = res.scalars().first()
    
    msg = "Si el email existe y está activo, se ha generado un enlace de restablecimiento."
    
    if not user or not user.is_active:
        logger.info("Request reset for non-existent or inactive email: %s", email_clean)
        return {
            "ok": True,
            "message": msg
        }
        
    token = secrets.token_urlsafe(32)
    user.reset_token = token
    user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    
    await db.commit()
    
    logger.info("Generated reset token for user: %s", user.email)
    
    settings = get_settings()
    # We return the token and url in the JSON so that Lovable/developers can access it manually.
    return {
        "ok": True,
        "message": msg,
        "reset_token": token,
        "reset_url": f"{settings.frontend_public_url}/reset-password?token={token}"
    }


@router.post("/auth/reset-password")
async def reset_password(
    payload: ResetPasswordPayload,
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """
    Submit a password reset token to set a new password.
    Supports both legacy User.reset_token and the new hashed PasswordResetToken.
    Clears must_reset_password flag and updates password_set_at.
    """
    import hashlib
    
    token = payload.token.strip()
    
    # 1. Try legacy User.reset_token first
    stmt_legacy = select(User).where(User.reset_token == token)
    res_legacy = await db.execute(stmt_legacy)
    user = res_legacy.scalars().first()
    
    if user:
        if not user.reset_token_expires_at or user.reset_token_expires_at < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El token de restablecimiento ha expirado."
            )
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El usuario asociado a este token está inactivo."
            )
            
        user.password_hash = hash_password(payload.new_password)
        user.password_plain_dev = None
        user.must_reset_password = False
        user.password_set_at = datetime.now(timezone.utc)
        user.reset_token = None
        user.reset_token_expires_at = None
        
        # Also revoke any tokens in PasswordResetToken for this user to be clean
        stmt_other = select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.user_id,
            PasswordResetToken.used_at == None,
            PasswordResetToken.revoked_at == None
        )
        res_other = await db.execute(stmt_other)
        other_tokens = res_other.scalars().all()
        for ot in other_tokens:
            ot.revoked_at = datetime.now(timezone.utc)
            db.add(ot)
            
        db.add(user)
        await db.commit()
        logger.info("Successfully reset password for user %s via legacy token.", user.email)
        return {
            "ok": True,
            "message": "Contraseña restablecida correctamente."
        }
        
    # 2. Try hashed PasswordResetToken table
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    stmt_new = select(PasswordResetToken, User).join(
        User, PasswordResetToken.user_id == User.user_id
    ).where(PasswordResetToken.token_hash == token_hash)
    
    res_new = await db.execute(stmt_new)
    row = res_new.first()
    
    if not row:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token de restablecimiento inválido o expirado."
        )
        
    token_record, user = row
    
    if token_record.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de restablecimiento ya ha sido utilizado."
        )
        
    if token_record.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de restablecimiento ha sido revocado."
        )
        
    if token_record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de restablecimiento ha expirado."
        )
        
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El usuario asociado a este token está inactivo."
        )
        
    # Transactional update
    now = datetime.now(timezone.utc)
    
    user.password_hash = hash_password(payload.new_password)
    user.password_plain_dev = None
    user.must_reset_password = False
    user.password_set_at = now
    db.add(user)
    
    token_record.used_at = now
    db.add(token_record)
    
    # Revoke all other active reset tokens for this user
    stmt_other = select(PasswordResetToken).where(
        PasswordResetToken.user_id == user.user_id,
        PasswordResetToken.id != token_record.id,
        PasswordResetToken.used_at == None,
        PasswordResetToken.revoked_at == None
    )
    res_other = await db.execute(stmt_other)
    other_tokens = res_other.scalars().all()
    for ot in other_tokens:
        ot.revoked_at = now
        db.add(ot)
        
    # Log Audit
    admin_id = token_record.created_by_admin_id or user.user_id
    audit = UserAudit(
        admin_user_id=admin_id,
        target_user_id=user.user_id,
        action="password_reset_completed",
        changes_json={
            "description": "Restablecimiento de contraseña completado mediante enlace administrativo.",
            "token_created_by_admin_id": token_record.created_by_admin_id,
            "token_expires_at": token_record.expires_at.isoformat(),
            "result": "success"
        }
    )
    db.add(audit)
    
    await db.commit()
    logger.info("Successfully reset password for user %s via administrative setup token.", user.email)
    return {
        "ok": True,
        "message": "Contraseña restablecida correctamente."
    }


@router.get("/auth/password-reset/validate")
async def validate_password_reset_token(
    token: str = Query(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Validate a password reset token.
    Checks if token exists (via SHA256 hash), is not used, is not revoked,
    has not expired, and the corresponding user is active.
    Returns basic metadata.
    """
    import hashlib
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    
    stmt = select(PasswordResetToken, User).join(
        User, PasswordResetToken.user_id == User.user_id
    ).where(PasswordResetToken.token_hash == token_hash)
    
    res = await db.execute(stmt)
    row = res.first()
    
    if not row:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token de restablecimiento inválido o inexistente."
        )
        
    token_record, user = row
    
    if token_record.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de restablecimiento ya ha sido utilizado."
        )
        
    if token_record.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de restablecimiento ha sido revocado."
        )
        
    if token_record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de restablecimiento ha expirado."
        )
        
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El usuario asociado a este token está inactivo."
        )
        
    return {
        "valid": True,
        "expires_at": token_record.expires_at.isoformat(),
        "user_display": user.name or user.username
    }


@router.post("/auth/password-reset/confirm")
async def confirm_password_reset(
    payload: PasswordResetConfirmPayload,
    db: AsyncSession = Depends(get_db)
):
    """
    Confirm password reset and set a new password.
    Updates the password hash, marks the token as used, clears must_reset_password,
    and invalidates any other active tokens for the user, all in a single transaction.
    """
    import hashlib
    
    if payload.new_password != payload.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La nueva contraseña y su confirmación no coinciden."
        )
        
    if len(payload.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La nueva contraseña debe tener al menos 8 caracteres."
        )
        
    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    
    # We query the token and user in a transaction
    stmt = select(PasswordResetToken, User).join(
        User, PasswordResetToken.user_id == User.user_id
    ).where(PasswordResetToken.token_hash == token_hash)
    
    res = await db.execute(stmt)
    row = res.first()
    
    if not row:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token de restablecimiento inválido o inexistente."
        )
        
    token_record, user = row
    
    if token_record.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de restablecimiento ya ha sido utilizado."
        )
        
    if token_record.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de restablecimiento ha sido revocado."
        )
        
    if token_record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de restablecimiento ha expirado."
        )
        
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El usuario asociado a este token está inactivo."
        )
        
    # Transactional update
    now = datetime.now(timezone.utc)
    
    user.password_hash = hash_password(payload.new_password)
    user.password_plain_dev = None
    user.must_reset_password = False
    user.password_set_at = now
    db.add(user)
    
    token_record.used_at = now
    db.add(token_record)
    
    # Revoke all other active reset tokens for this user
    stmt_other = select(PasswordResetToken).where(
        PasswordResetToken.user_id == user.user_id,
        PasswordResetToken.id != token_record.id,
        PasswordResetToken.used_at == None,
        PasswordResetToken.revoked_at == None
    )
    res_other = await db.execute(stmt_other)
    other_tokens = res_other.scalars().all()
    for ot in other_tokens:
        ot.revoked_at = now
        db.add(ot)
        
    # Log Audit
    admin_id = token_record.created_by_admin_id or user.user_id
    audit = UserAudit(
        admin_user_id=admin_id,
        target_user_id=user.user_id,
        action="password_reset_completed",
        changes_json={
            "description": "Restablecimiento de contraseña completado mediante enlace.",
            "token_created_by_admin_id": token_record.created_by_admin_id,
            "token_expires_at": token_record.expires_at.isoformat(),
            "result": "success"
        }
    )
    db.add(audit)
    
    await db.commit()
    
    logger.info("User %s successfully reset password via secure token.", user.email)
    return {
        "ok": True,
        "message": "Contraseña restablecida correctamente."
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
        
    # 3. Update hashes
    current_user.password_hash = hash_password(payload.new_password)
    current_user.password_plain_dev = None
    
    await db.commit()
    return {"ok": True, "detail": "Contraseña actualizada exitosamente."}


@router.patch("/me", response_model=UserOut)
async def update_my_profile(
    payload: MeUpdatePayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Update username and/or email, requiring password confirmation."""
    # Check if they try to change their hubspot_owner_id
    if current_user.role in ["agent", "agente"]:
        clean_payload_hs = payload.hubspot_owner_id
        if clean_payload_hs is not None:
            clean_payload_hs = str(clean_payload_hs).strip()
            if clean_payload_hs == "":
                clean_payload_hs = None
        if clean_payload_hs != current_user.hubspot_owner_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No está permitido modificar tu propio HubSpot Owner ID."
            )

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


@router.get("/me/analysis-results", response_model=TrainingAgentReportOut)
async def get_my_analysis_results(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    training_report_id: Optional[int] = Query(None, description="ID of a specific historical report. If not provided, returns the current active report.")
):
    """Retrieve training report details for the authenticated agent."""
    is_admin = current_user.role in ["admin", "administrador"]
        
    if training_report_id is not None:
        report_details = await PersonalizedTrainingService.get_report_by_id(db, report_id=training_report_id)
        if not report_details:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Informe de entrenamiento ID {training_report_id} no encontrado."
            )
        # Ownership check
        if not is_admin and report_details["hubspot_owner_id"] != current_user.hubspot_owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para ver el informe de entrenamiento de otro agente."
            )
        report_data = report_details
    else:
        if not current_user.hubspot_owner_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
            )
        detail = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=current_user.hubspot_owner_id)
        if not detail or not detail.get("current_report"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No se encontró ningún informe de entrenamiento actual disponible para tu usuario."
            )
        report_data = detail["current_report"]
        
    # Sanitize prompt instructions for non-admin agents
    if current_user.role not in ["admin", "administrador"]:
        from app.routers.personalized_training import sanitize_report_for_agent
        report_data = sanitize_report_for_agent(report_data)
        
    return report_data
