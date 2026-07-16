"""FastAPI router for Typologies."""
import logging
import unicodedata
import re
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_tenant_context
from app.models.typologies import Typology
from app.models.services import Service
from app.models.criteria import PromptCriterionTypology
from app.models.prompts import PromptBaseStructure, BaseStructureTypology
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.schemas.typologies import TypologyCreate, TypologyOut, TypologyUpdate, TypologyCreateFlex, TypologyCreateResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/typologies", tags=["Typologies"])


def slugify(text: str) -> str:
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8')
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '_', text)


@router.get("", response_model=list[TypologyOut])
async def list_typologies(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: int | None = None,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db)
):
    """Retrieve all typologies, optionally filtered by service_id and is_active, scoped by tenant."""
    role = context.normalized_role
    if role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: rol no autorizado para ver tipologías."
        )

    stmt = select(Typology)

    if context.is_super_admin:
        if service_id is not None:
            stmt = stmt.where(Typology.service_id == service_id)
    elif role == InternalRole.COMPANY_ADMIN:
        stmt = stmt.where(Typology.company_id == context.company_id)
        if service_id is not None:
            # Verify the service belongs to their company
            s_res = await db.execute(
                select(Service).where(
                    Service.service_id == service_id,
                    Service.company_id == context.company_id
                )
            )
            if not s_res.scalar():
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="El servicio especificado no pertenece a tu empresa."
                )
            stmt = stmt.where(Typology.service_id == service_id)
    elif role == InternalRole.SERVICE_MANAGER:
        if service_id is not None:
            if context.allowed_service_ids is None or service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para ver las tipologías de este servicio."
                )
            stmt = stmt.where(Typology.service_id == service_id)
        else:
            if not context.allowed_service_ids:
                return []
            stmt = stmt.where(Typology.service_id.in_(context.allowed_service_ids))

    if is_active is not None:
        stmt = stmt.where(Typology.is_active == is_active)

    stmt = stmt.order_by(Typology.sort_order.asc(), Typology.typology_id.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{typology_id}", response_model=TypologyOut)
async def get_typology(
    typology_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a specific typology with tenant validation."""
    role = context.normalized_role
    if role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: rol no autorizado."
        )

    stmt = select(Typology).where(Typology.typology_id == typology_id)
    result = await db.execute(stmt)
    typology = result.scalars().first()
    if not typology:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tipología con ID {typology_id} no encontrada."
        )

    # Validate access
    if not context.is_super_admin:
        if role == InternalRole.COMPANY_ADMIN:
            if typology.company_id != context.company_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para ver esta tipología."
                )
        elif role == InternalRole.SERVICE_MANAGER:
            if context.allowed_service_ids is None or typology.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para ver esta tipología."
                )

    return typology


@router.post("", response_model=TypologyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_typology(
    payload: TypologyCreateFlex,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Create a new typology. Supports flex inputs and auto-associates with active same-service base structures."""
    role = context.normalized_role
    if role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: rol no autorizado para crear tipologías."
        )

    # Resolve service_id & service name
    service_id = payload.service_id
    service_name = payload.service

    if service_id is not None:
        s_stmt = select(Service).where(Service.service_id == service_id)
    elif service_name is not None:
        s_stmt = select(Service).where(
            (func.lower(Service.service_key) == service_name.lower()) |
            (func.lower(Service.service_name) == service_name.lower())
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debe especificar service o service_id."
        )

    s_res = await db.execute(s_stmt)
    service_obj = s_res.scalars().first()
    if not service_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El servicio especificado no existe."
        )

    service_id = service_obj.service_id
    service_name = service_obj.service_name

    # Validate scope
    if not context.is_super_admin:
        if role == InternalRole.COMPANY_ADMIN:
            if service_obj.company_id != context.company_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para crear tipologías en este servicio."
                )
        elif role == InternalRole.SERVICE_MANAGER:
            if context.allowed_service_ids is None or service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para crear tipologías en este servicio."
                )

    # Resolve typology_name
    typology_name = payload.typology_name or payload.name
    if not typology_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debe especificar typology_name o name."
        )

    # Resolve key
    typology_key = payload.typology_key or slugify(typology_name)

    # Verify if a typology with the same key already exists within the service
    t_stmt = select(Typology).where(
        Typology.service_id == service_id,
        Typology.typology_key == typology_key
    )
    t_res = await db.execute(t_stmt)
    existing_typology = t_res.scalars().first()
    
    if existing_typology:
        if not existing_typology.is_active:
            # RESTORE IT logically and update its fields to keep DB constraint clean
            logger.info("Restoring soft-deleted/inactive typology (ID: %d, key: '%s') with new parameters.", existing_typology.typology_id, typology_key)
            existing_typology.is_active = True
            existing_typology.typology_name = typology_name
            existing_typology.company_id = service_obj.company_id
            if payload.description is not None:
                existing_typology.description = payload.description
            if payload.sort_order is not None:
                existing_typology.sort_order = payload.sort_order
            
            typology = existing_typology
            await db.commit()
            await db.refresh(typology)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ya existe una tipología activa con la clave '{typology_key}' en el servicio {service_id}."
            )
    else:
        typology = Typology(
            service_id=service_id,
            company_id=service_obj.company_id,
            typology_key=typology_key,
            typology_name=typology_name,
            description=payload.description,
            sort_order=payload.sort_order,
            is_active=payload.is_active
        )
        db.add(typology)
        await db.commit()
        await db.refresh(typology)

    structs_stmt = select(PromptBaseStructure).where(
        PromptBaseStructure.service_id == service_id,
        PromptBaseStructure.is_active == True
    )
    structs_res = await db.execute(structs_stmt)
    active_structs = structs_res.scalars().all()

    if not active_structs:
        logger.info(
            "No active base structures found for service_id %d. Typology %d (%s) created without auto-associations.",
            service_id,
            typology.typology_id,
            typology.typology_key
        )

    associated_count = 0
    for struct in active_structs:
        # Check if association already exists
        check_stmt = select(BaseStructureTypology).where(
            BaseStructureTypology.base_structure_id == struct.id,
            BaseStructureTypology.typology_id == typology.typology_id
        )
        check_res = await db.execute(check_stmt)
        if not check_res.scalars().first():
            db.add(
                BaseStructureTypology(
                    base_structure_id=struct.id,
                    typology_id=typology.typology_id
                )
            )
            associated_count += 1

    if associated_count > 0:
        await db.commit()

    # Query total associated count
    count_stmt = select(func.count(BaseStructureTypology.id)).where(
        BaseStructureTypology.typology_id == typology.typology_id
    )
    count_res = await db.execute(count_stmt)
    total_associated = count_res.scalar() or 0

    return TypologyCreateResponse(
        id=typology.typology_id,
        name=typology.typology_name,
        service=service_name,
        associated_base_structures_count=total_associated
    )


