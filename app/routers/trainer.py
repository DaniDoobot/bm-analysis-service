"""FastAPI APIRouter for Trainer simulations, evaluation configs, sessions, and AI prompt assistance."""
import logging
from typing import Annotated, List, Optional
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.dependencies import get_db, get_current_user, get_tenant_context
from app.models.users import User
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.schemas.trainer import (
    TrainerEvaluationConfigCreate,
    TrainerEvaluationConfigUpdate,
    TrainerEvaluationConfigResponse,
    TrainerSimulationCreate,
    TrainerSimulationUpdate,
    TrainerSimulationResponse,
    TrainerSessionResponse,
    TrainerSessionList,
    AIPromptGenerateRequest,
    AIPromptImproveRequest,
    AvailableSpeechStructure,
)
from app.services.trainer_service import TrainerService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/trainer", tags=["Trainer Module"])


# ── Security Helpers ─────────────────────────────────────────────────────────

def enforce_admin_role(user: User):
    """Enforce that the logged-in user is an administrator (Legacy support)."""
    if user.role not in ["admin", "administrador"]:
        logger.warning("Access denied: User ID %s does not have administrator role.", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol de administrador para realizar esta operación."
        )


def enforce_agent_or_admin_ownership(user: User, hubspot_owner_id: str):
    """Enforce that a user can only access their own data unless they are an admin (Legacy support)."""
    if user.role in ["admin", "administrador"]:
        return
    
    if not user.hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID. Contacta con administración."
        )
        
    if user.hubspot_owner_id != hubspot_owner_id:
        logger.warning(
            "Access denied: User ID %s (agent %s) tried to access agent %s session data.",
            user.user_id, user.hubspot_owner_id, hubspot_owner_id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para ver el entrenamiento de otros agentes."
        )


async def verify_simulation_write_scope(db: AsyncSession, simulation_id: int, context: TenantContext):
    """Verify that the simulation exists and is within the actor's company and service scopes."""
    from app.models.trainer import TrainerSimulation
    sim = await TrainerService.get_simulation(db, simulation_id)
    if not sim:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulación no encontrada.")
    if not context.is_super_admin:
        if sim.company_id not in context.allowed_company_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: La simulación pertenece a otra empresa.")
        if context.allowed_service_ids is not None:
            if sim.service_id not in context.allowed_service_ids:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: No tienes permisos para este servicio.")
    return sim


async def verify_config_write_scope(db: AsyncSession, config_id: int, context: TenantContext):
    """Verify that the evaluation config exists and is within the actor's company and service scopes."""
    from app.models.trainer import TrainerEvaluationConfig
    cfg = await TrainerService.get_evaluation_config(db, config_id)
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuración no encontrada.")
    if not context.is_super_admin:
        if cfg.company_id not in context.allowed_company_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: La configuración pertenece a otra empresa.")
        if context.allowed_service_ids is not None:
            if cfg.service_id not in context.allowed_service_ids:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: No tienes permisos para este servicio.")
    return cfg


async def verify_session_read_scope(db: AsyncSession, session_id: int, context: TenantContext):
    """Verify that the session exists and is within the actor's company, service, and agent scopes."""
    sess = await TrainerService.get_session_detail(db, session_id)
    if not sess:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sesión no encontrada.")
    if not context.is_super_admin:
        if sess.company_id not in context.allowed_company_ids:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sesión no encontrada.")
        if context.allowed_service_ids is not None:
            if sess.service_id not in context.allowed_service_ids:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sesión no encontrada.")
        if context.allowed_agent_ids is not None:
            if sess.agent_id not in context.allowed_agent_ids:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tienes permisos para ver esta sesión.")
    return sess


# ── Simulations Endpoints ─────────────────────────────────────────────────────

