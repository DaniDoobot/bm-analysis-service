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

    result = await build_prompt_with_ai(
        db=db,
        prompt_id=body.prompt_id,
        instructions=active_instructions,
        draft_data=body.draft_data,
        base_structure_id=body.base_structure_id,
    )
    
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse
    
    return JSONResponse(
        content=jsonable_encoder(result), 
        media_type="application/json; charset=utf-8"
    )

