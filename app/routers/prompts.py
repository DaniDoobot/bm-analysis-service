"""Prompts router — thin layer delegating to service functions."""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, require_admin, get_tenant_context
from app.models.users import User
from app.models.services import Service
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.services.auth_service import (
    get_effective_structure_permission,
    require_structure_view,
    require_structure_use,
    require_structure_edit,
    require_structure_share,
    require_structure_delete,
    require_structure_transfer,
    require_structure_archive,
    require_structure_restore,
    log_audit,
)
from app.models.prompts import StructurePermission, PromptBaseStructure, BaseStructureTypology
from app.models.typologies import Typology
from app.schemas.prompts import (
    ActivateVersionRequest,
    ActivePromptOut,
    PromptVersionOut,
    PromptWithCurrentVersion,
    SavePromptRequest,
    PromptBaseStructureOut,
    PromptBaseStructureDetailOut,
    PromptBaseStructureNestedDetailOut,
    PromptBaseStructureCreate,
    PromptBaseStructureUpdate,
    CreateFromBaseRequest,
    CreateFromBaseResponse,
    StructureType,
    StructurePermissionOut,
    PermissionActionResponse,
    GrantPermissionRequest,
    TransferOwnershipRequest,
    TypologyItem,
)
from app.services import prompts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Prompts"])


async def _enrich_structure_response(
    db: AsyncSession,
    user: User,
    structure_type: str,
    structure_id: int,
    owner_user_id: int | None,
    response_dict: dict
) -> dict:
    owner_data = None
    if owner_user_id:
        owner_res = await db.execute(select(User).where(User.user_id == owner_user_id))
        owner_obj = owner_res.scalars().first()
        if owner_obj:
            owner_data = {
                "user_id": owner_obj.user_id,
                "display_name": owner_obj.username,
                "email": owner_obj.email
            }
    response_dict["owner"] = owner_data

    access_data = await get_effective_structure_permission(db, user, structure_type, structure_id)
    response_dict["access"] = access_data

    return response_dict


class AssignBaseStructureRequest(BaseModel):
    base_structure_id: int


@router.get("/prompts", response_model=list[PromptWithCurrentVersion])
async def list_prompts(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    base_structure_id: Annotated[int | None, Query(description="Filter by base structure ID")] = None,
    base_structure_key: Annotated[str | None, Query(description="Filter by base structure Key")] = None,
    active: Annotated[bool | None, Query(description="Filter by active status")] = None,
    include_archived: Annotated[bool, Query(description="Include archived structures")] = False,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
):
    """Return all prompts with their current version (if any), with optional filtering."""
    role = context.normalized_role
    if role == InternalRole.AGENT:
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")

    target_service_id = service_id
    target_service_ids = None

    if not context.is_super_admin:
        if role in (InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR):
            if target_service_id is not None:
                if context.allowed_service_ids is None or target_service_id not in context.allowed_service_ids:
                    raise HTTPException(status_code=403, detail="Acceso denegado: No tienes permisos sobre este servicio.")
            else:
                target_service_ids = context.allowed_service_ids

    prompts = await prompts_service.list_prompts(
        db,
        prompt_type=type,
        base_structure_id=base_structure_id,
        base_structure_key=base_structure_key,
        is_active=active,
        include_archived=include_archived,
        service_id=target_service_id,
        service_ids=target_service_ids,
    )

    enriched_prompts = []
    for p in prompts:
        pid = p["prompt_id"]
        owner_id = p.get("owner_user_id")
        access = await get_effective_structure_permission(db, current_user, "specific", pid)
        if access["can_view"]:
            enriched = await _enrich_structure_response(
                db, current_user, "specific", pid, owner_id, p
            )
            enriched_prompts.append(enriched)
            
    return enriched_prompts


