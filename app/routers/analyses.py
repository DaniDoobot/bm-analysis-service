"""Analyses listing and detail router."""
import logging
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.analyses import AnalysisDetailResponse, AnalysisListItem
from app.services import analyses_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Analyses"])


@router.get("/analyses", response_model=list[AnalysisListItem])
async def list_analyses(
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    call_id: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    tipo_llamada: Annotated[str | None, Query()] = None,
    prompt_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """
    Legacy endpoint. List all current analyses from bm_call_analysis_current.
    Filters are applied only when provided.
    """
    return await analyses_service.list_analyses(
        db,
        analysis_type=type,
        call_id=call_id,
        agent=agent,
        tipo_llamada=tipo_llamada,
        prompt_id=prompt_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get("/analyses/current", response_model=list[AnalysisListItem])
async def list_analyses_current(
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    call_id: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    tipo_llamada: Annotated[str | None, Query()] = None,
    prompt_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """
    List all current analyses from bm_call_analysis_current (one per call_id).
    """
    return await analyses_service.list_analyses(
        db,
        analysis_type=type,
        call_id=call_id,
        agent=agent,
        tipo_llamada=tipo_llamada,
        prompt_id=prompt_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get("/analyses/history", response_model=list[AnalysisListItem])
async def list_analyses_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    call_id: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    tipo_llamada: Annotated[str | None, Query()] = None,
    prompt_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """
    List all historical analyses from bm_analyses (multiple per call_id possible).
    """
    return await analyses_service.list_analyses_history(
        db,
        analysis_type=type,
        call_id=call_id,
        agent=agent,
        tipo_llamada=tipo_llamada,
        prompt_id=prompt_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get("/analysis-detail", response_model=AnalysisDetailResponse)
async def analysis_detail(
    db: Annotated[AsyncSession, Depends(get_db)],
    analysis_id: Annotated[int | None, Query()] = None,
    call_id: Annotated[str | None, Query()] = None,
    type: Annotated[str | None, Query()] = None,
):
    """
    Return full detail of an analysis by analysis_id, or by call_id + type.
    """
    if not analysis_id and not call_id:
        raise HTTPException(status_code=400, detail="Provide analysis_id or call_id + type")

    result = await analyses_service.get_analysis_detail(
        db,
        analysis_id=analysis_id,
        call_id=call_id,
        analysis_type=type,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return result
