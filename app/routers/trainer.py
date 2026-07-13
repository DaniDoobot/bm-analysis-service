"""FastAPI APIRouter for Trainer simulations, evaluation configs, sessions, and AI prompt assistance."""
import logging
from typing import Annotated, List, Optional
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user
from app.models.users import User
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
    """Enforce that the logged-in user is an administrator."""
    if user.role not in ["admin", "administrador"]:
        logger.warning("Access denied: User ID %s does not have administrator role.", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol de administrador para realizar esta operación."
        )


def enforce_agent_or_admin_ownership(user: User, hubspot_owner_id: str):
    """Enforce that a user can only access their own data unless they are an admin."""
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


# ── Simulations Endpoints ─────────────────────────────────────────────────────

@router.get("/simulations", response_model=List[TrainerSimulationResponse])
async def list_simulations(
    current_user: Annotated[User, Depends(get_current_user)],
    service_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    code: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List all simulations with optional filtering."""
    return await TrainerService.list_simulations(
        db, service_id=service_id, status=status, search=search, code=code
    )


@router.get("/simulations/{simulation_id}", response_model=TrainerSimulationResponse)
async def get_simulation(
    simulation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Get details of a single simulation."""
    sim = await TrainerService.get_simulation(db, simulation_id)
    if not sim:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulación no encontrada.")
    return sim


@router.post("/simulations", response_model=TrainerSimulationResponse, status_code=status.HTTP_201_CREATED)
async def create_simulation(
    payload: TrainerSimulationCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Create a new simulation in draft state (Admin only)."""
    enforce_admin_role(current_user)
    try:
        return await TrainerService.create_simulation(db, payload, created_by=current_user.email)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.patch("/simulations/{simulation_id}", response_model=TrainerSimulationResponse)
async def update_simulation(
    simulation_id: int,
    payload: TrainerSimulationUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Update an existing simulation (Admin only)."""
    enforce_admin_role(current_user)
    try:
        sim = await TrainerService.update_simulation(db, simulation_id, payload, updated_by=current_user.email)
        if not sim:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulación no encontrada.")
        return sim
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.post("/simulations/{simulation_id}/publish", response_model=TrainerSimulationResponse)
async def publish_simulation(
    simulation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Publish a simulation making it active for voice calls (Admin only)."""
    enforce_admin_role(current_user)
    try:
        return await TrainerService.publish_simulation(db, simulation_id, user_email=current_user.email)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.post("/simulations/{simulation_id}/archive", response_model=TrainerSimulationResponse)
async def archive_simulation(
    simulation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Archive a simulation to hide/disable it (Admin only)."""
    enforce_admin_role(current_user)
    sim = await TrainerService.archive_simulation(db, simulation_id)
    if not sim:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulación no encontrada.")
    return sim


@router.post("/simulations/{simulation_id}/duplicate", response_model=TrainerSimulationResponse)
async def duplicate_simulation(
    simulation_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Duplicate an existing simulation as a new draft (Admin only)."""
    enforce_admin_role(current_user)
    try:
        return await TrainerService.duplicate_simulation(db, simulation_id, user_email=current_user.email)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


# ── Evaluation Configs Endpoints ──────────────────────────────────────────────

@router.get("/evaluation-configs", response_model=List[TrainerEvaluationConfigResponse])
async def list_evaluation_configs(
    current_user: Annotated[User, Depends(get_current_user)],
    service_id: Optional[int] = Query(None),
    is_active: Optional[bool] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List all evaluation configs."""
    return await TrainerService.list_evaluation_configs(db, service_id=service_id, is_active=is_active)


@router.get("/evaluation-configs/{config_id}", response_model=TrainerEvaluationConfigResponse)
async def get_evaluation_config(
    config_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Get single evaluation config."""
    cfg = await TrainerService.get_evaluation_config(db, config_id)
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuración de evaluación no encontrada.")
    return cfg


@router.post("/evaluation-configs", response_model=TrainerEvaluationConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_evaluation_config(
    payload: TrainerEvaluationConfigCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Create a new evaluation config (Admin only)."""
    enforce_admin_role(current_user)
    try:
        return await TrainerService.create_evaluation_config(db, payload, created_by=current_user.email)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.patch("/evaluation-configs/{config_id}", response_model=TrainerEvaluationConfigResponse)
async def update_evaluation_config(
    config_id: int,
    payload: TrainerEvaluationConfigUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Update an evaluation config (Admin only)."""
    enforce_admin_role(current_user)
    cfg = await TrainerService.update_evaluation_config(db, config_id, payload)
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuración no encontrada.")
    return cfg


@router.post("/evaluation-configs/{config_id}/activate", response_model=TrainerEvaluationConfigResponse)
async def activate_evaluation_config(
    config_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Activate evaluation config (Admin only)."""
    enforce_admin_role(current_user)
    cfg = await TrainerService.update_evaluation_config(
        db, config_id, TrainerEvaluationConfigUpdate(is_active=True)
    )
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuración no encontrada.")
    return cfg


@router.post("/evaluation-configs/{config_id}/deactivate", response_model=TrainerEvaluationConfigResponse)
async def deactivate_evaluation_config(
    config_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Deactivate evaluation config (Admin only)."""
    enforce_admin_role(current_user)
    cfg = await TrainerService.update_evaluation_config(
        db, config_id, TrainerEvaluationConfigUpdate(is_active=False)
    )
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuración no encontrada.")
    return cfg


@router.get("/services/{service_id}/available-evaluation-structures", response_model=List[AvailableSpeechStructure])
async def list_available_structures(
    service_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    include_inactive: bool = Query(False, description="Incluir estructuras de evaluación inactivas"),
    include_archived: bool = Query(False, description="Incluir estructuras de evaluación archivadas"),
    db: AsyncSession = Depends(get_db),
):
    """List available Speech prompts/structures for a service."""
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
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """AI Assistant to generate a new roleplay prompt from objective/ideas (Admin only)."""
    enforce_admin_role(current_user)
    try:
        prompt_text = await TrainerService.generate_roleplay_prompt_ai(payload)
        return {"roleplay_prompt": prompt_text}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"AI generation failed: {str(e)}")


@router.post("/ai/improve-roleplay-prompt")
async def improve_roleplay_prompt(
    payload: AIPromptImproveRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """AI Assistant to improve/refine an existing roleplay prompt (Admin only)."""
    enforce_admin_role(current_user)
    try:
        prompt_text = await TrainerService.improve_roleplay_prompt_ai(payload)
        return {"roleplay_prompt": prompt_text}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"AI refinement failed: {str(e)}")


# ── Sessions Endpoints ────────────────────────────────────────────────────────

@router.get("/sessions", response_model=TrainerSessionList)
async def list_sessions(
    current_user: Annotated[User, Depends(get_current_user)],
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
    """List sessions matching filters (Agents see only their own sessions)."""
    # Enforce agent boundary
    if current_user.role not in ["admin", "administrador"]:
        if not current_user.hubspot_owner_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID."
            )
        agent_id = current_user.hubspot_owner_id

    sessions, total = await TrainerService.list_sessions(
        db,
        agent_id=agent_id,
        service_id=service_id,
        simulation_id=simulation_id,
        status=status,
        evaluation_status=evaluation_status,
        date_from=date_from,
        date_to=date_to,
        min_score=min_score,
        max_score=max_score,
        limit=limit,
    )
    return {"sessions": sessions, "total_count": total}


@router.get("/sessions/{session_id}", response_model=TrainerSessionResponse)
async def get_session_detail(
    session_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Get detailed training session with evaluation results."""
    sess = await TrainerService.get_session_detail(db, session_id)
    if not sess:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sesión no encontrada.")

    # Enforce ownership check for non-admin agents
    enforce_agent_or_admin_ownership(current_user, sess.agent_id)
    return sess
