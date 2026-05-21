"""
Build-with-AI router — generates a prompt using OpenAI based on active criteria.
"""
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services.prompt_builder import build_prompt_with_ai

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["AI Prompt Builder"])


class BuildWithAIRequest(BaseModel):
    prompt_id: int
    base_structure_id: int | None = None
    instructions: str | None = None
    general_instructions: str | None = None  # Alias for fallback
    draft_data: Any | None = None
    updated_by: str | None = None
    updated_by_email: str | None = None
    version_name: str | None = None
    change_note: str | None = None


@router.post("/prompt/build-with-ai")
async def build_with_ai_endpoint(
    body: BuildWithAIRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Generate a new prompt version using AI based on active criteria from DB.
    """
    # Use instructions if provided, fallback to general_instructions
    active_instructions = body.instructions or body.general_instructions

    try:
        result = await build_prompt_with_ai(
            db=db,
            prompt_id=body.prompt_id,
            instructions=active_instructions,
            draft_data=body.draft_data,
            base_structure_id=body.base_structure_id,
            version_name=body.version_name,
            change_note=body.change_note,
        )
    except Exception as e:
        logger.exception("Unexpected exception occurred during build-with-ai for prompt_id=%s:", body.prompt_id)
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"Error interno durante la generación: {str(e)}"
        )

    if not result or not result.get("ok"):
        from fastapi import HTTPException
        error_msg = result.get("error_message") if result else "No se pudo generar una estructura válida."
        # If error mentions legacy typologies, return the clean non-normalizable message requested
        if "legacy" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail="No se pudo generar una estructura válida porque el borrador contiene tipologías legacy no normalizables."
            )
        raise HTTPException(
            status_code=400,
            detail=error_msg
        )

    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse
    
    return JSONResponse(
        content=jsonable_encoder(result), 
        media_type="application/json; charset=utf-8"
    )

