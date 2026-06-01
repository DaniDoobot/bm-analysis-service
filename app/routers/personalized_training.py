"""FastAPI router for personalized agent training, settings, admin overview, reports and simulations."""
import logging
from typing import Annotated, List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user
from app.models.users import User
from app.models.personalized_training import TrainingAgentReport, TrainingAgentSetting
from app.schemas.personalized_training import (
    TrainingAgentSettingOut,
    TrainingAgentSettingUpdate,
    TrainingRunResponse,
    AgentOverviewItem,
    AgentDetailResponse,
    TrainingAgentReportOut,
    TrainingAgentReportBase,
    ManualGeneratePayload,
)
from app.services.personalized_training_service import PersonalizedTrainingService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/training", tags=["Personalized Training"])


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
    """
    Enforces that a user can only access their own data unless they are an admin.
    """
    if user.role in ["admin", "administrador"]:
        return  # Admins can see everything
    
    if not user.hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID. Contacta con administración."
        )
        
    if user.hubspot_owner_id != hubspot_owner_id:
        logger.warning(
            "Access denied: User ID %s (agent %s) tried to access agent %s data.",
            user.user_id, user.hubspot_owner_id, hubspot_owner_id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para ver el entrenamiento de otros agentes."
        )


# ── Admin Endpoints ──────────────────────────────────────────────────────────

@router.get("/admin/settings", response_model=List[TrainingAgentSettingOut])
async def list_agent_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """List all personalized training settings for agents (Admin only)."""
    enforce_admin_role(current_user)
    return await PersonalizedTrainingService.get_agent_settings(db)


@router.patch("/admin/settings/{hubspot_owner_id}", response_model=TrainingAgentSettingOut)
async def update_agent_setting(
    hubspot_owner_id: str,
    payload: TrainingAgentSettingUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Update training setting for an agent (enable/disable) (Admin only)."""
    enforce_admin_role(current_user)
    try:
        setting = await PersonalizedTrainingService.update_agent_setting(
            db=db,
            hubspot_owner_id=hubspot_owner_id,
            is_enabled=payload.is_enabled,
            agent_name=payload.agent_name,
            agent_initials=payload.agent_initials
        )
        if not setting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No se encontró configuración para el agente {hubspot_owner_id}"
            )
        return setting
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.get("/admin/agents-overview", response_model=List[AgentOverviewItem])
async def list_agents_overview(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Overview list of all active agents and their current training statuses (Admin only)."""
    enforce_admin_role(current_user)
    return await PersonalizedTrainingService.get_agent_overview(db)


@router.get("/admin/agents/{hubspot_owner_id}", response_model=AgentDetailResponse)
async def get_agent_detail_admin(
    hubspot_owner_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Full detail of an agent's training, objectives, prompts, history and progress (Admin only)."""
    enforce_admin_role(current_user)
    detail = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=hubspot_owner_id)
    if not detail:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agente {hubspot_owner_id} no encontrado en configuraciones."
        )
    return detail


@router.post("/admin/generate", response_model=TrainingRunResponse)
async def trigger_manual_generation(
    payload: ManualGeneratePayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Trigger manual personalized training generation for one or more agents (Admin only)."""
    enforce_admin_role(current_user)
    try:
        run = await PersonalizedTrainingService.run_personalized_training_pass(
            db=db,
            hubspot_owner_ids=payload.hubspot_owner_ids,
            period_start=payload.period_start,
            period_end=payload.period_end,
            triggered_by="manual",
            created_by_email=current_user.email,
            force_regenerate=payload.force_regenerate
        )
        return run
    except Exception as e:
        logger.exception("Failed manual generation pass: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo al ejecutar la generación manual: {str(e)}"
        )


# ── Agent / Self Endpoints (Me) ──────────────────────────────────────────────

@router.get("/me/current", response_model=TrainingAgentReportOut)
async def get_my_current_training(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the current training report, prompts and progress for the authenticated agent."""
    if not current_user.hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
        )
    
    detail = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=current_user.hubspot_owner_id)
    if not detail or not detail.get("current_report"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontró ningún informe de entrenamiento actual disponible para tu usuario."
        )
    return detail["current_report"]


@router.get("/me/history", response_model=List[TrainingAgentReportBase])
async def get_my_training_history(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the list of historical training reports for the authenticated agent."""
    if not current_user.hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
        )
    
    detail = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=current_user.hubspot_owner_id)
    if not detail:
        return []
    return detail["history"]


@router.get("/me/reports/{training_report_id}", response_model=TrainingAgentReportOut)
async def get_my_historical_report(
    training_report_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a specific historical training report for the authenticated agent."""
    if not current_user.hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
        )

    report_details = await PersonalizedTrainingService.get_report_by_id(db, report_id=training_report_id)
    if not report_details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado."
        )

    # Ownership check
    if current_user.role not in ["admin", "administrador"] and report_details["hubspot_owner_id"] != current_user.hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para ver el informe de entrenamiento de otro agente."
        )

    return report_details


# ── Generic/Agent Parameterized Endpoints ─────────────────────────────────────

@router.get("/agents/{hubspot_owner_id}/current", response_model=TrainingAgentReportOut)
async def get_agent_current_training(
    hubspot_owner_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the current training report, prompts and progress for a specific agent (Ownership enforced)."""
    enforce_agent_or_admin_ownership(current_user, hubspot_owner_id)
    detail = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=hubspot_owner_id)
    if not detail or not detail.get("current_report"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró ningún informe de entrenamiento actual para el agente {hubspot_owner_id}."
        )
    return detail["current_report"]


@router.get("/agents/{hubspot_owner_id}/history", response_model=List[TrainingAgentReportBase])
async def get_agent_training_history(
    hubspot_owner_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve all historical training reports for a specific agent (Ownership enforced)."""
    enforce_agent_or_admin_ownership(current_user, hubspot_owner_id)
    detail = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=hubspot_owner_id)
    if not detail:
        return []
    return detail["history"]


@router.get("/reports/{training_report_id}", response_model=TrainingAgentReportOut)
async def get_report_by_id(
    training_report_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve detail of a specific training report by ID (Ownership enforced)."""
    report_details = await PersonalizedTrainingService.get_report_by_id(db, report_id=training_report_id)
    if not report_details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado."
        )
    enforce_agent_or_admin_ownership(current_user, report_details["hubspot_owner_id"])
    return report_details
