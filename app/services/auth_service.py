"""Centralized authorization service for structures permissions."""
import logging
from typing import Any
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_db, get_current_user
from app.models.users import User
from app.models.prompts import Prompt, PromptBaseStructure, StructurePermission, StructurePermissionAudit

logger = logging.getLogger(__name__)

LEVEL_NONE = 0
LEVEL_VIEW = 1
LEVEL_USE = 2
LEVEL_EDIT = 3
LEVEL_OWNER = 4
LEVEL_ADMIN = 5

LEVEL_NAMES = {
    LEVEL_NONE: "none",
    LEVEL_VIEW: "view",
    LEVEL_USE: "use",
    LEVEL_EDIT: "edit",
    LEVEL_OWNER: "owner",
    LEVEL_ADMIN: "admin",
}

LEVEL_VALUES = {
    "none": LEVEL_NONE,
    "view": LEVEL_VIEW,
    "use": LEVEL_USE,
    "edit": LEVEL_EDIT,
    "owner": LEVEL_OWNER,
    "admin": LEVEL_ADMIN,
}


async def get_effective_structure_permission(
    db: AsyncSession,
    user: User,
    structure_type: str,
    structure_id: int
) -> dict:
    """
    Dynamically resolve structure permissions for a user based on role, ownership,
    manual sharing, and inheritance (for base structures).
    """
    # 1. Normalize roles. Exclude agents completely.
    user_role = getattr(user, "role", "agent").lower()
    
    if user_role == "agent":
        return {
            "is_admin": False,
            "is_owner": False,
            "manual_permission": "none",
            "inherited_permission": "none",
            "effective_permission": "none",
            "can_view": False,
            "can_use": False,
            "can_edit": False,
            "can_share": False,
            "can_delete": False,
            "can_transfer": False,
            "can_duplicate": False,
            "can_archive": False,
            "can_restore": False,
            "access_source": "none"
        }
    
    settings = get_settings()
    if not settings.enable_structure_permissions:
        # When permissions feature flag is disabled, non-agents have full rights
        is_admin = user_role in ("admin", "administrador")
        return {
            "is_admin": is_admin,
            "is_owner": not is_admin,
            "manual_permission": "none",
            "inherited_permission": "none",
            "effective_permission": "admin" if is_admin else "owner",
            "can_view": True,
            "can_use": True,
            "can_edit": True,
            "can_share": True,
            "can_delete": is_admin,
            "can_transfer": is_admin,
            "can_duplicate": True,
            "can_archive": True,
            "can_restore": True,
            "access_source": "admin" if is_admin else "owner"
        }

    is_admin = user_role in ("admin", "administrador")
    if is_admin:
        return {
            "is_admin": True,
            "is_owner": False,
            "manual_permission": "none",
            "inherited_permission": "none",
            "effective_permission": "admin",
            "can_view": True,
            "can_use": True,
            "can_edit": True,
            "can_share": True,
            "can_delete": True,
            "can_transfer": True,
            "can_duplicate": True,
            "can_archive": True,
            "can_restore": True,
            "access_source": "admin"
        }

    # 2. Check Ownership and existence
    is_owner = False
    if structure_type == "base":
        stmt = select(PromptBaseStructure).where(PromptBaseStructure.id == structure_id)
        res = await db.execute(stmt)
        obj = res.scalars().first()
        if not obj:
            raise HTTPException(status_code=404, detail=f"Base structure {structure_id} not found.")
        owner_id = obj.owner_user_id
        if owner_id == user.user_id:
            is_owner = True
    elif structure_type == "specific":
        stmt = select(Prompt).where(Prompt.prompt_id == structure_id)
        res = await db.execute(stmt)
        obj = res.scalars().first()
        if not obj:
            raise HTTPException(status_code=404, detail=f"Specific structure {structure_id} not found.")
        owner_id = obj.owner_user_id
        if owner_id == user.user_id:
            is_owner = True

    if is_owner:
        return {
            "is_admin": False,
            "is_owner": True,
            "manual_permission": "none",
            "inherited_permission": "none",
            "effective_permission": "owner",
            "can_view": True,
            "can_use": True,
            "can_edit": True,
            "can_share": True,
            "can_delete": False,  # Owner cannot delete
            "can_transfer": False,  # Owner cannot transfer ownership
            "can_duplicate": True,
            "can_archive": True,
            "can_restore": True,
            "access_source": "owner"
        }

    # 3. Check Manual sharing
    manual_perm = "none"
    stmt_manual = select(StructurePermission.permission_level).where(
        and_(
            StructurePermission.structure_type == structure_type,
            StructurePermission.structure_id == structure_id,
            StructurePermission.user_id == user.user_id
        )
    )
    res_manual = await db.execute(stmt_manual)
    db_manual = res_manual.scalar()
    if db_manual:
        manual_perm = db_manual.lower()

    # 4. Check Inherited permission (only applies to base structures)
    inherited_perm = "none"
    if structure_type == "base":
        # Check all specific structures linking to this base_structure_id
        stmt_spec = select(Prompt.prompt_id, Prompt.owner_user_id).where(
            and_(Prompt.base_structure_id == structure_id, Prompt.is_archived == False)
        )
        res_spec = await db.execute(stmt_spec)
        specifics = res_spec.all()
        
        if specifics:
            spec_ids = [s[0] for s in specifics]
            # If user owns any specific structure, they get 'use' inherited on base
            owns_any_specific = any(s[1] == user.user_id for s in specifics)
            if owns_any_specific:
                inherited_perm = "use"
            else:
                # Query manual permissions on all child specific structures
                stmt_spec_perms = select(StructurePermission.permission_level).where(
                    and_(
                        StructurePermission.structure_type == "specific",
                        StructurePermission.structure_id.in_(spec_ids),
                        StructurePermission.user_id == user.user_id
                    )
                )
                res_spec_perms = await db.execute(stmt_spec_perms)
                spec_perm_levels = res_spec_perms.scalars().all()
                
                max_spec_level = LEVEL_NONE
                for pl in spec_perm_levels:
                    lvl_val = LEVEL_VALUES.get(pl.lower(), LEVEL_NONE)
                    if lvl_val > max_spec_level:
                        max_spec_level = lvl_val
                
                if max_spec_level >= LEVEL_USE:  # 'use' or 'edit' on specific
                    inherited_perm = "use"
                elif max_spec_level == LEVEL_VIEW:  # 'view' on specific
                    inherited_perm = "view"

    # 5. Effective level is the max of manual and inherited levels
    manual_lvl = LEVEL_VALUES.get(manual_perm, LEVEL_NONE)
    inherited_lvl = LEVEL_VALUES.get(inherited_perm, LEVEL_NONE)
    effective_lvl = max(manual_lvl, inherited_lvl)
    
    effective_perm = LEVEL_NAMES.get(effective_lvl, "none")
    
    can_view = effective_lvl >= LEVEL_VIEW
    can_use = effective_lvl >= LEVEL_USE
    can_edit = effective_lvl >= LEVEL_EDIT
    can_share = False
    can_delete = False
    can_transfer = False
    can_duplicate = effective_lvl >= LEVEL_EDIT
    
    # access source
    if manual_lvl >= inherited_lvl and manual_lvl > LEVEL_NONE:
        access_source = "shared"
    elif inherited_lvl > LEVEL_NONE:
        access_source = "inherited"
    else:
        access_source = "none"

    return {
        "is_admin": False,
        "is_owner": False,
        "manual_permission": manual_perm,
        "inherited_permission": inherited_perm,
        "effective_permission": effective_perm,
        "can_view": can_view,
        "can_use": can_use,
        "can_edit": can_edit,
        "can_share": can_share,
        "can_delete": can_delete,
        "can_transfer": can_transfer,
        "can_duplicate": can_duplicate,
        "can_archive": False,
        "can_restore": False,
        "access_source": access_source
    }


