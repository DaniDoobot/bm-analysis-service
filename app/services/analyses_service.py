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
from app.utils.hubspot_owners import resolve_owner_name, resolve_agent_display

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
    items = list(result.scalars().all())
    await enrich_analyses(db, items)
    return items


async def list_analyses_history(
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
) -> list[Analysis]:
    """List all analyses from bm_analyses (history) with optional filters."""
    query = select(Analysis)

    if analysis_type:
        query = query.where(Analysis.analysis_type == analysis_type)
    if call_id:
        query = query.where(Analysis.call_id == call_id)
    if agent:
        query = query.where(Analysis.agente_telefonico.ilike(f"%{agent}%"))
    if tipo_llamada:
        query = query.where(Analysis.tipo_llamada == tipo_llamada)
    if prompt_id is not None:
        query = query.where(Analysis.prompt_id == prompt_id)
    if date_from:
        try:
            query = query.where(Analysis.created_at >= date_from)
        except Exception:
            logger.warning("Invalid date_from value: %s — skipping filter", date_from)
    if date_to:
        try:
            query = query.where(Analysis.created_at <= date_to)
        except Exception:
            logger.warning("Invalid date_to value: %s — skipping filter", date_to)

    query = query.order_by(Analysis.analysis_id.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    items = list(result.scalars().all())
    await enrich_analyses(db, items)
    return items


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

    await enrich_analyses(db, [analysis])

    # Get analysis results from new normalized table
    from app.models.analyses import AnalysisCriterionResult
    from app.schemas.analyses import AnalysisResultOut
    res_result = await db.execute(
        select(AnalysisCriterionResult)
        .where(AnalysisCriterionResult.analysis_id == analysis.analysis_id)
        .order_by(AnalysisCriterionResult.id)
    )
    criteria_results = res_result.scalars().all()
    
    results = []
    for c in criteria_results:
        results.append(AnalysisResultOut(
            result_id=c.id,
            analysis_id=c.analysis_id,
            criterion_id=c.criterion_id,
            criterion_key=c.criterion_key,
            criterion_name=c.criterion_name,
            criterion_type=c.criterion_type,
            value_number=c.numeric_value,
            value_text=c.text_value,
            value_boolean=c.boolean_value,
            value_category=c.category_value,
            feed=c.feedback,
            description=None,
            raw_value=c.value_raw,
            created_at=c.created_at,
        ))

    grouped = group_results(results)
    summary = build_summary(analysis, results)

    return AnalysisDetailResponse(
        ok=True,
        analysis=analysis,
        summary=summary,
        results=results,
        grouped=grouped,
    )


async def enrich_analyses(db: AsyncSession, items: list[Any]) -> list[Any]:
    if not items:
        return items

    prompt_ids = {item.prompt_id for item in items if getattr(item, 'prompt_id', None)}
    version_ids = {item.prompt_version_id for item in items if getattr(item, 'prompt_version_id', None)}

    prompt_map = {}
    if prompt_ids:
        from app.models.prompts import Prompt
        pr = await db.execute(select(Prompt.prompt_id, Prompt.prompt_name, Prompt.prompt_type).where(Prompt.prompt_id.in_(prompt_ids)))
        for pid, pname, ptype in pr.fetchall():
            prompt_map[pid] = (pname, ptype)

    version_map = {}
    if version_ids:
        from app.models.prompts import PromptVersion
        vr = await db.execute(select(PromptVersion.id, PromptVersion.version_label).where(PromptVersion.id.in_(version_ids)))
        for vid, vlabel in vr.fetchall():
            version_map[vid] = vlabel

    owner_ids_to_resolve = set()
    for item in items:
        agent = getattr(item, 'agente_telefonico', None)
        owner_id = getattr(item, 'hubspot_owner_id', None)
        if owner_id:
            if not agent or (isinstance(agent, str) and agent.isdigit()):
                owner_ids_to_resolve.add(owner_id)

    agent_map = {}
    if owner_ids_to_resolve:
        from app.models.analyses import Analysis
        for oid in owner_ids_to_resolve:
            qr = await db.execute(
                select(Analysis.agente_telefonico)
                .where(Analysis.hubspot_owner_id == oid)
                .where(Analysis.agente_telefonico.is_not(None))
                .where(~Analysis.agente_telefonico.op('~')('^[0-9]+$'))
                .order_by(Analysis.analysis_id.desc())
                .limit(1)
            )
            val = qr.scalar()
            if val:
                agent_map[oid] = val

    for item in items:
        pid = getattr(item, 'prompt_id', None)
        vid = getattr(item, 'prompt_version_id', None)

        pname = f"Prompt #{pid}" if pid else None
        ptype = None
        if pid and pid in prompt_map:
            pname, ptype = prompt_map[pid]
        
        vlabel = f"Versión #{vid}" if vid else None
        if vid and vid in version_map and version_map[vid]:
            vlabel = version_map[vid]

        setattr(item, 'prompt_name', pname)
        setattr(item, 'prompt_type', ptype)
        setattr(item, 'prompt_version_label', vlabel)

        agent = getattr(item, 'agente_telefonico', None)
        owner_id = getattr(item, 'hubspot_owner_id', None)
        
        # 1. Try static resolution first
        resolved_name = resolve_owner_name(owner_id)
        if resolved_name:
            setattr(item, 'agente_telefonico', resolved_name)
            setattr(item, 'agente_telefonico_display', resolved_name)
            continue
            
        # 2. Try DB fallback resolution
        db_resolved_name = None
        if owner_id in agent_map and (not agent or (isinstance(agent, str) and agent.isdigit())):
            db_resolved_name = agent_map[owner_id]
            
        # 3. Use resolve_agent_display for fallback display
        display_name = resolve_agent_display(db_resolved_name or agent, owner_id)
        
        setattr(item, 'agente_telefonico_display', display_name)
        if display_name and not str(display_name).isdigit():
            setattr(item, 'agente_telefonico', display_name)

    return items
