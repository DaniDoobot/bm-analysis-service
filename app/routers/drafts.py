"""Drafts router."""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from app.dependencies import get_db
from app.services import drafts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Drafts"])


class SaveDraftRequest(BaseModel):
    prompt_id: int
    draft_name: str | None = None
    draft_data: Any | None = None
    updated_by: str | None = None
    updated_by_email: str | None = None


class DraftActionRequest(BaseModel):
    draft_id: int
    updated_by: str | None = None
    updated_by_email: str | None = None


@router.get("/prompt-draft")
async def get_draft(
    prompt_id: Annotated[int, Query()],
    user_email: Annotated[str | None, Query()] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Return the active draft for a prompt (optionally filtered by user)."""
    draft = await drafts_service.get_draft(db, prompt_id=prompt_id, user_email=user_email)
    if not draft:
        return {"ok": True, "draft": None}
    return {"ok": True, "draft": draft}


@router.post("/prompt-draft/save")
async def save_draft(
    body: SaveDraftRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    draft = await drafts_service.save_draft(db, body)
    return {"ok": True, "status": "saved", "draft_id": draft.draft_id}


@router.post("/prompt-draft/discard")
async def discard_draft(
    body: DraftActionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await drafts_service.set_draft_status(db, body.draft_id, "discarded")
    return {"ok": True, "status": "discarded"}


@router.post("/prompt-draft/publish")
async def publish_draft(
    body: DraftActionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await drafts_service.set_draft_status(db, body.draft_id, "published")
    return {"ok": True, "status": "published"}
