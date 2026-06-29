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
    global_score_min: float | None = None,
    global_score_max: float | None = None,
) -> list[CallAnalysisCurrent]:
    """List analyses from bm_call_analysis_current with optional filters."""
    query = select(CallAnalysisCurrent)

    # Exclude test records from all listings and metrics
    query = query.where(~CallAnalysisCurrent.call_id.like("TEST_%"))
    query = query.where(CallAnalysisCurrent.hubspot_owner_id != "test_owner")

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
    if global_score_min is not None:
        query = query.where(CallAnalysisCurrent.evaluacion_global.is_not(None))
        query = query.where(CallAnalysisCurrent.evaluacion_global >= global_score_min)
    if global_score_max is not None:
        query = query.where(CallAnalysisCurrent.evaluacion_global.is_not(None))
        query = query.where(CallAnalysisCurrent.evaluacion_global <= global_score_max)

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
    global_score_min: float | None = None,
    global_score_max: float | None = None,
) -> list[Analysis]:
    """List all historical analyses from bm_analyses (history) with optional filters."""
    query = select(Analysis)

    # Exclude test records from all listings and metrics
    query = query.where(~Analysis.call_id.like("TEST_%"))
    query = query.where(Analysis.hubspot_owner_id != "test_owner")

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
    if global_score_min is not None:
        query = query.where(Analysis.evaluacion_global.is_not(None))
        query = query.where(Analysis.evaluacion_global >= global_score_min)
    if global_score_max is not None:
        query = query.where(Analysis.evaluacion_global.is_not(None))
        query = query.where(Analysis.evaluacion_global <= global_score_max)

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

    # Query service & typology metadata from bm_analysis_criterion_results
    analysis_ids = {getattr(item, 'analysis_id', None) or getattr(item, 'latest_analysis_id', None) for item in items}
    analysis_ids = {aid for aid in analysis_ids if aid is not None}

    service_typology_map = {}
    if analysis_ids:
        from app.models.analyses import AnalysisCriterionResult
        from sqlalchemy import func
        q_services = await db.execute(
            select(
                AnalysisCriterionResult.analysis_id,
                func.max(AnalysisCriterionResult.service_id).label('service_id'),
                func.max(AnalysisCriterionResult.service_key).label('service_key'),
                func.max(AnalysisCriterionResult.service_name).label('service_name'),
                func.max(AnalysisCriterionResult.typology_id).label('typology_id'),
                func.max(AnalysisCriterionResult.typology_key).label('typology_key'),
                func.max(AnalysisCriterionResult.typology_name).label('typology_name')
            )
            .where(AnalysisCriterionResult.analysis_id.in_(analysis_ids))
            .group_by(AnalysisCriterionResult.analysis_id)
        )
        for row in q_services.fetchall():
            m = row._mapping
            service_typology_map[m['analysis_id']] = {
                "service_id": m['service_id'],
                "service_key": m['service_key'],
                "service_name": m['service_name'],
                "typology_id": m['typology_id'],
                "typology_key": m['typology_key'],
                "typology_name": m['typology_name']
            }

    typology_mapping = {
        "cita": "Cita",
        "confirmacion": "Confirmación",
        "cancelacion": "Cancelación",
        "reagendo": "Reagendo",
        "falta": "Falta",
        "otros": "Otros",
        "informacion_sin_cita": "Información sin cita"
    }

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
        else:
            # 2. Try DB fallback resolution
            db_resolved_name = None
            if owner_id in agent_map and (not agent or (isinstance(agent, str) and agent.isdigit())):
                db_resolved_name = agent_map[owner_id]
                
            # 3. Use resolve_agent_display for fallback display
            display_name = resolve_agent_display(db_resolved_name or agent, owner_id)
            
            setattr(item, 'agente_telefonico_display', display_name)
            if display_name and not str(display_name).isdigit():
                setattr(item, 'agente_telefonico', display_name)

        # Service & Typology enrichment
        aid = getattr(item, 'analysis_id', None) or getattr(item, 'latest_analysis_id', None)
        s_data = service_typology_map.get(aid) if aid else None
        
        s_id = s_data["service_id"] if s_data else None
        s_key = s_data["service_key"] if s_data else None
        s_name = s_data["service_name"] if s_data else None
        t_id = s_data["typology_id"] if s_data else None
        t_key = s_data["typology_key"] if s_data else None
        t_name = s_data["typology_name"] if s_data else None

        # Typology fallback from tipo_llamada if not set
        tipo_llamada = getattr(item, 'tipo_llamada', None)
        if tipo_llamada:
            if not t_key:
                t_key = tipo_llamada
            if not t_name:
                t_name = typology_mapping.get(tipo_llamada)
                if not t_name:
                    t_name = tipo_llamada.replace("_", " ").title()

        setattr(item, 'service_id', s_id)
        setattr(item, 'service_key', s_key)
        setattr(item, 'service_name', s_name)
        setattr(item, 'typology_id', t_id)
        setattr(item, 'typology_key', t_key)
        setattr(item, 'typology_name', t_name)

    return items