@router.put("/{typology_id}", response_model=TypologyOut)
async def update_typology(
    typology_id: int,
    payload: TypologyUpdate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Update details of an existing typology."""
    role = context.normalized_role
    if role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: rol no autorizado para modificar tipologías."
        )

    stmt = select(Typology).where(Typology.typology_id == typology_id)
    result = await db.execute(stmt)
    typology = result.scalars().first()
    if not typology:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tipología con ID {typology_id} no encontrada."
        )

    # Validate scope
    if not context.is_super_admin:
        if role == InternalRole.COMPANY_ADMIN:
            if typology.company_id != context.company_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para modificar esta tipología."
                )
        elif role == InternalRole.SERVICE_MANAGER:
            if context.allowed_service_ids is None or typology.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para modificar esta tipología."
                )

    if payload.typology_name is not None:
        typology.typology_name = payload.typology_name
    if payload.description is not None:
        typology.description = payload.description
    if payload.sort_order is not None:
        typology.sort_order = payload.sort_order
    if payload.is_active is not None:
        typology.is_active = payload.is_active

    await db.commit()
    await db.refresh(typology)
    return typology


@router.delete("/{typology_id}", status_code=status.HTTP_200_OK)
async def delete_typology(
    typology_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Delete a typology by soft-deleting/archiving it and clearing associations."""
    role = context.normalized_role
    if role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: rol no autorizado para eliminar tipologías."
        )

    stmt = select(Typology).where(Typology.typology_id == typology_id)
    result = await db.execute(stmt)
    typology = result.scalars().first()
    if not typology:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tipología con ID {typology_id} no encontrada."
        )

    # Validate scope
    if not context.is_super_admin:
        if role == InternalRole.COMPANY_ADMIN:
            if typology.company_id != context.company_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para eliminar esta tipología."
                )
        elif role == InternalRole.SERVICE_MANAGER:
            if context.allowed_service_ids is None or typology.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para eliminar esta tipología."
                )

    logger.info("Soft-deleting/archiving typology (ID: %d, key: '%s').", typology_id, typology.typology_key)
    typology.is_active = False
    
    # Cascade clean up from bm_prompt_criterion_typologies so inactive typologies are excluded from active prompt applicability
    await db.execute(
        delete(PromptCriterionTypology).where(PromptCriterionTypology.typology_id == typology_id)
    )
    
    await db.commit()
    return {"ok": True, "detail": f"Tipología {typology_id} archivada exitosamente y removida de matrices de aplicabilidad."}


