"""FastAPI router for personalized agent training, settings, admin overview, reports and simulations."""
import logging
from typing import Annotated, List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, get_tenant_context
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
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
    TrainingSchedulerSettingOut,
    TrainingSchedulerSettingPatch,
    CyclesTeamSummaryResponse,
    UpdateCycleObjectivesPayload,
    ApproveCycleResponse,
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


def sanitize_report_for_agent(report: dict) -> dict:
    """Removes sensitive prompt_text instructions from simulation prompts for agents."""
    if not report:
        return report
    
    if "prompts" in report and isinstance(report["prompts"], list):
        for p in report["prompts"]:
            if isinstance(p, dict):
                p["prompt_text"] = ""
    return report


# ── Admin Endpoints ──────────────────────────────────────────────────────────

@router.get("/admin/settings", response_model=List[TrainingAgentSettingOut])
async def list_agent_settings(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """List all personalized training settings for agents (Admin/Company Admin/Service Manager/Team Coordinator)."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_agent_ids = None
    if context.normalized_role in [InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR]:
        allowed_agent_ids = context.allowed_agent_ids or []

    return await PersonalizedTrainingService.get_agent_settings(
        db,
        company_ids=company_ids,
        allowed_agent_ids=allowed_agent_ids
    )


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
            agent_initials=payload.agent_initials,
            training_code=payload.training_code,
            training_numeric_code=payload.training_numeric_code,
            training_code_enabled=payload.training_code_enabled
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
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Overview list of all active agents and their current training statuses."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_agent_ids = None
    if context.normalized_role in [InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR]:
        allowed_agent_ids = context.allowed_agent_ids or []

    return await PersonalizedTrainingService.get_agent_overview(
        db,
        company_ids=company_ids,
        allowed_agent_ids=allowed_agent_ids
    )


@router.get("/admin/cycles-summary", response_model=CyclesTeamSummaryResponse)
async def get_team_cycles_summary(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Get team-wide training metrics, aggregates and priority targets."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_agent_ids = None
    if context.normalized_role in [InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR]:
        allowed_agent_ids = context.allowed_agent_ids or []

    return await PersonalizedTrainingService.get_cycles_team_summary(
        db,
        company_ids=company_ids,
        allowed_agent_ids=allowed_agent_ids
    )


@router.get("/admin/scheduler-settings", response_model=TrainingSchedulerSettingOut)
async def get_scheduler_settings(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the persistent personalized training scheduler configuration (Super Admin only)."""
    if not context.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Super Administrador para ver los ajustes globales del scheduler."
        )
    
    from app.config import get_settings
    settings = get_settings()
    
    db_settings = await PersonalizedTrainingService.get_or_create_scheduler_settings(db)
    
    # Inject runtime override status dynamically
    runtime_enabled = settings.enable_training_scheduler
    
    return TrainingSchedulerSettingOut(
        is_enabled=db_settings.is_enabled if runtime_enabled else False,
        interval_days=db_settings.interval_days,
        lookback_days=db_settings.lookback_days,
        last_run_at=db_settings.last_run_at,
        next_run_at=db_settings.next_run_at if runtime_enabled else None,
        last_status=db_settings.last_status,
        updated_at=db_settings.updated_at,
        runtime_enabled=runtime_enabled,
        reason=None if runtime_enabled else "Scheduler deshabilitado por variable de entorno"
    )


