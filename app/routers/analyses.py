"""Analyses listing and detail router."""
import logging
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_tenant_context
from app.core.tenant_context import TenantContext
from app.schemas.analyses import AnalysisDetailResponse, AnalysisListItem
from app.services import analyses_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Analyses"])


@router.get("/analyses", response_model=list[AnalysisListItem])
async def list_analyses(
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    call_id: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    tipo_llamada: Annotated[str | None, Query()] = None,
    prompt_id: Annotated[int | None, Query()] = None,
    service_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    global_score_min: Annotated[float | None, Query(ge=0.0, le=10.0)] = None,
    global_score_max: Annotated[float | None, Query(ge=0.0, le=10.0)] = None,
):
    """
    Legacy endpoint. List all current analyses from bm_call_analysis_current.
    Filters are applied only when provided.
    """
    if global_score_min is not None and global_score_max is not None:
        if global_score_min > global_score_max:
            raise HTTPException(
                status_code=422,
                detail="global_score_min cannot be greater than global_score_max",
            )

    return await analyses_service.list_analyses(
        db,
        analysis_type=type,
        call_id=call_id,
        agent=agent,
        tipo_llamada=tipo_llamada,
        prompt_id=prompt_id,
        service_id=service_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        global_score_min=global_score_min,
        global_score_max=global_score_max,
        context=context,
    )


@router.get("/analyses/current", response_model=list[AnalysisListItem])
async def list_analyses_current(
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    call_id: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    tipo_llamada: Annotated[str | None, Query()] = None,
    prompt_id: Annotated[int | None, Query()] = None,
    service_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    global_score_min: Annotated[float | None, Query(ge=0.0, le=10.0)] = None,
    global_score_max: Annotated[float | None, Query(ge=0.0, le=10.0)] = None,
):
    """
    List all current analyses from bm_call_analysis_current (one per call_id).
    """
    if global_score_min is not None and global_score_max is not None:
        if global_score_min > global_score_max:
            raise HTTPException(
                status_code=422,
                detail="global_score_min cannot be greater than global_score_max",
            )

    return await analyses_service.list_analyses(
        db,
        analysis_type=type,
        call_id=call_id,
        agent=agent,
        tipo_llamada=tipo_llamada,
        prompt_id=prompt_id,
        service_id=service_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        global_score_min=global_score_min,
        global_score_max=global_score_max,
        context=context,
    )


@router.get("/analyses/history", response_model=list[AnalysisListItem])
async def list_analyses_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    call_id: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    tipo_llamada: Annotated[str | None, Query()] = None,
    prompt_id: Annotated[int | None, Query()] = None,
    service_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    global_score_min: Annotated[float | None, Query(ge=0.0, le=10.0)] = None,
    global_score_max: Annotated[float | None, Query(ge=0.0, le=10.0)] = None,
):
    """
    List all historical analyses from bm_analyses (multiple per call_id possible).
    """
    if global_score_min is not None and global_score_max is not None:
        if global_score_min > global_score_max:
            raise HTTPException(
                status_code=422,
                detail="global_score_min cannot be greater than global_score_max",
            )

    if service_id is not None and not context.is_super_admin:
        if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado: No tienes permisos para este servicio.",
            )

    return await analyses_service.list_analyses_history(
        db,
        analysis_type=type,
        call_id=call_id,
        agent=agent,
        tipo_llamada=tipo_llamada,
        prompt_id=prompt_id,
        service_id=service_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        global_score_min=global_score_min,
        global_score_max=global_score_max,
        context=context,
    )


@router.get("/analysis-detail", response_model=AnalysisDetailResponse)
async def analysis_detail(
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
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
        context=context,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return result
