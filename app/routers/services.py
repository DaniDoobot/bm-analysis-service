"""FastAPI router for Services."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.services import Service
from app.models.prompts import Prompt, PromptBaseStructure
from app.schemas.services import ServiceCreate, ServiceOut, ServiceUpdate

router = APIRouter(prefix="/bm/services", tags=["Services"])


@router.get("", response_model=list[ServiceOut])
async def list_services(db: AsyncSession = Depends(get_db)):
    """Retrieve all services, sorted by service_id."""
    stmt = select(Service).order_by(Service.service_id.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{service_id}", response_model=ServiceOut)
async def get_service(service_id: int, db: AsyncSession = Depends(get_db)):
    """Retrieve details of a specific service."""
    stmt = select(Service).where(Service.service_id == service_id)
    result = await db.execute(stmt)
    service = result.scalars().first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Servicio con ID {service_id} no encontrado."
        )
    return service


@router.post("", response_model=ServiceOut, status_code=status.HTTP_201_CREATED)
async def create_service(payload: ServiceCreate, db: AsyncSession = Depends(get_db)):
    """Create a new service. Enforces unique service_key."""
    stmt = select(Service).where(Service.service_key == payload.service_key)
    res = await db.execute(stmt)
    if res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ya existe un servicio con la clave '{payload.service_key}'."
        )

    service = Service(
        service_key=payload.service_key,
        service_name=payload.service_name,
        description=payload.description,
        is_active=payload.is_active
    )
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return service


@router.put("/{service_id}", response_model=ServiceOut)
async def update_service(
    service_id: int, payload: ServiceUpdate, db: AsyncSession = Depends(get_db)
):
    """Update details of an existing service."""
    stmt = select(Service).where(Service.service_id == service_id)
    result = await db.execute(stmt)
    service = result.scalars().first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Servicio con ID {service_id} no encontrado."
        )

    if payload.service_name is not None:
        service.service_name = payload.service_name
    if payload.description is not None:
        service.description = payload.description
    if payload.is_active is not None:
        service.is_active = payload.is_active

    await db.commit()
    await db.refresh(service)
    return service


@router.delete("/{service_id}", status_code=status.HTTP_200_OK)
async def delete_service(service_id: int, db: AsyncSession = Depends(get_db)):
    """
    Delete a service.
    Validates that no active prompts or base structures reference this service before deleting.
    """
    stmt = select(Service).where(Service.service_id == service_id)
    result = await db.execute(stmt)
    service = result.scalars().first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Servicio con ID {service_id} no encontrado."
        )

    # Check for prompts referencing this service
    p_stmt = select(func.count(Prompt.prompt_id)).where(
        Prompt.service_id == service_id,
        Prompt.is_active == True,
        Prompt.is_archived == False
    )
    p_res = await db.execute(p_stmt)
    if p_res.scalar() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede eliminar el servicio porque tiene prompts activos asociados."
        )

    # Check for base structures referencing this service
    bs_stmt = select(func.count(PromptBaseStructure.id)).where(
        PromptBaseStructure.service_id == service_id,
        PromptBaseStructure.is_active == True
    )
    bs_res = await db.execute(bs_stmt)
    if bs_res.scalar() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede eliminar el servicio porque tiene estructuras base activas asociadas."
        )

    await db.delete(service)
    await db.commit()
    return {"ok": True, "detail": f"Servicio {service_id} eliminado exitosamente."}


@router.post("/backfill-default-front", status_code=status.HTTP_200_OK)
async def backfill_default_front(db: AsyncSession = Depends(get_db)):
    """
    Emergency/Administrative endpoint to force execute database seeding 
    and backfilling to the default 'front' service.
    """
    # 1. Ensure services exist
    services_data = [
        {"key": "front", "name": "Front", "desc": "Servicio de Front Desk / Recepción"},
        {"key": "experiencia_paciente", "name": "Experiencia de Paciente", "desc": "Servicio de Experiencia de Paciente"},
        {"key": "asesorias", "name": "Asesorías", "desc": "Servicio de Asesorías / Consultas"}
    ]
    service_ids_map = {}
    seeded_services_count = 0
    for s_item in services_data:
        s_key = s_item["key"]
        stmt_s = select(Service).where(Service.service_key == s_key)
        res_s = await db.execute(stmt_s)
        existing_s = res_s.scalars().first()
        if not existing_s:
            new_s = Service(
                service_key=s_key,
                service_name=s_item["name"],
                description=s_item["desc"],
                is_active=True
            )
            db.add(new_s)
            await db.flush()
            service_ids_map[s_key] = new_s.service_id
            seeded_services_count += 1
        else:
            service_ids_map[s_key] = existing_s.service_id

    # 2. Ensure Front typologies exist
    front_service_id = service_ids_map.get("front")
    seeded_typologies_count = 0
    updated_base_structures = 0
    updated_prompts = 0
    updated_associations = 0

    if front_service_id:
        typologies_data = [
            {"key": "cita", "name": "Cita", "order": 10},
            {"key": "confirmacion", "name": "Confirmación", "order": 20},
            {"key": "cancelacion", "name": "Cancelación", "order": 30},
            {"key": "reagendo", "name": "Reagendo", "order": 40},
            {"key": "falta", "name": "Falta", "order": 50},
            {"key": "otros", "name": "Otros", "order": 60}
        ]
        typology_ids = []
        for t_item in typologies_data:
            t_key = t_item["key"]
            stmt_t = select(Typology).where(Typology.service_id == front_service_id, Typology.typology_key == t_key)
            res_t = await db.execute(stmt_t)
            existing_t = res_t.scalars().first()
            if not existing_t:
                new_t = Typology(
                    service_id=front_service_id,
                    typology_key=t_key,
                    typology_name=t_item["name"],
                    sort_order=t_item["order"],
                    is_active=True
                )
                db.add(new_t)
                await db.flush()
                typology_ids.append(new_t.typology_id)
                seeded_typologies_count += 1
            else:
                typology_ids.append(existing_t.typology_id)

        # 3. Assign service_id Front to base structures
        from sqlalchemy import text
        res_bs = await db.execute(
            text("UPDATE bm_prompt_base_structures SET service_id = :front_id WHERE service_id IS NULL"),
            {"front_id": front_service_id}
        )
        updated_base_structures = res_bs.rowcount

        # 4. Assign service_id Front to prompts
        res_p = await db.execute(
            text("UPDATE bm_prompts SET service_id = :front_id WHERE service_id IS NULL"),
            {"front_id": front_service_id}
        )
        updated_prompts = res_p.rowcount

        # 5. Backfill criteria typologies associations
        from app.models.criteria import PromptCriterion, PromptCriterionTypology
        c_stmt = select(PromptCriterion.criterion_id)
        c_res = await db.execute(c_stmt)
        all_c_ids = c_res.scalars().all()
        for c_id in all_c_ids:
            for t_id in typology_ids:
                assoc_stmt = select(PromptCriterionTypology).where(
                    PromptCriterionTypology.criterion_id == c_id,
                    PromptCriterionTypology.typology_id == t_id
                )
                assoc_res = await db.execute(assoc_stmt)
                existing_assoc = assoc_res.scalars().first()
                if not existing_assoc:
                    new_assoc = PromptCriterionTypology(
                        criterion_id=c_id,
                        typology_id=t_id
                    )
                    db.add(new_assoc)
                    updated_associations += 1

        await db.commit()

    return {
        "ok": True,
        "message": "Manual emergency seeding and backfill executed successfully.",
        "seeded_services": seeded_services_count,
        "seeded_typologies": seeded_typologies_count,
        "updated_base_structures": updated_base_structures,
        "updated_prompts": updated_prompts,
        "updated_criteria_associations": updated_associations
    }