@router.put("/prompts/{prompt_id}/base-structure", dependencies=[require_structure_edit("specific")])
async def assign_base_structure(
    prompt_id: int,
    body: AssignBaseStructureRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Assign a base structure to an existing prompt (metadata reference only).
    """
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
    try:
        return await prompts_service.assign_base_structure(
            db, prompt_id=prompt_id, base_structure_id=body.base_structure_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))



@router.get("/prompts/active", response_model=ActivePromptOut)
async def get_active_prompt(
    type: Annotated[str, Query(description="audio | text")],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service_id: Annotated[int | None, Query(description="Scope active prompt to this service ID")] = None,
):
    """Return the active prompt for the given type and service, including current version text."""
    context = await TenantContext.build(current_user, db)
    role = context.normalized_role
    
    if role == InternalRole.AGENT:
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")

    resolved_service_id = service_id
    if resolved_service_id is not None:
        if not context.is_super_admin:
            s_stmt = select(Service).where(Service.service_id == resolved_service_id)
            if role == InternalRole.COMPANY_ADMIN:
                s_stmt = s_stmt.where(Service.company_id == context.company_id)
            elif role in (InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR):
                s_stmt = s_stmt.where(Service.service_id.in_(context.allowed_service_ids))
            s_res = await db.execute(s_stmt)
            if not s_res.scalar():
                raise HTTPException(status_code=403, detail="No tienes permisos para ver el prompt activo de este servicio.")
    else:
        if context.is_super_admin:
            s_stmt = select(Service.service_id).limit(1)
            s_res = await db.execute(s_stmt)
            resolved_service_id = s_res.scalar()
        elif role == InternalRole.COMPANY_ADMIN:
            s_stmt = select(Service.service_id).where(Service.company_id == context.company_id).limit(1)
            s_res = await db.execute(s_stmt)
            resolved_service_id = s_res.scalar()
        elif role in (InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR):
            if context.allowed_service_ids:
                resolved_service_id = context.allowed_service_ids[0]
                
    if resolved_service_id is None:
        raise HTTPException(status_code=400, detail="No se pudo determinar el servicio para resolver el prompt activo.")

    result = await prompts_service.get_active_prompt(db, prompt_type=type, service_id=resolved_service_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"No active prompt found for type '{type}' and service {resolved_service_id}")
    
    # Check permissions dynamically
    pid = result["prompt_id"]
    perm = await get_effective_structure_permission(db, current_user, "specific", pid)
    if not perm["can_use"]:
        raise HTTPException(status_code=403, detail="No tienes permisos para usar esta estructura.")
        
    return result


@router.get("/prompt-versions", response_model=list[PromptVersionOut], dependencies=[require_structure_view("specific")])
async def list_prompt_versions(
    prompt_id: Annotated[int, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: Annotated[bool, Query(description="Include archived/hidden versions (admin only)")] = False,
):
    """Return non-archived versions of a prompt by default. Use include_archived=true for full audit history."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
    if include_archived and current_user.role not in {"admin", "administrador"}:
        raise HTTPException(status_code=403, detail="Solo los administradores pueden ver versiones archivadas.")
    return await prompts_service.list_versions(db, prompt_id=prompt_id, include_archived=include_archived)


@router.post("/save-prompt")
async def save_prompt(
    body: SavePromptRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Create a new prompt version and mark it as current."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
    perm = await get_effective_structure_permission(db, current_user, "specific", body.prompt_id)
    if not perm["can_edit"]:
        raise HTTPException(status_code=403, detail="No tienes permisos de edición sobre esta estructura.")
    version = await prompts_service.save_prompt_version(db, body)
    return {"ok": True, "status": "created", "version": PromptVersionOut.model_validate(version)}


@router.post("/activate-prompt-version")
async def activate_prompt_version(
    body: ActivateVersionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Mark a specific version as current for its prompt."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
    from app.models.prompts import PromptVersion
    stmt = select(PromptVersion).where(PromptVersion.id == body.id)
    res = await db.execute(stmt)
    version = res.scalars().first()
    if not version:
        raise HTTPException(status_code=404, detail=f"Version {body.id} not found")
        
    perm = await get_effective_structure_permission(db, current_user, "specific", version.prompt_id)
    if not perm["can_edit"]:
        raise HTTPException(status_code=403, detail="No tienes permisos de edición sobre esta estructura.")
        
    version = await prompts_service.activate_version(db, version_id=body.id)
    return {"ok": True, "status": "activated", "version": PromptVersionOut.model_validate(version)}


# ── Prompt Base Structures Endpoints ──────────────────────────────────────────

def _base_structure_out(struct) -> dict:
    return {
        "id": struct.id,
        "structure_key": struct.structure_key,
        "structure_name": struct.structure_name,
        "description": struct.description,
        "prompt_type": "text",
        "is_active": struct.is_active,
        "created_at": struct.created_at,
        "updated_at": struct.updated_at,
        "created_by": struct.created_by,
        "created_by_email": struct.created_by_email,
        "service_id": struct.service_id,
        "service_key": struct.service.service_key if struct.service else None,
        "service_name": struct.service.service_name if struct.service else None,
    }


def _base_structure_detail_out(struct) -> dict:
    return {
        "id": struct.id,
        "structure_key": struct.structure_key,
        "structure_name": struct.structure_name,
        "description": struct.description,
        "prompt_type": "text",
        "is_active": struct.is_active,
        "created_at": struct.created_at,
        "updated_at": struct.updated_at,
        "created_by": struct.created_by,
        "created_by_email": struct.created_by_email,
        "base_prompt": struct.base_prompt,
        "default_criteria": [],
        "service_id": struct.service_id,
        "service_key": struct.service.service_key if struct.service else None,
        "service_name": struct.service.service_name if struct.service else None,
    }


@router.get("/prompt-base-structures", response_model=list[PromptBaseStructureOut])
async def list_prompt_base_structures(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    include_archived: Annotated[bool, Query(description="Include inactive/archived structures")] = False,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
):
    """Return active base structures by default; pass include_archived=true to see all."""
    role = context.normalized_role
    if role == InternalRole.AGENT:
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")

    target_service_id = service_id
    target_service_ids = None

    if not context.is_super_admin:
        if role in (InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR):
            if target_service_id is not None:
                if context.allowed_service_ids is None or target_service_id not in context.allowed_service_ids:
                    raise HTTPException(status_code=403, detail="Acceso denegado: No tienes permisos sobre este servicio.")
            else:
                target_service_ids = context.allowed_service_ids

    structures = await prompts_service.list_base_structures(
        db,
        prompt_type=type,
        include_archived=include_archived,
        service_id=target_service_id,
        service_ids=target_service_ids,
    )
    
    enriched_structures = []
    for s in structures:
        access = await get_effective_structure_permission(db, current_user, "base", s.id)
        if access["can_view"]:
            s_dict = _base_structure_out(s)
            enriched = await _enrich_structure_response(
                db, current_user, "base", s.id, s.owner_user_id, s_dict
            )
            enriched_structures.append(enriched)
            
    return enriched_structures


async def _get_base_structure_nested_dict(
    db: AsyncSession,
    struct: PromptBaseStructure,
    current_user: User
) -> dict:
    """Helper to fetch and format the nested detail response for a base structure."""
    # 1. Enrich base structure
    s_dict = _base_structure_detail_out(struct)
    enriched_structure = await _enrich_structure_response(
        db, current_user, "base", struct.id, struct.owner_user_id, s_dict
    )

    service_name = struct.service.service_name if struct.service else "Desconocido"
    service_key = struct.service.service_key if struct.service else "Desconocido"

    associated_typologies = []
    available_typologies = []
    inactive_associated_typologies = []

    if struct.service_id:
        # 2. Get associated typologies
        assoc_stmt = select(Typology).join(
            BaseStructureTypology, BaseStructureTypology.typology_id == Typology.typology_id
        ).where(
            BaseStructureTypology.base_structure_id == struct.id
        )
        assoc_res = await db.execute(assoc_stmt)
        assoc_list = list(assoc_res.scalars().all())

        # Format associated typologies
        for t in assoc_list:
            item = TypologyItem(
                id=t.typology_id,
                key=t.typology_key,
                name=t.typology_name,
                service=service_name,
                typology_key=t.typology_key,
                service_id=t.service_id,
                service_key=service_key,
                is_active=t.is_active,
                description=t.description
            )
            if t.is_active:
                associated_typologies.append(item)
            else:
                inactive_associated_typologies.append(item)

        # 3. Get all active typologies for the service
        all_typo_stmt = select(Typology).where(
            Typology.service_id == struct.service_id,
            Typology.is_active == True
        )
        all_typo_res = await db.execute(all_typo_stmt)
        all_typos = all_typo_res.scalars().all()

        assoc_ids = {t.typology_id for t in assoc_list}

        # Format available typologies
        for t in all_typos:
            if t.typology_id not in assoc_ids:
                available_typologies.append(
                    TypologyItem(
                        id=t.typology_id,
                        key=t.typology_key,
                        name=t.typology_name,
                        service=service_name,
                        typology_key=t.typology_key,
                        service_id=t.service_id,
                        service_key=service_key,
                        is_active=t.is_active,
                        description=t.description
                    )
                )

    # Sanitize base_prompt dynamically on read/preview using only active associated typologies
    from app.services.prompt_builder import sanitize_legacy_typologies_block
    if enriched_structure.get("base_prompt"):
        enriched_structure["base_prompt"] = sanitize_legacy_typologies_block(
            enriched_structure["base_prompt"], associated_typologies
        )

    return {
        "structure": enriched_structure,
        "associated_typologies": associated_typologies,
        "available_typologies": available_typologies,
        "inactive_associated_typologies": inactive_associated_typologies
    }


@router.get("/prompt-base-structures/{id}", response_model=PromptBaseStructureNestedDetailOut, dependencies=[require_structure_view("base")])
async def get_prompt_base_structure(
    id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Return detailed base structure by ID including associated and available typologies."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
    struct = await prompts_service.get_base_structure(db, structure_id=id)
    if not struct:
        raise HTTPException(status_code=404, detail=f"Base structure {id} not found.")
    
    return await _get_base_structure_nested_dict(db, struct, current_user)


@router.post("/prompt-base-structures", response_model=PromptBaseStructureDetailOut)
async def create_prompt_base_structure(
    body: PromptBaseStructureCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Create a new prompt base structure."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
        
    is_admin = getattr(current_user, "role", "usuario").lower() in ("admin", "administrador")
    if not is_admin or body.owner_user_id is None:
        body.owner_user_id = current_user.user_id
        
    struct = await prompts_service.create_base_structure(db, body)
    
    await log_audit(db, current_user.user_id, "create", "base", struct.id)
    
    s_dict = _base_structure_detail_out(struct)
    return await _enrich_structure_response(db, current_user, "base", struct.id, struct.owner_user_id, s_dict)


@router.put("/prompt-base-structures/{id}", response_model=PromptBaseStructureDetailOut, dependencies=[require_structure_edit("base")])
async def update_prompt_base_structure(
    id: int,
    body: PromptBaseStructureUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Update an existing prompt base structure."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
    struct = await prompts_service.update_base_structure(db, structure_id=id, body=body)
    if not struct:
        raise HTTPException(status_code=404, detail=f"Base structure {id} not found.")
    
    await log_audit(db, current_user.user_id, "modify", "base", struct.id)
    
    s_dict = _base_structure_detail_out(struct)
    return await _enrich_structure_response(db, current_user, "base", struct.id, struct.owner_user_id, s_dict)


@router.delete("/prompt-base-structures/{id}")
async def delete_prompt_base_structure(
    id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    confirm: bool = False,
    force: bool = False,
    confirm_active: bool = Query(False, description="Requerido junto con confirm=true para borrar en cascada estructuras específicas ACTIVAS."),
):
    """Delete a prompt base structure with dependency check.

    Flujo:
    - Sin confirm: devuelve 409 con resumen de dependencias (incluye número de prompts activos).
    - confirm=true sin confirm_active: si hay dependencias activas, devuelve 409 con aviso específico.
    - confirm=true + confirm_active=true: elimina en cascada incluyendo prompts activos.
    """
    role = context.normalized_role
    if role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(status_code=403, detail="Los agentes y coordinadores no tienen acceso para eliminar estructuras.")

    struct = await db.get(PromptBaseStructure, id)
    if not struct:
        raise HTTPException(status_code=404, detail=f"Base structure {id} not found.")

    from app.routers.base_structures import _verify_base_structure_tenant_access
    _verify_base_structure_tenant_access(struct, context)

    from fastapi.responses import JSONResponse
    result = await prompts_service.delete_base_structure(db, id, confirm=confirm or force, confirm_active=confirm_active)

    if not result.get("deleted"):
        if result.get("has_dependencies") or result.get("blocked_by_active_prompts"):
            return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=result)

    await log_audit(db, current_user.user_id, "delete", "base", id)
    return {"ok": True, "message": f"Estructura base {id} eliminada correctamente.", "details": result}


@router.post("/prompts/create-from-base", response_model=CreateFromBaseResponse)
async def create_prompt_from_base(
    body: CreateFromBaseRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Create a new prompt and versions/criteria populated from base structure."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")

    # Validate permission on the base structure (minimum 'use')
    base_perm = await get_effective_structure_permission(db, current_user, "base", body.base_structure_id)
    if not base_perm["can_use"]:
        raise HTTPException(status_code=403, detail="No tienes permisos suficientes para usar la estructura base.")

    is_admin = getattr(current_user, "role", "usuario").lower() in ("admin", "administrador")
    if not is_admin or body.owner_user_id is None:
        body.owner_user_id = current_user.user_id

    try:
        res = await prompts_service.create_prompt_from_base(db, body=body)
        await log_audit(db, current_user.user_id, "create", "specific", res["prompt_id"])
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/prompt-base-structures/boston-medical/refresh", dependencies=[Depends(require_admin)])
async def refresh_boston_medical_base_structure(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Manually synchronizes the 'boston_medical_audio' structure from active prompt 1
    and its active criteria.
    """
    try:
        return await prompts_service.refresh_boston_medical_base_structure(db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/prompt-base-structures/backfill-clear-criteria", dependencies=[Depends(require_admin)])
async def backfill_clear_criteria(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Emergency backfill: sets default_criteria = NULL for all prompt base structures.
    Idempotent and safe — use to force-clean legacy data without a restart.
    """
    from sqlalchemy import text
    result = await db.execute(
        text("UPDATE bm_prompt_base_structures SET default_criteria = NULL WHERE default_criteria IS NOT NULL;")
    )
    await db.commit()
    return {
        "ok": True,
        "rows_updated": result.rowcount,
        "message": "All base structures cleared of default_criteria.",
    }


class ArchiveRequest(BaseModel):
    user_email: str | None = None


class UpdateCurrentRequest(BaseModel):
    prompt: str
    prompt_name: str | None = None
    description: str | None = None
    updated_by: str | None = None
    updated_by_email: str | None = None


@router.put("/prompts/{prompt_id}/current", dependencies=[require_structure_edit("specific")])
async def update_prompt_current(
    prompt_id: int,
    body: UpdateCurrentRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Overwrite the current prompt content without creating a visible new version.
    This is the 'Save' / 'Edit in place' operation.
    """
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
    try:
        result = await prompts_service.update_prompt_current(
            db,
            prompt_id=prompt_id,
            prompt_text=body.prompt,
            prompt_name=body.prompt_name,
            description=body.description,
            updated_by=body.updated_by,
            updated_by_email=body.updated_by_email,
        )
        await log_audit(db, current_user.user_id, "modify", "specific", prompt_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class DuplicatePromptRequest(BaseModel):
    prompt_name: str
    description: str | None = None
    created_by: str | None = None
    created_by_email: str | None = None


@router.post("/prompts/{prompt_id}/duplicate")
async def duplicate_prompt(
    prompt_id: int,
    body: DuplicatePromptRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Create a fully independent copy of an existing prompt with its content and criteria.
    The new prompt starts as inactive/unpublished.
    """
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")

    # Check if user has permission to duplicate the source prompt (requires 'can_duplicate')
    perm = await get_effective_structure_permission(db, current_user, "specific", prompt_id)
    if not perm["can_duplicate"]:
        raise HTTPException(status_code=403, detail="No tienes permisos suficientes para duplicar esta estructura.")

    try:
        result = await prompts_service.duplicate_prompt(
            db,
            source_prompt_id=prompt_id,
            prompt_name=body.prompt_name,
            description=body.description,
            created_by=body.created_by,
            created_by_email=body.created_by_email,
            owner_user_id=current_user.user_id,
        )
        await log_audit(db, current_user.user_id, "duplicate", "specific", result["prompt_id"], details={"source_prompt_id": prompt_id})
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/prompts/{prompt_id}/archive", dependencies=[require_structure_archive("specific")])
async def archive_prompt(
    prompt_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    body: ArchiveRequest | None = None,
):
    """Archive a prompt structure."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
    from app.services import archive_service
    user_email = body.user_email if body else None
    try:
        prompt = await archive_service.archive_prompt(db, prompt_id, user_email=user_email)
        await log_audit(db, current_user.user_id, "modify", "specific", prompt_id, details={"archived": True})
        return {
            "ok": True,
            "status": "archived",
            "prompt_id": prompt.prompt_id,
            "is_archived": prompt.is_archived,
            "archived_at": prompt.archived_at,
            "archived_by_email": prompt.archived_by_email,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/prompts/{prompt_id}/restore", dependencies=[require_structure_restore("specific")])
async def restore_prompt(
    prompt_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Restore an archived prompt as inactive/draft."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")
    from app.services import archive_service
    try:
        prompt = await archive_service.restore_prompt(db, prompt_id)
        await log_audit(db, current_user.user_id, "modify", "specific", prompt_id, details={"restored": True})
        return {
            "ok": True,
            "status": "restored",
            "prompt_id": prompt.prompt_id,
            "is_archived": prompt.is_archived,
            "is_active": prompt.is_active,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(
    prompt_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_admin)],
):
    """Hard delete a prompt structure if safeguards allow. Admin only."""
    from app.services import archive_service
    try:
        res = await archive_service.delete_prompt(db, prompt_id)
        await log_audit(db, current_user.user_id, "delete", "specific", prompt_id)
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{structure_type}s/{id}/permissions", response_model=list[StructurePermissionOut])
async def get_structure_permissions(
    structure_type: StructureType,
    id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    structure_type = structure_type.value
    if structure_type == "prompt":
        structure_type = "specific"
    elif structure_type == "prompt-base-structure":
        structure_type = "base"

    if structure_type not in ("base", "specific"):
        raise HTTPException(status_code=400, detail="Tipo de estructura inválido.")
        
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")

    # Only admin or owner can query permissions
    perm = await get_effective_structure_permission(db, current_user, structure_type, id)
    if not (perm["is_admin"] or perm["is_owner"]):
        raise HTTPException(status_code=403, detail="Solo los administradores o propietarios pueden ver los permisos.")

    stmt = select(StructurePermission, User).join(User, StructurePermission.user_id == User.user_id).where(
        StructurePermission.structure_type == structure_type,
        StructurePermission.structure_id == id
    )
    res = await db.execute(stmt)
    rows = res.all()
    
    out = []
    for p_obj, u_obj in rows:
        out.append({
            "permission_id": p_obj.permission_id,
            "user_id": u_obj.user_id,
            "username": u_obj.username,
            "email": u_obj.email,
            "permission_level": p_obj.permission_level,
            "granted_by_user_id": p_obj.granted_by_user_id,
            "created_at": p_obj.created_at,
            "updated_at": p_obj.updated_at
        })
    return out





@router.post("/{structure_type}s/{id}/permissions", response_model=PermissionActionResponse)
async def grant_structure_permission(
    structure_type: StructureType,
    id: int,
    body: GrantPermissionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    structure_type = structure_type.value
    if structure_type == "prompt":
        structure_type = "specific"
    elif structure_type == "prompt-base-structure":
        structure_type = "base"

    if structure_type not in ("base", "specific"):
        raise HTTPException(status_code=400, detail="Tipo de estructura inválido.")
        
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")

    # Only admin or owner can modify permissions
    perm = await get_effective_structure_permission(db, current_user, structure_type, id)
    if not (perm["is_admin"] or perm["is_owner"]):
        raise HTTPException(status_code=403, detail="Solo los administradores o propietarios pueden gestionar permisos.")

    if body.permission_level not in ("view", "use", "edit"):
        raise HTTPException(status_code=400, detail="Nivel de permiso inválido.")

    # Fetch target user
    stmt_u = select(User).where(User.user_id == body.user_id)
    res_u = await db.execute(stmt_u)
    target_user = res_u.scalars().first()
    
    if not target_user:
        raise HTTPException(status_code=400, detail="Usuario a compartir no encontrado.")
    if not target_user.is_active:
        raise HTTPException(status_code=400, detail="No se puede compartir con un usuario inactivo.")
    if target_user.role == "agent":
        raise HTTPException(status_code=400, detail="No se puede compartir con un agente.")

    # Check if target user is owner or admin (they shouldn't have rows in bm_structure_permissions)
    target_perm = await get_effective_structure_permission(db, target_user, structure_type, id)
    if target_perm["is_admin"] or target_perm["is_owner"]:
        raise HTTPException(status_code=400, detail="El usuario ya tiene acceso completo por rol o propiedad.")

    # Check if manual permission already exists
    stmt_p = select(StructurePermission).where(
        StructurePermission.structure_type == structure_type,
        StructurePermission.structure_id == id,
        StructurePermission.user_id == body.user_id
    )
    res_p = await db.execute(stmt_p)
    existing_p = res_p.scalars().first()
    
    if existing_p:
        prev_level = existing_p.permission_level
        existing_p.permission_level = body.permission_level
        existing_p.granted_by_user_id = current_user.user_id
        db.add(existing_p)
        await db.commit()
        await log_audit(
            db,
            current_user.user_id,
            "modify",
            structure_type,
            id,
            affected_user_id=body.user_id,
            previous_permission=prev_level,
            new_permission=body.permission_level
        )
    else:
        new_p = StructurePermission(
            structure_type=structure_type,
            structure_id=id,
            user_id=body.user_id,
            permission_level=body.permission_level,
            granted_by_user_id=current_user.user_id
        )
        db.add(new_p)
        await db.commit()
        await log_audit(
            db,
            current_user.user_id,
            "grant",
            structure_type,
            id,
            affected_user_id=body.user_id,
            previous_permission="none",
            new_permission=body.permission_level
        )
        
    return {"ok": True, "message": "Permiso actualizado con éxito."}


@router.delete("/{structure_type}s/{id}/permissions/{user_id}", response_model=PermissionActionResponse)
async def revoke_structure_permission(
    structure_type: StructureType,
    id: int,
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    structure_type = structure_type.value
    if structure_type == "prompt":
        structure_type = "specific"
    elif structure_type == "prompt-base-structure":
        structure_type = "base"

    if structure_type not in ("base", "specific"):
        raise HTTPException(status_code=400, detail="Tipo de estructura inválido.")
        
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(status_code=403, detail="Los agentes no tienen acceso a las estructuras.")

    # Only admin or owner can modify permissions
    perm = await get_effective_structure_permission(db, current_user, structure_type, id)
    if not (perm["is_admin"] or perm["is_owner"]):
        raise HTTPException(status_code=403, detail="Solo los administradores o propietarios pueden gestionar permisos.")

    stmt_p = select(StructurePermission).where(
        StructurePermission.structure_type == structure_type,
        StructurePermission.structure_id == id,
        StructurePermission.user_id == user_id
    )
    res_p = await db.execute(stmt_p)
    existing_p = res_p.scalars().first()
    
    if not existing_p:
        raise HTTPException(status_code=404, detail="El permiso no existe para este usuario.")
        
    prev_level = existing_p.permission_level
    await db.delete(existing_p)
    await db.commit()
    
    await log_audit(
        db,
        current_user.user_id,
        "revoke",
        structure_type,
        id,
        affected_user_id=user_id,
        previous_permission=prev_level,
        new_permission="none"
    )
    
    return {"ok": True, "message": "Permiso revocado con éxito."}


@router.post("/{structure_type}s/{id}/transfer-ownership", response_model=PermissionActionResponse)
async def transfer_structure_ownership(
    structure_type: StructureType,
    id: int,
    body: TransferOwnershipRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_admin)],
):
    structure_type = structure_type.value
    if structure_type == "prompt":
        structure_type = "specific"
    elif structure_type == "prompt-base-structure":
        structure_type = "base"

    if structure_type not in ("base", "specific"):
        raise HTTPException(status_code=400, detail="Tipo de estructura inválido.")

    # Fetch structure to verify existence and get current owner
    from app.models.prompts import Prompt, PromptBaseStructure
    old_owner_id = None
    struct_obj = None
    if structure_type == "base":
        stmt = select(PromptBaseStructure).where(PromptBaseStructure.id == id)
        res = await db.execute(stmt)
        struct_obj = res.scalars().first()
        if not struct_obj:
            raise HTTPException(status_code=404, detail=f"Base structure {id} not found.")
        old_owner_id = struct_obj.owner_user_id
    elif structure_type == "specific":
        stmt = select(Prompt).where(Prompt.prompt_id == id)
        res = await db.execute(stmt)
        struct_obj = res.scalars().first()
        if not struct_obj:
            raise HTTPException(status_code=404, detail=f"Specific structure {id} not found.")
        old_owner_id = struct_obj.owner_user_id

    # Fetch new owner
    stmt_u = select(User).where(User.user_id == body.new_owner_user_id)
    res_u = await db.execute(stmt_u)
    new_owner = res_u.scalars().first()
    
    if not new_owner:
        raise HTTPException(status_code=400, detail="Nuevo propietario no encontrado.")
    if not new_owner.is_active:
        raise HTTPException(status_code=400, detail="El nuevo propietario debe ser un usuario activo.")
    if new_owner.role == "agent":
        raise HTTPException(status_code=400, detail="El nuevo propietario no puede ser un agente.")

    # Transfer ownership
    struct_obj.owner_user_id = body.new_owner_user_id
    db.add(struct_obj)

    # Prevent redundant manual permission for new owner
    from sqlalchemy import delete
    await db.execute(
        delete(StructurePermission).where(
            StructurePermission.structure_type == structure_type,
            StructurePermission.structure_id == id,
            StructurePermission.user_id == body.new_owner_user_id
        )
    )
    
    await db.commit()
    
    await log_audit(
        db,
        current_user.user_id,
        "transfer",
        structure_type,
        id,
        affected_user_id=body.new_owner_user_id,
        details={"previous_owner_id": old_owner_id}
    )
    
    return {"ok": True, "message": "Propiedad transferida con éxito."}
