"""
Analyses service — listing and detail queries.
"""
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyses import Analysis, AnalysisResult, CallAnalysisCurrent
from app.schemas.analyses import AnalysisDetailResponse
from app.services.analysis_results_mapper import group_results, build_summary

logger = logging.getLogger(__name__)


async def list_analyses(
    db: AsyncSession,
    analysis_type: str | None = None,
    call_id: str | None = None,
    agent: str | None = None,
    tipo_llamada: str | None = None,
    prompt_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CallAnalysisCurrent]:
    """List analyses from bm_call_analysis_current with optional filters."""
    query = select(CallAnalysisCurrent)

    if analysis_type:
        query = query.where(CallAnalysisCurrent.analysis_type == analysis_type)
    if call_id:
        query = query.where(CallAnalysisCurrent.call_id == call_id)
    if agent:
        query = query.where(CallAnalysisCurrent.agente_telefonico.ilike(f"%{agent}%"))
    if tipo_llamada:
        query = query.where(CallAnalysisCurrent.tipo_llamada == tipo_llamada)
    if prompt_id is not None:
        query = query.where(CallAnalysisCurrent.prompt_id == prompt_id)
    if date_from:
        try:
            query = query.where(CallAnalysisCurrent.updated_at >= date_from)
        except Exception:
            logger.warning("Invalid date_from value: %s — skipping filter", date_from)
    if date_to:
        try:
            query = query.where(CallAnalysisCurrent.updated_at <= date_to)
        except Exception:
            logger.warning("Invalid date_to value: %s — skipping filter", date_to)

    query = query.order_by(CallAnalysisCurrent.updated_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    return result.scalars().all()


async def get_analysis_detail(
    db: AsyncSession,
    analysis_id: int | None = None,
    call_id: str | None = None,
    analysis_type: str | None = None,
) -> AnalysisDetailResponse | None:
    """Get full analysis detail by analysis_id or by call_id+type."""

    analysis: Analysis | None = None

    if analysis_id:
        result = await db.execute(
            select(Analysis).where(Analysis.analysis_id == analysis_id)
        )
        analysis = result.scalars().first()
    elif call_id:
        # Resolve latest_analysis_id from current table
        q = select(CallAnalysisCurrent).where(CallAnalysisCurrent.call_id == call_id)
        if analysis_type:
            q = q.where(CallAnalysisCurrent.analysis_type == analysis_type)
        q = q.limit(1)
        cur_result = await db.execute(q)
        current = cur_result.scalars().first()
        if current and current.latest_analysis_id:
            a_result = await db.execute(
                select(Analysis).where(Analysis.analysis_id == current.latest_analysis_id)
            )
            analysis = a_result.scalars().first()

    if not analysis:
        return None

    # Get analysis results
    res_result = await db.execute(
        select(AnalysisResult)
        .where(AnalysisResult.analysis_id == analysis.analysis_id)
        .order_by(AnalysisResult.result_id)
    )
    results = res_result.scalars().all()

    grouped = group_results(results)
    summary = build_summary(analysis, results)

    return AnalysisDetailResponse(
        ok=True,
        analysis=analysis,
        summary=summary,
        results=list(results),
        grouped=grouped,
    )
