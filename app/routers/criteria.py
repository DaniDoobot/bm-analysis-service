"""Criteria router."""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.criteria import (
    CriteriaGroupedOut,
    CriterionOut,
    SaveCriterionRequest,
    ToggleCriterionRequest,
    DeleteCriterionRequest,
    AIDescriptionRequest,
    AIDescriptionResponse,
)
from app.schemas.typologies import CriterionTypologyAssociation
from app.services import criteria_service
from app.services.prompts_service import PromptValidationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Criteria"])


@router.get("/prompt-criteria", response_model=CriteriaGroupedOut)
async def get_prompt_criteria(
    prompt_id: Annotated[int, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_deleted: bool = False,
):
    """Return active criteria for a prompt, grouped by criterion_type."""
    return await criteria_service.get_criteria_grouped(db, prompt_id=prompt_id, include_deleted=include_deleted)


@router.post("/prompt-criteria/save")
async def save_criterion(
    body: SaveCriterionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create, restore, or update a criterion."""
    logger.info(f"Received request to save criterion. Body: {body.model_dump()}")
    try:
        criterion = await criteria_service.save_criterion(db, body)
        logger.info(f"Successfully saved criterion (ID: {criterion.criterion_id}, key: '{criterion.criterion_key}').")
        
        # Run validation post-save
        from app.services.prompts_service import validate_prompt_sync
        val_result = await validate_prompt_sync(db, criterion.prompt_id)
        
        return {
            "ok": True,
            "status": "saved",
            "criterion_id": criterion.criterion_id,
            "criterion": CriterionOut.model_validate(criterion),
            "prompt_sync": val_result
        }
    except PromptValidationError as val_ex:
        logger.warning(f"Prompt validation failed: {val_ex}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "PROMPT_VALIDATION_FAILED",
                "message": str(val_ex),
                "suggestion": "El prompt final supera el límite defensivo de 120,000 caracteres. Intente compactar las descripciones de los criterios o desactivar criterios redundantes."
            }
        )
    except criteria_service.CriterionSyncError as e:
        return {
            "ok": False,
            "error": "CRITERION_SYNC_FAILED",
            "missing_in_output_format": e.val_result["missing_in_output_format"],
            "missing_in_prompt": e.val_result["missing_in_prompt"]
        }
    except HTTPException as he:
        logger.warning(f"HTTPException while saving criterion: {he.detail} (status: {he.status_code})")
        raise he
    except Exception as e:
        logger.exception("Unexpected error while saving criterion: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Error interno del servidor al guardar el criterio: {str(e)}"
        )


@router.post("/prompt-criteria/toggle")
async def toggle_criterion(
    body: ToggleCriterionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Activate or deactivate a criterion."""
    try:
        await criteria_service.toggle_criterion(db, body.criterion_id, body.is_active)
        from app.services.prompts_service import validate_prompt_sync
        from app.models.criteria import PromptCriterion
        
        criterion = await db.get(PromptCriterion, body.criterion_id)
        prompt_id = criterion.prompt_id if criterion else 0
        val_result = await validate_prompt_sync(db, prompt_id) if prompt_id else {"ok": True, "missing_in_prompt": [], "missing_in_output_format": [], "orphan_keys_removed": []}
        
        return {
            "ok": True,
            "status": "updated",
            "criterion_id": body.criterion_id,
            "is_active": body.is_active,
            "prompt_sync": val_result
        }
    except PromptValidationError as val_ex:
        logger.warning(f"Prompt validation failed: {val_ex}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "PROMPT_VALIDATION_FAILED",
                "message": str(val_ex),
                "suggestion": "El prompt final supera el límite defensivo de 120,000 caracteres. Intente compactar las descripciones de los criterios o desactivar criterios redundantes."
            }
        )
    except criteria_service.CriterionSyncError as e:
        return {
            "ok": False,
            "error": "CRITERION_SYNC_FAILED",
            "missing_in_output_format": e.val_result["missing_in_output_format"],
            "missing_in_prompt": e.val_result["missing_in_prompt"]
        }


@router.get("/prompt-criteria/{criterion_id}/typologies", response_model=list[CriterionTypologyAssociation])
async def get_criterion_typologies(
    criterion_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Retrieve all active typologies of the service and whether they are associated with the criterion."""
    return await criteria_service.get_criterion_typologies(db, criterion_id=criterion_id)


@router.put("/prompt-criteria/{criterion_id}/typologies")
async def update_criterion_typologies(
    criterion_id: int,
    typology_ids: list[int],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update typology associations for a specific criterion."""
    try:
        res = await criteria_service.update_criterion_typologies(db, criterion_id=criterion_id, typology_ids=typology_ids)
        from app.services.prompts_service import validate_prompt_sync
        from app.models.criteria import PromptCriterion
        
        criterion = await db.get(PromptCriterion, criterion_id)
        prompt_id = criterion.prompt_id if criterion else 0
        val_result = await validate_prompt_sync(db, prompt_id) if prompt_id else {"ok": True, "missing_in_prompt": [], "missing_in_output_format": [], "orphan_keys_removed": []}
        
        return {
            "ok": True,
            "detail": res.get("detail"),
            "prompt_sync": val_result
        }
    except PromptValidationError as val_ex:
        logger.warning(f"Prompt validation failed: {val_ex}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "PROMPT_VALIDATION_FAILED",
                "message": str(val_ex),
                "suggestion": "El prompt final supera el límite defensivo de 120,000 caracteres. Intente compactar las descripciones de los criterios o desactivar criterios redundantes."
            }
        )
    except criteria_service.CriterionSyncError as e:
        return {
            "ok": False,
            "error": "CRITERION_SYNC_FAILED",
            "missing_in_output_format": e.val_result["missing_in_output_format"],
            "missing_in_prompt": e.val_result["missing_in_prompt"]
        }


@router.delete("/prompt-criteria/{criterion_id}")
async def delete_criterion(
    criterion_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: DeleteCriterionRequest | None = None,
):
    """Delete or soft-delete a criterion."""
    try:
        email = body.performed_by_email if body else None
        from app.models.criteria import PromptCriterion
        
        criterion = await db.get(PromptCriterion, criterion_id)
        prompt_id = criterion.prompt_id if criterion else 0
        
        res = await criteria_service.delete_criterion(db, criterion_id=criterion_id, performed_by_email=email)
        from app.services.prompts_service import validate_prompt_sync
        val_result = await validate_prompt_sync(db, prompt_id) if prompt_id else {"ok": True, "missing_in_prompt": [], "missing_in_output_format": [], "orphan_keys_removed": []}
        
        return {
            "ok": True,
            "criterion_id": criterion_id,
            "action": res.get("action"),
            "message": res.get("message"),
            "prompt_sync": val_result
        }
    except PromptValidationError as val_ex:
        logger.warning(f"Prompt validation failed: {val_ex}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "PROMPT_VALIDATION_FAILED",
                "message": str(val_ex),
                "suggestion": "El prompt final supera el límite defensivo de 120,000 caracteres. Intente compactar las descripciones de los criterios o desactivar criterios redundantes."
            }
        )
    except criteria_service.CriterionSyncError as e:
        return {
            "ok": False,
            "error": "CRITERION_SYNC_FAILED",
            "missing_in_output_format": e.val_result["missing_in_output_format"],
            "missing_in_prompt": e.val_result["missing_in_prompt"]
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.exception("Error deleting criterion %s: %s", criterion_id, e)
        raise HTTPException(status_code=400, detail=f"Error eliminando el criterio: {str(e)}")


@router.post("/prompt-criteria/{criterion_id}/ai-description", response_model=AIDescriptionResponse)
@router.post("/criteria/{criterion_id}/ai-description", response_model=AIDescriptionResponse)
async def generate_ai_description(
    criterion_id: int,
    body: AIDescriptionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Generate or improve a single criterion's description using AI based on user instructions.
    Does NOT save automatically; returns the generated text for review.
    """
    from fastapi import HTTPException
    logger.info(f"Generating AI description for criterion_id={criterion_id}, name='{body.criterion_name}'")
    try:
        return await criteria_service.generate_criterion_description_ai(db, criterion_id, body)
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.exception(f"Unexpected error generating AI description for criterion_id={criterion_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error al generar la descripción con IA: {str(e)}"
        )


@router.post("/prompt-criteria/ai-description", response_model=AIDescriptionResponse)
@router.post("/criteria/ai-description", response_model=AIDescriptionResponse)
async def generate_ai_description_no_id(
    body: AIDescriptionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Generate or improve a single criterion's description using AI based on user instructions.
    Does NOT require a criterion_id (used during creation).
    Does NOT save automatically.
    """
    from fastapi import HTTPException
    logger.info(f"Generating AI description for new criterion, name='{body.criterion_name}'")
    try:
        return await criteria_service.generate_criterion_description_ai(db, None, body)
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.exception("Unexpected error generating AI description for new criterion: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Error al generar la descripción con IA: {str(e)}"
        )