def check_structure_permission(structure_type: str, required_level: str):
    """Factory creating a FastAPI dependency to validate permission on a path parameter structure ID."""
    async def dependency(
        request: Request,
        db: AsyncSession = Depends(get_db),
        user: User = Depends(get_current_user)
    ) -> User:
        val = request.path_params.get("prompt_id") or request.path_params.get("id") or request.query_params.get("prompt_id") or request.query_params.get("id")
        if val is None:
            # If no ID is present, bypass checking
            return user
            
        try:
            struct_id = int(val)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Identificador de estructura inválido: '{val}'"
            )
            
        perm = await get_effective_structure_permission(db, user, structure_type, struct_id)
        
        allowed = False
        if required_level == "view":
            allowed = perm["can_view"]
        elif required_level == "use":
            allowed = perm["can_use"]
        elif required_level == "edit":
            allowed = perm["can_edit"]
        elif required_level == "share":
            allowed = perm["can_share"]
        elif required_level == "delete":
            allowed = perm["can_delete"]
        elif required_level == "transfer":
            allowed = perm["can_transfer"]
        elif required_level == "archive":
            allowed = perm["can_archive"]
        elif required_level == "restore":
            allowed = perm["can_restore"]
            
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos suficientes para realizar esta operación sobre la estructura."
            )
            
        return user
    return dependency


def require_structure_view(structure_type: str):
    return Depends(check_structure_permission(structure_type, "view"))


def require_structure_use(structure_type: str):
    return Depends(check_structure_permission(structure_type, "use"))


def require_structure_edit(structure_type: str):
    return Depends(check_structure_permission(structure_type, "edit"))


def require_structure_share(structure_type: str):
    return Depends(check_structure_permission(structure_type, "share"))


def require_structure_delete(structure_type: str):
    return Depends(check_structure_permission(structure_type, "delete"))


def require_structure_transfer(structure_type: str):
    return Depends(check_structure_permission(structure_type, "transfer"))


def require_structure_archive(structure_type: str):
    return Depends(check_structure_permission(structure_type, "archive"))


def require_structure_restore(structure_type: str):
    return Depends(check_structure_permission(structure_type, "restore"))


async def log_audit(
    db: AsyncSession,
    actor_user_id: int | None,
    action: str,
    structure_type: str,
    structure_id: int,
    affected_user_id: int | None = None,
    previous_permission: str | None = None,
    new_permission: str | None = None,
    details: dict | None = None
):
    """Log an event in the structure permissions audit table."""
    try:
        audit = StructurePermissionAudit(
            actor_user_id=actor_user_id,
            action=action,
            structure_type=structure_type,
            structure_id=structure_id,
            affected_user_id=affected_user_id,
            previous_permission=previous_permission,
            new_permission=new_permission,
            details=details or {}
        )
        db.add(audit)
        await db.commit()
        logger.info(
            "Structure permission audit logged: actor=%s action=%s type=%s id=%d",
            actor_user_id, action, structure_type, structure_id
        )
    except Exception as e:
        logger.error("Failed to write to structure permissions audit log: %s", e)
        await db.rollback()