@router.patch("/admin/scheduler-settings", response_model=TrainingSchedulerSettingOut)
async def update_scheduler_settings(
    payload: TrainingSchedulerSettingPatch,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Modify the persistent personalized training scheduler configuration (Admin only)."""
    enforce_admin_role(current_user)
    
    from app.config import get_settings
    settings = get_settings()
    
    db_settings = await PersonalizedTrainingService.update_scheduler_settings(
        db=db,
        is_enabled=payload.is_enabled,
        interval_days=payload.interval_days,
        lookback_days=payload.lookback_days,
        updated_by_email=current_user.email
    )
    
    runtime_enabled = settings.enable_training_scheduler
    
    return TrainingSchedulerSettingOut(
        is_enabled=db_settings.is_enabled if runtime_enabled else False,
        interval_days=db_settings.interval_days,
        lookback_days=db_settings.lookback_days,
        last_run_at=db_settings.last_run_at,
        next_run_at=db_settings.next_run_at if runtime_enabled else None,
        last_status=db_settings.last_status,
        updated_at=db_settings.updated_at,
        runtime_enabled=runtime_enabled,
        reason=None if runtime_enabled else "Scheduler deshabilitado por variable de entorno"
    )


@router.get("/admin/agents/{hubspot_owner_id}", response_model=AgentDetailResponse)
async def get_agent_detail_admin(
    hubspot_owner_id: str,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db)
):
    """Full detail of an agent's training, objectives, prompts, history and progress (Admin only)."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol de administración."
        )

    # Validamos permisos sobre el hubspot_owner_id solicitado
    if context.allowed_agent_ids is not None:
        if hubspot_owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para acceder a este agente."
            )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    try:
        detail = await PersonalizedTrainingService.get_agent_detail(
            db, 
            hubspot_owner_id=hubspot_owner_id,
            include_archived=include_archived,
            include_pending_approval=True,
            company_ids=company_ids
        )
        if not detail:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agente {hubspot_owner_id} no encontrado en configuraciones o sin acceso."
            )
        # Manually validate with Pydantic to catch serialization errors before returning
        AgentDetailResponse.model_validate(detail)
        return detail
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed GET detail for agent %s: %s", hubspot_owner_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_message": "Fallo de serialización en el endpoint de detalle.",
                "exception_type": type(e).__name__,
                "exception_message": str(e)
            }
        )


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
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the current training report, prompts and progress for the authenticated agent."""
    hubspot_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
    if not hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
        )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    detail = await PersonalizedTrainingService.get_agent_detail(
        db,
        hubspot_owner_id=hubspot_owner_id,
        company_ids=company_ids
    )
    if not detail or not detail.get("current_report"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontró ningún informe de entrenamiento actual disponible para tu usuario."
        )
    return sanitize_report_for_agent(detail["current_report"])


@router.get("/me/history", response_model=List[TrainingAgentReportBase])
async def get_my_training_history(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the list of historical training reports for the authenticated agent."""
    hubspot_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
    if not hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
        )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    detail = await PersonalizedTrainingService.get_agent_detail(
        db,
        hubspot_owner_id=hubspot_owner_id,
        include_archived=include_archived,
        company_ids=company_ids
    )
    if not detail:
        return []
    return detail["history"]


@router.get("/me/reports/{training_report_id}", response_model=TrainingAgentReportOut)
async def get_my_historical_report(
    training_report_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a specific historical training report for the authenticated agent."""
    hubspot_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
    if not hubspot_owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID de agente."
        )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    report_details = await PersonalizedTrainingService.get_report_by_id(
        db,
        report_id=training_report_id,
        company_ids=company_ids,
        allowed_agent_ids=[hubspot_owner_id]
    )
    if not report_details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado o sin acceso."
        )

    return sanitize_report_for_agent(report_details)


# ── Generic/Agent Parameterized Endpoints ─────────────────────────────────────

@router.get("/agents/{hubspot_owner_id}/current", response_model=TrainingAgentReportOut)
async def get_agent_current_training(
    hubspot_owner_id: str,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve the current training report, prompts and progress for a specific agent."""
    if context.allowed_agent_ids is not None:
        if hubspot_owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para ver el entrenamiento de otros agentes."
            )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    detail = await PersonalizedTrainingService.get_agent_detail(
        db,
        hubspot_owner_id=hubspot_owner_id,
        company_ids=company_ids
    )
    if not detail or not detail.get("current_report"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró ningún informe de entrenamiento actual para el agente {hubspot_owner_id}."
        )
    report_data = detail["current_report"]
    if context.normalized_role == InternalRole.AGENT:
        report_data = sanitize_report_for_agent(report_data)
    return report_data


