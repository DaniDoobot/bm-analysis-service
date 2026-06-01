"""
Admin router — administrative operations including environment cleanup and user management.
"""
import logging
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.users import User
from app.utils.security import hash_password, verify_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/admin", tags=["Admin"])

# ── Admin Secret Guard ─────────────────────────────────────────────────────────
_ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "bm-admin-secret-2026")

def require_admin_secret(x_admin_secret: str = Header(..., alias="X-Admin-Secret")):
    """Validate the X-Admin-Secret header for sensitive admin-only operations."""
    if x_admin_secret != _ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acceso denegado: secreto admin inválido.")


class CleanupRequest(BaseModel):
    keep_prompt_ids: list[int] = Field(default=[1], description="Prompt IDs to keep untouched")
    keep_base_structure_ids: list[int] = Field(default=[6], description="Base structure IDs to keep untouched")
    mode: Literal["dry_run", "execute"] = Field(default="dry_run", description="dry_run to preview, execute to apply")
    delete_physical_if_safe: bool = Field(default=False, description="Allow physical deletes if no dependencies exist")
    performed_by_email: str | None = Field(default=None, description="Email of user performing the cleanup")


@router.post("/cleanup-structures")
async def cleanup_structures(
    body: CleanupRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Administrative cleanup of stale prompts and base structures.

    - mode=dry_run: Returns what WOULD be archived/deleted without modifying any data.
    - mode=execute: Performs soft-delete/archive on all structures not in keep lists.

    Protections:
    - prompt_ids in keep_prompt_ids are never touched.
    - base_structure_ids in keep_base_structure_ids are never touched.
    - Structures referenced in mass evaluation jobs/results are archived, never physically deleted.
    - Historical results and jobs remain intact.
    """
    # Safety guard: always protect at minimum the defaults
    safe_prompt_ids = list(set(body.keep_prompt_ids))
    safe_base_ids = list(set(body.keep_base_structure_ids))

    if not safe_prompt_ids:
        raise HTTPException(status_code=400, detail="keep_prompt_ids cannot be empty.")
    if not safe_base_ids:
        raise HTTPException(status_code=400, detail="keep_base_structure_ids cannot be empty.")

    logger.info(
        "Admin cleanup-structures called: mode=%s keep_prompts=%s keep_bases=%s",
        body.mode, safe_prompt_ids, safe_base_ids,
    )

    try:
        from app.services.cleanup_service import run_cleanup
        result = await run_cleanup(
            db=db,
            keep_prompt_ids=safe_prompt_ids,
            keep_base_structure_ids=safe_base_ids,
            mode=body.mode,
            delete_physical_if_safe=body.delete_physical_if_safe,
            performed_by_email=body.performed_by_email,
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("Error during cleanup-structures: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"Error durante la limpieza: {str(e)}",
        )


class CleanupVersionsRequest(BaseModel):
    keep_prompt_ids: list[int] = Field(default=[1], description="Prompt IDs whose versions will be cleaned")
    keep_current_versions_only: bool = Field(default=True, description="Archive all non-current versions")
    mode: Literal["dry_run", "execute"] = Field(default="dry_run", description="dry_run to preview, execute to apply")
    delete_physical_if_safe: bool = Field(default=False, description="Allow physical deletes of unreferenced versions")
    performed_by_email: str | None = Field(default=None, description="Email of user performing the cleanup")


@router.post("/cleanup-prompt-versions")
async def cleanup_prompt_versions(
    body: CleanupVersionsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Archive (hide) all non-current versions of the specified prompts.

    - mode=dry_run: Returns what WOULD be archived without modifying any data.
    - mode=execute: Archives all non-current versions. Versions referenced in
      mass evaluation results are archived (not deleted) to preserve traceability.

    The current version (is_current=True) is always kept untouched.
    """
    if not body.keep_prompt_ids:
        raise HTTPException(status_code=400, detail="keep_prompt_ids cannot be empty.")

    logger.info(
        "Admin cleanup-prompt-versions called: mode=%s keep_prompts=%s",
        body.mode, body.keep_prompt_ids,
    )

    try:
        from app.services.cleanup_service import cleanup_prompt_versions as _cleanup_versions
        result = await _cleanup_versions(
            db=db,
            keep_prompt_ids=body.keep_prompt_ids,
            keep_current_versions_only=body.keep_current_versions_only,
            mode=body.mode,
            delete_physical_if_safe=body.delete_physical_if_safe,
            performed_by_email=body.performed_by_email,
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("Error during cleanup-prompt-versions: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"Error durante la limpieza de versiones: {str(e)}",
        )


class CleanupMassEvaluationsRequest(BaseModel):
    mode: Literal["dry_run", "execute"] = Field(default="dry_run", description="dry_run to preview, execute to delete all")
    performed_by_email: str | None = Field(default=None, description="Email of user performing the cleanup")


@router.post("/cleanup-mass-evaluations")
async def cleanup_mass_evaluations(
    body: CleanupMassEvaluationsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Delete ALL mass evaluation data (jobs, runs, results).

    - mode=dry_run: Returns counts and details without modifying any data.
    - mode=execute: Deletes in FK-safe order: results → runs → jobs.

    This operation is IRREVERSIBLE in execute mode.
    Does NOT touch prompts, criteria, services, typologies or manual analyses.
    """
    logger.info(
        "Admin cleanup-mass-evaluations called: mode=%s performed_by=%s",
        body.mode, body.performed_by_email,
    )
    try:
        from app.services.cleanup_service import cleanup_mass_evaluations as _cleanup_mass
        result = await _cleanup_mass(
            db=db,
            mode=body.mode,
            performed_by_email=body.performed_by_email,
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("Error durante la limpieza de evaluaciones masivas: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"Error durante la limpieza de evaluaciones masivas: {str(e)}",
        )


# ── User Management Endpoints ─────────────────────────────────────────────────

class UserUpsertPayload(BaseModel):
    email: str = Field(description="Email del usuario")
    username: str | None = Field(default=None, description="Username (si no se da, se usa la parte antes del @)")
    password: str = Field(description="Contraseña en claro")
    role: str = Field(default="agente", description="Rol: administrador, agente, etc.")
    is_active: bool = Field(default=True)
    hubspot_owner_id: str | None = Field(default=None)
    agent_initials: str | None = Field(default=None)


class UserResetPasswordPayload(BaseModel):
    email: str = Field(description="Email o username del usuario a actualizar")
    new_password: str = Field(description="Nueva contraseña en claro")


@router.get("/users", dependencies=[Depends(require_admin_secret)])
async def admin_list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    List all users in bm_users with their status.
    Protected by X-Admin-Secret header.
    """
    stmt = select(User).order_by(User.user_id.asc())
    res = await db.execute(stmt)
    users = res.scalars().all()
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
                "has_password_hash": bool(u.password_hash),
                "password_plain_dev": u.password_plain_dev,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }


@router.post("/users/upsert", dependencies=[Depends(require_admin_secret)])
async def admin_upsert_user(
    body: UserUpsertPayload,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Create or update a user in bm_users.
    - If a user with that email already exists, updates their password, role, and status.
    - If no user exists with that email, creates one.
    Protected by X-Admin-Secret header.
    """
    username = body.username or body.email.split("@")[0]

    stmt = select(User).where(User.email == body.email)
    res = await db.execute(stmt)
    existing = res.scalars().first()

    if existing:
        existing.password_hash = hash_password(body.password)
        existing.password_plain_dev = body.password
        existing.role = body.role
        existing.is_active = body.is_active
        if body.hubspot_owner_id is not None:
            existing.hubspot_owner_id = body.hubspot_owner_id
        if body.agent_initials is not None:
            existing.agent_initials = body.agent_initials
        await db.commit()
        await db.refresh(existing)
        logger.info("Admin upsert: UPDATED user %s (id=%s)", body.email, existing.user_id)
        return {
            "ok": True,
            "action": "updated",
            "user_id": existing.user_id,
            "email": existing.email,
            "username": existing.username,
            "role": existing.role,
            "is_active": existing.is_active,
        }
    else:
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
        logger.info("Admin upsert: CREATED user %s (id=%s)", body.email, new_user.user_id)
        return {
            "ok": True,
            "action": "created",
            "user_id": new_user.user_id,
            "email": new_user.email,
            "username": new_user.username,
            "role": new_user.role,
            "is_active": new_user.is_active,
        }


@router.post("/users/reset-password", dependencies=[Depends(require_admin_secret)])
async def admin_reset_password(
    body: UserResetPasswordPayload,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Reset password for an existing user (search by email or username).
    Protected by X-Admin-Secret header.
    """
    stmt = select(User).where(
        (User.email == body.email) | (User.username == body.email)
    )
    res = await db.execute(stmt)
    user = res.scalars().first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"No existe usuario con email/username '{body.email}'."
        )

    user.password_hash = hash_password(body.new_password)
    user.password_plain_dev = body.new_password
    user.is_active = True
    await db.commit()
    logger.info("Admin reset-password: updated password for user %s (id=%s)", user.email, user.user_id)
    return {
        "ok": True,
        "user_id": user.user_id,
        "email": user.email,
        "username": user.username,
        "is_active": user.is_active,
        "message": "Contraseña actualizada correctamente.",
    }
