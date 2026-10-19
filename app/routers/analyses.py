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


async def verify_analysis_read_scope(
    db: AsyncSession,
    analysis_id: int,
    context: TenantContext,
):
    """
    Verify that the analysis exists and the requesting user has tenant/service/agent
    scope to access it.
    Returns the Analysis ORM row if allowed; raises 404 if not found or 403 if out of scope.
    """
    from sqlalchemy import select
    from app.models.analyses import Analysis
    from app.models.prompts import Prompt
    from app.models.services import Service

    res = await db.execute(select(Analysis).where(Analysis.analysis_id == analysis_id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Análisis no encontrado.")

    if context.is_super_admin:
        return record

    effective_company_id = record.company_id
    effective_service_id = record.service_id

    if (effective_company_id is None or effective_service_id is None) and record.prompt_id:
        p_res = await db.execute(
            select(Prompt.company_id, Prompt.service_id).where(Prompt.prompt_id == record.prompt_id)
        )
        p_row = p_res.first()
        if p_row:
            if effective_company_id is None and p_row[0] is not None:
                effective_company_id = p_row[0]
            if effective_service_id is None and p_row[1] is not None:
                effective_service_id = p_row[1]

    if effective_company_id is None and effective_service_id is not None:
        s_res = await db.execute(
            select(Service.company_id).where(Service.service_id == effective_service_id)
        )
        effective_company_id = s_res.scalar_one_or_none()

    if context.allowed_company_ids is not None:
        if effective_company_id is None or effective_company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado: El análisis pertenece a otra empresa.",
            )
    elif context.company_id is not None and effective_company_id != context.company_id:
        raise HTTPException(
            status_code=403,
            detail="Acceso denegado: El análisis pertenece a otra empresa.",
        )

    if context.allowed_service_ids is not None:
        if effective_service_id is None or effective_service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado: No tienes permisos sobre este servicio.",
            )

    if context.allowed_agent_ids is not None:
        if not record.hubspot_owner_id or record.hubspot_owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado: Solo puedes acceder a tus propios análisis.",
            )

    return record