@router.get("/agents/{hubspot_owner_id}/history", response_model=List[TrainingAgentReportBase])
async def get_agent_training_history(
    hubspot_owner_id: str,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve all historical training reports for a specific agent."""
    if context.allowed_agent_ids is not None:
        if hubspot_owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para ver el entrenamiento de otros agentes."
            )

    company_ids = context.allowed_company_ids if not context.is_super_admin else None

    detail = await PersonalizedTrainingService.get_agent_detail(
        db,
        hubspot_owner_id=hubspot_owner_id,
        include_archived=include_archived,
        company_ids=company_ids
    )
    if not detail:
        return []
    return detail["history"]


@router.get("/reports/{training_report_id}", response_model=TrainingAgentReportOut)
async def get_report_by_id(
    training_report_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve detail of a specific training report by ID."""
    company_ids = context.allowed_company_ids if not context.is_super_admin else None
    allowed_agent_ids = context.allowed_agent_ids if not context.is_super_admin else None

    report_details = await PersonalizedTrainingService.get_report_by_id(
        db,
        report_id=training_report_id,
        company_ids=company_ids,
        allowed_agent_ids=allowed_agent_ids
    )
    if not report_details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado o sin acceso."
        )

    if context.normalized_role == InternalRole.AGENT:
        report_details = sanitize_report_for_agent(report_details)
    return report_details


@router.post("/admin/reports/{training_report_id}/archive", response_model=TrainingAgentReportOut)
async def archive_training_report(
    training_report_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Soft-delete / archive a training report so it no longer counts in active/pending stats (Admin only)."""
    enforce_admin_role(current_user)
    report = await PersonalizedTrainingService.archive_report(db, report_id=training_report_id)
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado."
        )
    return report


@router.patch("/admin/reports/{training_report_id}/objectives", response_model=TrainingAgentReportOut)
async def update_cycle_objectives(
    training_report_id: int,
    payload: UpdateCycleObjectivesPayload,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Edit general and/or specific objectives of a training cycle in 'pending_approval' status (Admin only).
    
    Validates that specific objectives are qualitative and do not contain percentage-based phrasing.
    Only cycles in 'pending_approval' status can be edited.
    """
    enforce_admin_role(current_user)
    try:
        report = await PersonalizedTrainingService.update_cycle_objectives(
            db=db,
            report_id=training_report_id,
            general_objectives_json=payload.general_objectives_json,
            specific_objectives_json=payload.specific_objectives_json,
        )
        report_dict = await PersonalizedTrainingService.get_report_by_id(db, report_id=training_report_id)
        if not report_dict:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Informe no encontrado tras actualizar.")
        return report_dict
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update objectives for report %d: %s", training_report_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al actualizar los objetivos: {str(e)}"
        )


