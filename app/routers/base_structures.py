"""FastAPI router for Base Structures typology associations."""
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_tenant_context, get_current_user
from app.models.prompts import PromptBaseStructure, BaseStructureTypology
from app.models.typologies import Typology
from app.models.users import User
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.schemas.prompts import UpdateTypologiesRequest, PromptBaseStructureNestedDetailOut
from app.routers.prompts import _get_base_structure_nested_dict

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/base-structures", tags=["Base Structures Typologies"])


def _verify_base_structure_tenant_access(struct: PromptBaseStructure, context: TenantContext):
    """Enforces multi-tenant restrictions on base structures."""
    if context.is_super_admin:
        return

    role = context.normalized_role
    if role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: rol no autorizado."
        )

    # Check company ownership unless global
    is_global = getattr(struct, "is_global", False)
    if not is_global:
        if struct.company_id != context.company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: esta estructura pertenece a otra empresa."
            )
        # Check service scope for service managers and team coordinators
        if role in (InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR):
            if context.allowed_service_ids is None or struct.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: no tienes permisos sobre el servicio de esta estructura."
                )


@router.get("/{id}", response_model=PromptBaseStructureNestedDetailOut)
async def get_base_structure_with_typologies(
    id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Retrieve detailed base structure by ID including associated and available typologies."""
    role = context.normalized_role
    if role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Los agentes no tienen acceso a las estructuras."
        )

    # 1. Fetch base structure
    struct = await db.get(PromptBaseStructure, id)
    if not struct:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Estructura base con ID {id} no encontrada."
        )

    _verify_base_structure_tenant_access(struct, context)

    return await _get_base_structure_nested_dict(db, struct, current_user)


@router.patch("/{id}/typologies", response_model=PromptBaseStructureNestedDetailOut)
async def update_base_structure_typologies(
    id: int,
    payload: UpdateTypologiesRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Replace all typology associations of a base structure with the specified typology IDs."""
    role = context.normalized_role
    if role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Los agentes y coordinadores no tienen acceso a las estructuras."
        )

    # 1. Fetch base structure
    struct = await db.get(PromptBaseStructure, id)
    if not struct:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Estructura base con ID {id} no encontrada."
        )

    _verify_base_structure_tenant_access(struct, context)

    if not struct.service_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La estructura base no tiene un servicio asignado."
        )

    # Validate that all typology IDs exist and belong to the same service
    unique_ids = list(dict.fromkeys(payload.typology_ids or []))
    if unique_ids:
        t_stmt = select(Typology).where(
            Typology.typology_id.in_(unique_ids)
        )
        t_res = await db.execute(t_stmt)
        typos = t_res.scalars().all()
        
        if len(typos) != len(unique_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Una o más tipologías especificadas no existen."
            )

        for t in typos:
            if t.service_id != struct.service_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"La tipología '{t.typology_name}' pertenece al servicio {t.service_id}, pero la estructura pertenece al servicio {struct.service_id}."
                )

    # Clear existing associations
    await db.execute(
        delete(BaseStructureTypology).where(BaseStructureTypology.base_structure_id == id)
    )

    # Insert new associations
    for tid in unique_ids:
        db.add(
            BaseStructureTypology(
                base_structure_id=id,
                typology_id=tid
            )
        )

    # Update base_prompt skeleton if empty or default template
    from app.services.prompts_service import generate_base_prompt_skeleton
    service_name = struct.service.service_name if struct.service else None
    if not struct.base_prompt or not struct.base_prompt.strip() or struct.base_prompt.startswith("### CONTEXTO"):
        struct.base_prompt = generate_base_prompt_skeleton(service_name, typos if unique_ids else [])

    await db.commit()
    await db.refresh(struct)
    
    return await _get_base_structure_nested_dict(db, struct, current_user)


@router.delete("/{id}")
async def delete_base_structure(
    id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    current_user: Annotated[User, Depends(get_current_user)],
    confirm: bool = False,
    force: bool = False,
    confirm_active: bool = Query(False, description="Requerido junto con confirm=true para borrar en cascada estructuras específicas ACTIVAS. Previene dejar el servicio sin prompt activo por accidente."),
):
    """Delete base structure (Super Admin or Company Admin in scope). Checks for dependencies.

    Flujo:
    - Sin confirm: devuelve 409 con resumen de dependencias (incluye número de prompts activos).
    - confirm=true sin confirm_active: si hay dependencias activas, devuelve 409 con aviso específico.
    - confirm=true + confirm_active=true: elimina en cascada incluyendo prompts activos.
    """
    role = context.normalized_role
    if role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Los agentes y coordinadores no tienen acceso para eliminar estructuras."
        )

    struct = await db.get(PromptBaseStructure, id)
    if not struct:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Estructura base con ID {id} no encontrada."
        )

    _verify_base_structure_tenant_access(struct, context)

    from app.services.prompts_service import delete_base_structure as svc_delete
    from fastapi.responses import JSONResponse

    result = await svc_delete(db, id, confirm=confirm or force, confirm_active=confirm_active)

    if not result.get("deleted"):
        if result.get("has_dependencies") or result.get("blocked_by_active_prompts"):
            return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=result)

    return {"ok": True, "message": f"Estructura base {id} eliminada correctamente.", "details": result}