@router.get("/analyses/{analysis_id}/recording-audio")
@router.get("/me/analyses/{analysis_id}/recording-audio")
async def get_analysis_recording_audio(
    analysis_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
):
    """
    Protected server-side proxy that downloads and streams the call recording
    associated with a Laboratorio / Test Analysis (bm_analyses) record.

    Resolution priority:
    1. CloudTalk direct WAV (via cloudtalk_id / hs_call_external_id)
    2. Fallback recording_url (grabaciones.bostonmedical.info with X-Api-Key, or Twilio)
    """
    from fastapi.responses import Response
    from app.core.side_effects import resolve_company_demo_status
    from app.services.audio_download_service import (
        AudioDownloadError,
        AudioDownloadService,
        detect_audio_format,
    )
    from app.services.hubspot_service import HubSpotService

    record = await verify_analysis_read_scope(db, analysis_id, context)

    payload = record.payload if isinstance(record.payload, dict) else {}
    hs_data = payload.get("hubspot_data") if isinstance(payload.get("hubspot_data"), dict) else {}
    hs_props = (
        hs_data.get("hubspot_properties")
        if isinstance(hs_data.get("hubspot_properties"), dict)
        else (payload.get("hubspot_properties") if isinstance(payload.get("hubspot_properties"), dict) else {})
    )

    raw_recording_url = (
        payload.get("recording_url")
        or payload.get("audio_url")
        or hs_data.get("recording_url")
        or hs_props.get("hs_call_recording_url")
    )
    cloudtalk_id = (
        payload.get("cloudtalk_id")
        or payload.get("hs_call_external_id")
        or hs_data.get("cloudtalk_id")
        or hs_data.get("hs_call_external_id")
        or hs_props.get("hs_call_external_id")
    )

    # Dynamic HubSpot resolution only when neither cloudtalk_id nor raw_recording_url is in payload
    call_id_str = str(record.call_id).strip() if record.call_id else ""
    if not cloudtalk_id and not raw_recording_url and call_id_str and not call_id_str.startswith("TEST_"):
        try:
            _, is_demo = await resolve_company_demo_status(
                db,
                company_id=record.company_id,
                service_id=record.service_id,
            )
            hs_service = HubSpotService(is_demo=is_demo)
            resolved_hs = await hs_service.get_call(call_id_str, is_demo=is_demo)
            if isinstance(resolved_hs, dict):
                if not cloudtalk_id:
                    cloudtalk_id = (
                        resolved_hs.get("cloudtalk_id")
                        or resolved_hs.get("hs_call_external_id")
                        or (resolved_hs.get("hubspot_properties") or {}).get("hs_call_external_id")
                    )
                if not raw_recording_url:
                    raw_recording_url = (
                        resolved_hs.get("recording_url")
                        or (resolved_hs.get("hubspot_properties") or {}).get("hs_call_recording_url")
                    )
        except Exception as e:
            logger.warning(
                "Dynamic HubSpot lookup failed during recording-audio for analysis_id=%d (call_id=%s): %s",
                analysis_id,
                call_id_str,
                e,
            )

    if not raw_recording_url and not cloudtalk_id:
        raise HTTPException(
            status_code=404,
            detail="No hay grabación disponible para este análisis.",
        )

    try:
        downloader = AudioDownloadService()
        audio_bytes = await downloader.download_audio(
            raw_recording_url,
            cloudtalk_id=str(cloudtalk_id) if cloudtalk_id else None,
        )
    except AudioDownloadError as e:
        logger.warning(
            "Recording download failed for analysis_id=%d (stage=%s, status=%s): %s",
            analysis_id,
            e.stage,
            e.status_code,
            e.message,
        )
        status_code = 404 if e.status_code == 404 else 502
        raise HTTPException(
            status_code=status_code,
            detail=f"No se pudo obtener la grabación de audio ({e.message}).",
        )
    except Exception as e:
        logger.error(
            "Unexpected error downloading recording for analysis_id=%d: %s",
            analysis_id,
            e,
        )
        raise HTTPException(
            status_code=502,
            detail="Error interno al descargar la grabación de audio.",
        )

    ext = detect_audio_format(audio_bytes, raw_recording_url)
    media_type = "audio/wav" if ext == "wav" else "audio/mpeg"

    return Response(
        content=audio_bytes,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="analysis_{analysis_id}.{ext}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.delete("/analyses/{analysis_id}")
async def delete_analysis(
    analysis_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
):
    """
    Securely delete an analysis and clean up dependent records:
    - Verifies permissions and tenant scope (super_admin, company_admin or user within scope).
    - Deletes dependent criteria and log records.
    - Updates or removes CallAnalysisCurrent if pointing to this analysis.
    - Deletes the main Analysis record.
    """
    from sqlalchemy import select, delete, desc
    from app.models.analyses import Analysis, CallAnalysisCurrent, AnalysisResult, AnalysisCriterionResult

    record = await verify_analysis_read_scope(db, analysis_id, context)

    call_id = record.call_id
    analysis_type = record.analysis_type

    # 1. Clean up dependent child records in bm_analysis_results and bm_analysis_criterion_results
    await db.execute(
        delete(AnalysisResult).where(AnalysisResult.analysis_id == analysis_id)
    )
    await db.execute(
        delete(AnalysisCriterionResult).where(AnalysisCriterionResult.analysis_id == analysis_id)
    )

    # 2. Handle bm_call_analysis_current
    if call_id:
        cur_stmt = select(CallAnalysisCurrent).where(CallAnalysisCurrent.call_id == call_id)
        if analysis_type:
            cur_stmt = cur_stmt.where(CallAnalysisCurrent.analysis_type == analysis_type)
        cur_res = await db.execute(cur_stmt)
        current_rows = cur_res.scalars().all()

        for cur_row in current_rows:
            if cur_row.latest_analysis_id == analysis_id:
                # Find the next most recent analysis for this call_id
                next_stmt = (
                    select(Analysis)
                    .where(Analysis.call_id == call_id)
                    .where(Analysis.analysis_id != analysis_id)
                )
                if cur_row.analysis_type:
                    next_stmt = next_stmt.where(Analysis.analysis_type == cur_row.analysis_type)
                next_stmt = next_stmt.order_by(desc(Analysis.analysis_id)).limit(1)

                next_res = await db.execute(next_stmt)
                prev_analysis = next_res.scalar_one_or_none()

                if prev_analysis:
                    # Demote to previous analysis
                    cur_row.latest_analysis_id = prev_analysis.analysis_id
                    cur_row.company_id = prev_analysis.company_id
                    cur_row.service_id = prev_analysis.service_id
                    cur_row.hubspot_url = prev_analysis.hubspot_url
                    cur_row.call_direction = prev_analysis.call_direction
                    cur_row.call_timestamp = prev_analysis.call_timestamp
                    cur_row.source = prev_analysis.source
                    cur_row.fecha_eval = prev_analysis.fecha_eval
                    cur_row.agente_telefonico = prev_analysis.agente_telefonico
                    cur_row.hubspot_owner_id = prev_analysis.hubspot_owner_id
                    cur_row.prompt_id = prev_analysis.prompt_id
                    cur_row.prompt_version_id = prev_analysis.prompt_version_id
                    cur_row.status = prev_analysis.status
                    cur_row.tipo_llamada = prev_analysis.tipo_llamada
                    cur_row.evaluacion_global = prev_analysis.evaluacion_global
                    cur_row.result = prev_analysis.result
                    cur_row.payload = prev_analysis.payload
                else:
                    # No other analysis exists for this call_id -> delete current row
                    await db.delete(cur_row)

    # 3. Delete the Analysis row itself
    await db.delete(record)
    await db.commit()

    logger.info("Successfully deleted analysis_id=%d by user=%s", analysis_id, context.user_id)
    return {
        "ok": True,
        "detail": f"Análisis {analysis_id} eliminado correctamente.",
        "analysis_id": analysis_id,
    }