@router.post("/admin/reports/{training_report_id}/approve", response_model=ApproveCycleResponse)
async def approve_training_cycle(
    training_report_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Approve a training cycle in 'pending_approval' status (Admin only).
    
    This endpoint:
    - Validates objectives are qualitative (no percentages).
    - Generates the 4 simulation prompts and completion status records.
    - Deactivates any previous in_progress cycle for the same agent.
    - Transitions the cycle to 'in_progress', making it visible to the agent.
    
    Idempotent: re-approving an already in_progress cycle returns the current state.
    """
    enforce_admin_role(current_user)
    try:
        report = await PersonalizedTrainingService.approve_training_cycle(
            db=db,
            report_id=training_report_id,
            approved_by_user_id=current_user.user_id,
        )
        # Count prompts created
        from sqlalchemy import func as sqlfunc
        from app.models.personalized_training import TrainingSimulationPrompt
        stmt_count = select(sqlfunc.count()).where(
            TrainingSimulationPrompt.training_report_id == training_report_id
        )
        res_count = await db.execute(stmt_count)
        prompts_count = res_count.scalar() or 0

        return ApproveCycleResponse(
            training_report_id=report.training_report_id,
            status=report.status,
            approved_at=report.approved_at,
            approved_by_user_id=report.approved_by_user_id,
            prompts_generated=prompts_count,
            message=(
                f"Ciclo ID {training_report_id} aprobado correctamente. "
                f"{prompts_count} prompts de simulación generados. "
                f"El ciclo es ahora visible para el agente."
            )
        )
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to approve cycle %d: %s", training_report_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al aprobar el ciclo: {str(e)}"
        )


@router.delete("/admin/reports/{training_report_id}/hard-delete")
async def hard_delete_training_report(
    training_report_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Hard-delete a training report and all its prompts and completions from the database (Admin only)."""
    enforce_admin_role(current_user)
    success = await PersonalizedTrainingService.hard_delete_report(db, report_id=training_report_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Informe de entrenamiento ID {training_report_id} no encontrado."
        )
    return {"message": f"Informe de entrenamiento ID {training_report_id} eliminado físicamente con éxito."}


@router.get("/admin/evaluations/{evaluation_id}")
async def get_evaluation_detail(
    evaluation_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the full detail of a training call evaluation, including score,
    feedback, transcription (formatted as conversation turns), and the
    result_json criteria checklist.
    Accessible by authorized administrators, managers, coordinators, and the owner agent.
    """
    from app.models.personalized_training import TrainingCallEvaluation, TrainingCallSession

    stmt = select(TrainingCallEvaluation).where(TrainingCallEvaluation.evaluation_id == evaluation_id)
    res = await db.execute(stmt)
    ev = res.scalars().first()

    if not ev:
        raise HTTPException(status_code=404, detail=f"Evaluación {evaluation_id} no encontrada.")

    # Ownership check: admin can see all; agents can only see their own
    if not context.is_super_admin:
        stmt_sess = select(TrainingCallSession).where(TrainingCallSession.session_id == ev.session_id)
        res_sess = await db.execute(stmt_sess)
        session = res_sess.scalars().first()
        if not session:
            raise HTTPException(status_code=403, detail="No tienes acceso a esta evaluación.")
        
        if context.allowed_agent_ids is not None:
            if session.agent_id not in context.allowed_agent_ids:
                raise HTTPException(status_code=403, detail="No tienes acceso a esta evaluación.")

        if context.allowed_company_ids:
            # Check company
            stmt_rep = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == session.cycle_id)
            res_rep = await db.execute(stmt_rep)
            rep = res_rep.scalars().first()
            if not rep or rep.company_id not in context.allowed_company_ids:
                raise HTTPException(status_code=403, detail="No tienes acceso a esta evaluación.")

    # Parse transcription into conversation turns for easy rendering
    turns = []
    if ev.transcription:
        for line in ev.transcription.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("Agente:"):
                turns.append({"role": "agent", "text": line[len("Agente:"):].strip()})
            elif line.startswith("Paciente:"):
                turns.append({"role": "patient", "text": line[len("Paciente:"):].strip()})
            else:
                turns.append({"role": "unknown", "text": line})

    # Extract criteria checklist from result_json (handles nested structure)
    criteria = {}
    if ev.result_json:
        raw = ev.result_json
        # Handle nested: { ..., result_json: { key: bool } }
        inner = raw.get("result_json") if isinstance(raw, dict) else None
        if isinstance(inner, dict):
            criteria = inner
        elif isinstance(raw, dict):
            # Flat structure — pick only boolean values as criteria
            criteria = {k: v for k, v in raw.items() if isinstance(v, bool)}

    return {
        "evaluation_id": ev.evaluation_id,
        "session_id": ev.session_id,
        "cycle_id": ev.cycle_id,
        "score": float(ev.score) if ev.score is not None else None,
        "feedback": ev.feedback,
        "transcription_raw": ev.transcription,
        "transcription_turns": turns,
        "criteria": criteria,
        "result_json": ev.result_json,
        "created_at": ev.created_at,
    }