@router.get("/simulations", response_model=List[TrainerSimulationResponse])
async def list_simulations(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    code: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List all simulations with optional filtering."""
    if service_id is not None and not context.is_super_admin:
        if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: No tienes permisos para este servicio.")
        
        # Resolve company_id of the service
        from app.models.services import Service
        stmt_svc = select(Service.company_id).where(Service.service_id == service_id)
        res_svc = await db.execute(stmt_svc)
        svc_company_id = res_svc.scalar()
        if svc_company_id not in context.allowed_company_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: El servicio pertenece a otra empresa.")

    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_service_ids = context.allowed_service_ids if not context.is_super_admin else None

    return await TrainerService.list_simulations(
        db,
        service_id=service_id,
        status=status,
        search=search,
        code=code,
        company_ids=company_ids,
        allowed_service_ids=allowed_service_ids,
    )


@router.get("/simulations/{simulation_id}", response_model=TrainerSimulationResponse)
async def get_simulation(
    simulation_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Get details of a single simulation."""
    sim = await verify_simulation_write_scope(db, simulation_id, context)
    return sim


@router.post("/simulations", response_model=TrainerSimulationResponse, status_code=status.HTTP_201_CREATED)
async def create_simulation(
    payload: TrainerSimulationCreate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Create a new simulation in draft state (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    # Validate service_id ownership
    from app.models.services import Service
    stmt_svc = select(Service.company_id).where(Service.service_id == payload.service_id)
    res_svc = await db.execute(stmt_svc)
    svc_company_id = res_svc.scalar()
    if not svc_company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Servicio no encontrado.")
    if not context.is_super_admin:
        if svc_company_id not in context.allowed_company_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: El servicio pertenece a otra empresa.")
        if context.allowed_service_ids is not None:
            if payload.service_id not in context.allowed_service_ids:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: No tienes permisos para este servicio.")

    try:
        return await TrainerService.create_simulation(db, payload, created_by=context.user_email)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.patch("/simulations/{simulation_id}", response_model=TrainerSimulationResponse)
async def update_simulation(
    simulation_id: int,
    payload: TrainerSimulationUpdate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Update an existing simulation (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )
    
    await verify_simulation_write_scope(db, simulation_id, context)
    try:
        sim = await TrainerService.update_simulation(db, simulation_id, payload, updated_by=context.user_email)
        return sim
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.post("/simulations/{simulation_id}/publish", response_model=TrainerSimulationResponse)
async def publish_simulation(
    simulation_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Publish a simulation making it active for voice calls (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )
    
    await verify_simulation_write_scope(db, simulation_id, context)
    try:
        return await TrainerService.publish_simulation(db, simulation_id, user_email=context.user_email)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.post("/simulations/{simulation_id}/archive", response_model=TrainerSimulationResponse)
async def archive_simulation(
    simulation_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Archive a simulation to hide/disable it (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )
    
    await verify_simulation_write_scope(db, simulation_id, context)
    sim = await TrainerService.archive_simulation(db, simulation_id)
    return sim


@router.post("/simulations/{simulation_id}/duplicate", response_model=TrainerSimulationResponse)
async def duplicate_simulation(
    simulation_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Duplicate an existing simulation as a new draft (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )
    
    await verify_simulation_write_scope(db, simulation_id, context)
    try:
        return await TrainerService.duplicate_simulation(db, simulation_id, user_email=context.user_email)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


# ── Evaluation Configs Endpoints ──────────────────────────────────────────────

@router.get("/evaluation-configs", response_model=List[TrainerEvaluationConfigResponse])
async def list_evaluation_configs(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: Optional[int] = Query(None),
    is_active: Optional[bool] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List all evaluation configs."""
    if service_id is not None and not context.is_super_admin:
        if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: No tienes permisos para este servicio.")
        
        # Resolve company_id of the service
        from app.models.services import Service
        stmt_svc = select(Service.company_id).where(Service.service_id == service_id)
        res_svc = await db.execute(stmt_svc)
        svc_company_id = res_svc.scalar()
        if svc_company_id not in context.allowed_company_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: El servicio pertenece a otra empresa.")

    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_service_ids = context.allowed_service_ids if not context.is_super_admin else None

    return await TrainerService.list_evaluation_configs(
        db,
        service_id=service_id,
        is_active=is_active,
        company_ids=company_ids,
        allowed_service_ids=allowed_service_ids,
    )


@router.get("/evaluation-configs/{config_id}", response_model=TrainerEvaluationConfigResponse)
async def get_evaluation_config(
    config_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Get single evaluation config."""
    cfg = await verify_config_write_scope(db, config_id, context)
    return cfg


@router.post("/evaluation-configs", response_model=TrainerEvaluationConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_evaluation_config(
    payload: TrainerEvaluationConfigCreate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Create a new evaluation config (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    # Validate service_id ownership
    from app.models.services import Service
    stmt_svc = select(Service.company_id).where(Service.service_id == payload.service_id)
    res_svc = await db.execute(stmt_svc)
    svc_company_id = res_svc.scalar()
    if not svc_company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Servicio no encontrado.")
    if not context.is_super_admin:
        if svc_company_id not in context.allowed_company_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: El servicio pertenece a otra empresa.")
        if context.allowed_service_ids is not None:
            if payload.service_id not in context.allowed_service_ids:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: No tienes permisos para este servicio.")

    try:
        return await TrainerService.create_evaluation_config(db, payload, created_by=context.user_email)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.patch("/evaluation-configs/{config_id}", response_model=TrainerEvaluationConfigResponse)
async def update_evaluation_config(
    config_id: int,
    payload: TrainerEvaluationConfigUpdate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Update an evaluation config (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    await verify_config_write_scope(db, config_id, context)
    cfg = await TrainerService.update_evaluation_config(db, config_id, payload)
    return cfg


@router.post("/evaluation-configs/{config_id}/activate", response_model=TrainerEvaluationConfigResponse)
async def activate_evaluation_config(
    config_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Activate evaluation config (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    await verify_config_write_scope(db, config_id, context)
    cfg = await TrainerService.update_evaluation_config(
        db, config_id, TrainerEvaluationConfigUpdate(is_active=True)
    )
    return cfg


@router.post("/evaluation-configs/{config_id}/deactivate", response_model=TrainerEvaluationConfigResponse)
async def deactivate_evaluation_config(
    config_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Deactivate evaluation config (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    await verify_config_write_scope(db, config_id, context)
    cfg = await TrainerService.update_evaluation_config(
        db, config_id, TrainerEvaluationConfigUpdate(is_active=False)
    )
    return cfg


@router.get("/services/{service_id}/available-evaluation-structures", response_model=List[AvailableSpeechStructure])
async def list_available_structures(
    service_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    include_inactive: bool = Query(False, description="Incluir estructuras de evaluación inactivas"),
    include_archived: bool = Query(False, description="Incluir estructuras de evaluación archivadas"),
    db: AsyncSession = Depends(get_db),
):
    """List available Speech prompts/structures for a service."""
    # Resolve company_id of the service
    from app.models.services import Service
    stmt_svc = select(Service.company_id).where(Service.service_id == service_id)
    res_svc = await db.execute(stmt_svc)
    svc_company_id = res_svc.scalar()
    if not svc_company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Servicio no encontrado.")
    if not context.is_super_admin:
        if svc_company_id not in context.allowed_company_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: El servicio pertenece a otra empresa.")
        if context.allowed_service_ids is not None:
            if service_id not in context.allowed_service_ids:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso denegado: No tienes permisos para este servicio.")

    try:
        return await TrainerService.list_available_structures(
            db,
            service_id=service_id,
            include_inactive=include_inactive,
            include_archived=include_archived,
        )
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ve))


# ── AI Prompt Generation Endpoints ────────────────────────────────────────────

@router.post("/ai/generate-roleplay-prompt")
async def generate_roleplay_prompt(
    payload: AIPromptGenerateRequest,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """AI Assistant to generate a new roleplay prompt from objective/ideas (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )
    try:
        prompt_text = await TrainerService.generate_roleplay_prompt_ai(payload)
        return {"roleplay_prompt": prompt_text}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"AI generation failed: {str(e)}")


@router.post("/ai/improve-roleplay-prompt")
async def improve_roleplay_prompt(
    payload: AIPromptImproveRequest,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """AI Assistant to improve/refine an existing roleplay prompt (Admin/Manager only)."""
    if context.normalized_role in [InternalRole.AGENT, InternalRole.TEAM_COORDINATOR]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )
    try:
        prompt_text = await TrainerService.improve_roleplay_prompt_ai(payload)
        return {"roleplay_prompt": prompt_text}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"AI refinement failed: {str(e)}")


# ── Sessions Endpoints ────────────────────────────────────────────────────────

@router.get("/sessions", response_model=TrainerSessionList)
async def list_sessions(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    agent_id: Optional[str] = Query(None),
    service_id: Optional[int] = Query(None),
    simulation_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    evaluation_status: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    min_score: Optional[Decimal] = Query(None),
    max_score: Optional[Decimal] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """List sessions matching filters (Agents/Coordinators see only their scope)."""
    if context.normalized_role == InternalRole.AGENT and not context.allowed_agent_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID."
        )

    # Validate agent query parameter
    if agent_id is not None:
        if not context.is_super_admin and context.allowed_agent_ids is not None:
            if agent_id not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para ver el entrenamiento de otros agentes."
                )

    # Validate service query parameter
    if service_id is not None:
        if not context.is_super_admin:
            if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: No tienes permisos para este servicio."
                )
            
            # Resolve company_id of the service
            from app.models.services import Service
            stmt_svc = select(Service.company_id).where(Service.service_id == service_id)
            res_svc = await db.execute(stmt_svc)
            svc_company_id = res_svc.scalar()
            if svc_company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: El servicio pertenece a otra empresa."
                )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_service_ids = context.allowed_service_ids if not context.is_super_admin else None
    agent_ids = context.allowed_agent_ids if not context.is_super_admin else None

    # If the user queried a specific agent_id, we pass it. If not, and they are restricted by agent_ids, we pass that.
    # Same logic: if agent_id parameter is passed, we filter only by it.
    actual_agent_id = agent_id
    actual_agent_ids = agent_ids if agent_id is None else None

    sessions, total = await TrainerService.list_sessions(
        db,
        agent_id=actual_agent_id,
        service_id=service_id,
        simulation_id=simulation_id,
        status=status,
        evaluation_status=evaluation_status,
        date_from=date_from,
        date_to=date_to,
        min_score=min_score,
        max_score=max_score,
        limit=limit,
        company_ids=company_ids,
        allowed_service_ids=allowed_service_ids,
        agent_ids=actual_agent_ids,
    )
    for s in sessions:
        if s.recording_url:
            s.recording_url = f"/bm/trainer/sessions/{s.session_id}/recording-audio"
    return {"sessions": sessions, "total_count": total}


@router.get("/sessions/{session_id}", response_model=TrainerSessionResponse)
async def get_session_detail(
    session_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Get detailed training session with evaluation results."""
    sess = await verify_session_read_scope(db, session_id, context)
    if sess.recording_url:
        sess.recording_url = f"/bm/trainer/sessions/{sess.session_id}/recording-audio"
    return sess


@router.get("/sessions/{session_id}/recording-audio")
async def get_session_recording_audio(
    session_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Proxy endpoint to stream/download Twilio recordings securely without client auth prompts."""
    await verify_session_read_scope(db, session_id, context)

    # Check database recording_url
    from sqlalchemy import select
    from app.models.trainer import TrainerSession
    stmt = select(TrainerSession.recording_url).where(TrainerSession.session_id == session_id)
    res = await db.execute(stmt)
    raw_recording_url = res.scalar()

    if not raw_recording_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Grabación no disponible todavía"
        )

    import httpx
    from fastapi import Response

    try:
        audio_bytes = await TrainerService.download_trainer_recording_audio(raw_recording_url)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="No se pudo recuperar la grabación desde Twilio"
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error al descargar la grabación desde Twilio: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo recuperar la grabación: {str(e)}"
        )

    return Response(
        content=audio_bytes,
        media_type="audio/mpeg",
        headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"inline; filename=session_{session_id}.mp3"
        }
    )
