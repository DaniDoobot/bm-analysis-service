"""API router for automated mass evaluations."""
from datetime import datetime
from typing import Any, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, get_tenant_context
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.users import User
from app.models.prompts import Prompt
from app.models.services import Service
from app.utils.hubspot_owners import resolve_owner_id_by_email
from app.schemas.mass_evaluations import (
    MassEvaluationJobCreate,
    MassEvaluationJobManualRunRequest,
    MassEvaluationJobResponse,
    MassEvaluationJobUpdate,
    MassEvaluationResultResponse,
    MassEvaluationRunResponse,
    MassEvaluationRunLaunchResponse,
    MassCriterionTypologyBackfillRequest,
    MassAnalysisAutomationCreate,
    MassAnalysisAutomationUpdate,
    MassAnalysisAutomationResponse,
    MassAnalysisAutomationRunResponse,
    PagedMassEvaluationResultResponse,
)
from app.services.mass_evaluation_service import MassEvaluationService

router = APIRouter(prefix="/bm", tags=["Mass Evaluations"])


# ── Jobs Endpoints ────────────────────────────────────────────────────────────

@router.get("/mass-evaluation-jobs", response_model=list[MassEvaluationJobResponse])
async def list_jobs(
    limit: int = Query(100, ge=1, le=1000),
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """List all active mass evaluation jobs."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para ver jobs masivos."
        )
    return await MassEvaluationService.list_jobs(
        db,
        limit=limit,
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids
    )


@router.get("/mass-evaluation-jobs/{job_id}", response_model=MassEvaluationJobResponse)
async def get_job(
    job_id: int,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a single mass evaluation job."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para acceder a este job."
        )
    job = await MassEvaluationService.get_job(db, job_id=job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job ID {job_id} not found."
        )
    if not context.is_super_admin:
        if job.company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este job pertenece a otra empresa."
            )
        if context.allowed_service_ids is not None and job.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este job pertenece a un servicio no asignado."
            )
    return job


@router.post("/mass-evaluation-jobs", response_model=MassEvaluationJobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: MassEvaluationJobCreate,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Create a new mass evaluation job."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para crear jobs masivos."
        )

    target_company_id: int | None = None
    target_service_id: int | None = None

    if payload.prompt_id is not None:
        stmt = select(Prompt).where(Prompt.prompt_id == payload.prompt_id)
        res = await db.execute(stmt)
        prompt = res.scalars().first()
        if not prompt or prompt.is_archived or prompt.deleted_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La estructura seleccionada no existe o está archivada."
            )

        prompt_service_id = prompt.service_id
        prompt_company_id = prompt.company_id

        if prompt_company_id is None and prompt_service_id is not None:
            s_res = await db.execute(select(Service.company_id).where(Service.service_id == prompt_service_id))
            company_from_service = s_res.scalar()
            if company_from_service is not None:
                prompt_company_id = company_from_service
                prompt.company_id = prompt_company_id
                db.add(prompt)
                await db.flush()

        if prompt_company_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La estructura seleccionada no tiene empresa asociada."
            )

        if prompt_service_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se pudo determinar el servicio para el job."
            )

        # Rule 2: If payload also sends company_id or service_id, validate match
        if payload.service_id is not None and payload.service_id != prompt_service_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La empresa/servicio no coincide con la estructura específica seleccionada."
            )
        if payload.company_id is not None and payload.company_id != prompt_company_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La empresa/servicio no coincide con la estructura específica seleccionada."
            )

        target_company_id = prompt_company_id
        target_service_id = prompt_service_id

        # Rules 4, 5, 6: Scope check based on prompt's derived scope
        if not context.is_super_admin:
            if prompt_company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: la estructura seleccionada pertenece a otra empresa."
                )
            if context.allowed_service_ids is not None and prompt_service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: la estructura seleccionada pertenece a un servicio no asignado."
                )
    else:
        target_company_id = payload.company_id or context.company_id
        target_service_id = payload.service_id
        if not target_company_id or not target_service_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debe seleccionar una estructura específica o especificar empresa y servicio."
            )
        if not context.is_super_admin:
            if target_company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: la empresa especificada no está autorizada."
                )
            if context.allowed_service_ids is not None and target_service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: el servicio especificado no está asignado."
                )

    try:
        return await MassEvaluationService.create_job(
            db,
            payload=payload,
            company_id=target_company_id,
            service_id=target_service_id
        )
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create job: {str(e)}"
        )


@router.put("/mass-evaluation-jobs/{job_id}", response_model=MassEvaluationJobResponse)
async def update_job(
    job_id: int,
    payload: MassEvaluationJobUpdate,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Update an existing mass evaluation job."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para modificar jobs masivos."
        )
    job = await MassEvaluationService.get_job(db, job_id=job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job ID {job_id} not found."
        )
    if not context.is_super_admin:
        if job.company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este job pertenece a otra empresa."
            )
        if context.allowed_service_ids is not None and job.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este job pertenece a un servicio no asignado."
            )

    if payload.prompt_id is not None:
        stmt = select(Prompt).where(Prompt.prompt_id == payload.prompt_id)
        res = await db.execute(stmt)
        new_prompt = res.scalars().first()
        if not new_prompt or new_prompt.is_archived or new_prompt.deleted_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La estructura seleccionada no existe o está archivada."
            )

        p_serv_id = new_prompt.service_id
        p_comp_id = new_prompt.company_id
        if p_comp_id is None and p_serv_id is not None:
            s_res = await db.execute(select(Service.company_id).where(Service.service_id == p_serv_id))
            comp_from_service = s_res.scalar()
            if comp_from_service is not None:
                p_comp_id = comp_from_service
                new_prompt.company_id = p_comp_id
                db.add(new_prompt)
                await db.flush()

        if p_comp_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La estructura seleccionada no tiene empresa asociada."
            )
        if p_serv_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se pudo determinar el servicio para el job."
            )

        if payload.service_id is not None and payload.service_id != p_serv_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La empresa/servicio no coincide con la estructura específica seleccionada."
            )
        if payload.company_id is not None and payload.company_id != p_comp_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La empresa/servicio no coincide con la estructura específica seleccionada."
            )

        if not context.is_super_admin:
            if p_comp_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: el nuevo prompt pertenece a otra empresa."
                )
            if context.allowed_service_ids is not None and p_serv_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: el nuevo prompt pertenece a un servicio no asignado."
                )

    try:
        updated_job = await MassEvaluationService.update_job(db, job_id=job_id, payload=payload)
        return updated_job
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update job: {str(e)}"
        )


@router.delete("/mass-evaluation-jobs/{job_id}")
async def delete_job(
    job_id: int,
    soft_delete: bool = Query(True),
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Soft delete (deactivate) or hard delete a mass evaluation job."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para borrar jobs masivos."
        )
    job = await MassEvaluationService.get_job(db, job_id=job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job ID {job_id} not found."
        )
    if not context.is_super_admin:
        if job.company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este job pertenece a otra empresa."
            )
        if context.allowed_service_ids is not None and job.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este job pertenece a un servicio no asignado."
            )
    success = await MassEvaluationService.delete_job(db, job_id=job_id, soft_delete=soft_delete)
    return {"ok": True, "message": f"Job {job_id} deleted successfully."}


@router.post("/mass-evaluation-jobs/run-due")
async def run_due_jobs_endpoint(
    db: AsyncSession = Depends(get_db)
):
    """
    Manually check for and trigger all scheduled mass evaluation jobs that are due.
    Returns the count of due jobs found and how many were successfully launched.
    """
    try:
        stats = await MassEvaluationService.run_due_jobs(db)
        return {"ok": True, **stats}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to execute due jobs scheduler pass: {str(e)}"
        )


@router.post("/mass-evaluation-jobs/{job_id}/run")
async def run_job(
    job_id: int,
    payload: MassEvaluationJobManualRunRequest = MassEvaluationJobManualRunRequest(),
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """
    Trigger immediate execution of a mass evaluation job.
    Supports dry run mode to inspect HubSpot calls found without launching analysis.
    """
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para ejecutar jobs masivos."
        )
    job = await MassEvaluationService.get_job(db, job_id=job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job ID {job_id} not found."
        )
    if not context.is_super_admin:
        if job.company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este job pertenece a otra empresa."
            )
        if context.allowed_service_ids is not None and job.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este job pertenece a un servicio no asignado."
            )

    if payload.dry_run:
        try:
            return await MassEvaluationService.dry_run_job(
                db,
                job_id=job_id,
                override_date_from=payload.override_date_from,
                override_date_to=payload.override_date_to
            )
        except ValueError as ve:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(ve)
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Dry run failed: {str(e)}"
            )
    else:
        try:
            run = await MassEvaluationService.run_job(
                db,
                job_id=job_id,
                trigger_type=payload.trigger_type,
                override_date_from=payload.override_date_from,
                override_date_to=payload.override_date_to
            )
            return {
                "message": "Run started",
                "polling_url": f"/bm/mass-evaluation-runs/{run.run_id}",
                "run": run
            }
        except ValueError as ve:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(ve)
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to launch job execution: {str(e)}"
            )


# ── Runs Endpoints ────────────────────────────────────────────────────────────

@router.get("/mass-evaluation-runs", response_model=list[MassEvaluationRunResponse])
async def list_runs(
    job_id: int | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """List mass evaluation executions, optionally filtering by job and status."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para ver ejecuciones masivas."
        )

    if job_id is not None:
        job = await MassEvaluationService.get_job(db, job_id=job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job ID {job_id} not found."
            )
        if not context.is_super_admin:
            if job.company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: este job pertenece a otra empresa."
                )
            if context.allowed_service_ids is not None and job.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: este job pertenece a un servicio no asignado."
                )

    return await MassEvaluationService.list_runs(
        db,
        job_id=job_id,
        status=status,
        limit=limit,
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids
    )


@router.get("/mass-evaluation-runs/{run_id}", response_model=MassEvaluationRunResponse)
async def get_run(
    run_id: int,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Get details and summary stats of a single mass evaluation run."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para acceder a este run."
        )
    run = await MassEvaluationService.get_run(db, run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run ID {run_id} not found."
        )
    if not context.is_super_admin:
        if run.company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este run pertenece a otra empresa."
            )
        if context.allowed_service_ids is not None and run.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este run pertenece a un servicio no asignado."
            )
    return run


@router.post("/mass-evaluation-runs/{run_id}/cancel", response_model=MassEvaluationRunResponse)
async def cancel_run(
    run_id: int,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Cancel a running mass evaluation run cooperatively."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para cancelar ejecuciones masivas."
        )
    run = await MassEvaluationService.get_run(db, run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run ID {run_id} not found."
        )
    if not context.is_super_admin:
        if run.company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este run pertenece a otra empresa."
            )
        if context.allowed_service_ids is not None and run.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este run pertenece a un servicio no asignado."
            )
    try:
        return await MassEvaluationService.cancel_run(db, run_id=run_id)
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )


# ── Results Endpoints ─────────────────────────────────────────────────────────

def resolve_agent_owner_id(user: User) -> str | None:
    if user.hubspot_owner_id:
        return user.hubspot_owner_id
    return resolve_owner_id_by_email(user.email)


@router.get("/me/analysis-results", response_model=PagedMassEvaluationResultResponse)
async def get_my_analysis_results(
    context: TenantContext = Depends(get_tenant_context),
    run_id: int | None = Query(None),
    job_id: int | None = Query(None),
    agent_owner_id: str | None = Query(None, description="For backwards compatibility, ignored for agents"),
    call_id: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    execution_source: str | None = Query(None, description="on_demand | automation"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    global_score_min: float | None = Query(None, ge=0.0, le=10.0),
    global_score_max: float | None = Query(None, ge=0.0, le=10.0),
    service_id: int | None = Query(None, description="Filter by service ID"),
    service_key: str | None = Query(None, description="Filter by service key"),
    typology_key: str | None = Query(None, description="Filter by typology key"),
    typology_ids: str | None = Query(None, description="Comma-separated typology IDs to filter"),
    duration_min_seconds: int | None = Query(None, description="Min duration in seconds"),
    duration_max_seconds: int | None = Query(None, description="Max duration in seconds"),
    db: AsyncSession = Depends(get_db)
):
    """List detailed mass analysis call results for the logged-in agent with filters."""
    # Enforce agent scope
    if context.normalized_role == InternalRole.AGENT:
        effective_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
        if not effective_owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No hay agente asociado a este usuario."
            )
        if agent_owner_id and agent_owner_id != effective_owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para ver resultados de este agente."
            )
    else:
        effective_owner_id = agent_owner_id
        if effective_owner_id and context.allowed_agent_ids is not None:
            if effective_owner_id not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permiso para ver resultados de este agente."
                )

    if global_score_min is not None and global_score_max is not None:
        if global_score_min > global_score_max:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="global_score_min cannot be greater than global_score_max",
            )

    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

    # Validate query service_id if provided
    if service_id is not None and context.allowed_service_ids is not None:
        if service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes acceso al servicio seleccionado."
            )

    total = await MassEvaluationService.count_results(
        db,
        run_id=run_id,
        job_id=job_id,
        agent_owner_id=effective_owner_id,
        call_id=call_id,
        date_from=date_from,
        date_to=date_to,
        execution_source=execution_source,
        global_score_min=global_score_min,
        global_score_max=global_score_max,
        service_id=service_id,
        service_key=service_key,
        typology_key=typology_key,
        typology_ids=typo_ids,
        duration_min_seconds=duration_min_seconds,
        duration_max_seconds=duration_max_seconds,
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids,
        allowed_agent_ids=context.allowed_agent_ids if not effective_owner_id else None
    )

    from app.utils.visual_formatters import build_items_visual
    results = await MassEvaluationService.list_results(
        db,
        run_id=run_id,
        job_id=job_id,
        agent_owner_id=effective_owner_id,
        call_id=call_id,
        date_from=date_from,
        date_to=date_to,
        execution_source=execution_source,
        limit=limit,
        global_score_min=global_score_min,
        global_score_max=global_score_max,
        service_id=service_id,
        service_key=service_key,
        typology_key=typology_key,
        offset=offset,
        typology_ids=typo_ids,
        duration_min_seconds=duration_min_seconds,
        duration_max_seconds=duration_max_seconds,
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids,
        allowed_agent_ids=context.allowed_agent_ids if not effective_owner_id else None
    )
    
    items_out = []
    for r in results:
        d = MassEvaluationResultResponse.model_validate(r)
        d.items_visual = build_items_visual(r.items_json)
        if d.execution_source is None:
            d.execution_source = "on_demand"
        items_out.append(d)

    return PagedMassEvaluationResultResponse(
        items=items_out,
        total=total,
        limit=limit,
        offset=offset
    )


@router.get("/mass-evaluation-results", response_model=list[MassEvaluationResultResponse])
async def list_results(
    context: TenantContext = Depends(get_tenant_context),
    run_id: int | None = Query(None),
    job_id: int | None = Query(None),
    agent_owner_id: str | None = Query(None),
    call_id: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    execution_source: str | None = Query(None, description="on_demand | automation"),
    limit: int = Query(100, ge=1, le=1000),
    global_score_min: float | None = Query(None, ge=0.0, le=10.0),
    global_score_max: float | None = Query(None, ge=0.0, le=10.0),
    service_id: int | None = Query(None, description="Filter by service ID"),
    service_key: str | None = Query(None, description="Filter by service key"),
    typology_key: str | None = Query(None, description="Filter by typology key"),
    typology_ids: str | None = Query(None, description="Comma-separated typology IDs to filter"),
    duration_min_seconds: int | None = Query(None, description="Min duration in seconds"),
    duration_max_seconds: int | None = Query(None, description="Max duration in seconds"),
    db: AsyncSession = Depends(get_db)
):
    """List detailed mass analysis call results with advanced filtering options."""
    if context.normalized_role == InternalRole.AGENT:
        effective_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
        if not effective_owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No hay agente asociado a este usuario."
            )
        if agent_owner_id and agent_owner_id != effective_owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para ver resultados de este agente."
            )
    else:
        effective_owner_id = agent_owner_id
        if effective_owner_id and context.allowed_agent_ids is not None:
            if effective_owner_id not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permiso para ver resultados de este agente."
                )

    if global_score_min is not None and global_score_max is not None:
        if global_score_min > global_score_max:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="global_score_min cannot be greater than global_score_max",
            )

    # Validate query service_id if provided
    if service_id is not None and context.allowed_service_ids is not None:
        if service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes acceso al servicio seleccionado."
            )

    from app.utils.visual_formatters import build_items_visual
    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

    results = await MassEvaluationService.list_results(
        db,
        run_id=run_id,
        job_id=job_id,
        agent_owner_id=effective_owner_id,
        call_id=call_id,
        date_from=date_from,
        date_to=date_to,
        execution_source=execution_source,
        limit=limit,
        global_score_min=global_score_min,
        global_score_max=global_score_max,
        service_id=service_id,
        service_key=service_key,
        typology_key=typology_key,
        typology_ids=typo_ids,
        duration_min_seconds=duration_min_seconds,
        duration_max_seconds=duration_max_seconds,
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids,
        allowed_agent_ids=context.allowed_agent_ids if not effective_owner_id else None
    )
    
    out = []
    for r in results:
        d = MassEvaluationResultResponse.model_validate(r)
        d.items_visual = build_items_visual(r.items_json)
        if d.execution_source is None:
            d.execution_source = "on_demand"
        out.append(d)
    return out


@router.get("/mass-evaluation-results/{mass_analysis_id}", response_model=MassEvaluationResultResponse)
async def get_result(
    mass_analysis_id: int,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve full analysis result and normalized prompt snapshot elements of a call."""
    from app.utils.visual_formatters import build_items_visual
    result = await MassEvaluationService.get_result(db, mass_analysis_id=mass_analysis_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mass analysis result ID {mass_analysis_id} not found."
        )

    # Scoping validation
    if not context.is_super_admin:
        if result.company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este resultado pertenece a otra empresa."
            )
        if context.allowed_service_ids is not None and result.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este resultado pertenece a un servicio no asignado."
            )
        if context.allowed_agent_ids is not None and result.hubspot_owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: no tienes permisos sobre el agente de este resultado."
            )

    d = MassEvaluationResultResponse.model_validate(result)
    d.items_visual = build_items_visual(result.items_json)
    if d.execution_source is None:
        d.execution_source = "on_demand"
    return d



@router.post("/admin/backfill-mass-criterion-typologies", status_code=status.HTTP_200_OK)
async def backfill_mass_criterion_typologies(
    payload: MassCriterionTypologyBackfillRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Backfill typology fields in MassEvaluationCriterionResult and parent MassEvaluationResult
    for historical mass evaluation rows using the value from 'tipo_llamada' criterion.
    """
    try:
        return await MassEvaluationService.backfill_mass_criterion_typologies(db, payload=payload)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Backfill operation failed: {str(e)}"
        )


# ── Automations Endpoints ──────────────────────────────────────────────────

@router.get("/mass-analysis/automations", response_model=list[MassAnalysisAutomationResponse])
async def list_automations(
    limit: int = Query(100, ge=1, le=1000),
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """List all active automation configurations."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para gestionar automatizaciones."
        )
    return await MassEvaluationService.list_automations(
        db,
        limit=limit,
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids
    )


@router.get("/mass-analysis/automations/{automation_id}", response_model=MassAnalysisAutomationResponse)
async def get_automation(
    automation_id: int,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a single automation configuration."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para gestionar automatizaciones."
        )
    automation = await MassEvaluationService.get_automation(db, automation_id=automation_id)
    if not automation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
        )

    # Scoping validation
    if not context.is_super_admin:
        if context.allowed_service_ids is not None:
            if automation.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a un servicio no asignado."
                )
        else:
            stmt_svc = select(Service.company_id).where(Service.service_id == automation.service_id)
            res_svc = await db.execute(stmt_svc)
            svc_comp_id = res_svc.scalar()
            if svc_comp_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a otra empresa."
                )

    return automation


@router.post("/mass-analysis/automations", response_model=MassAnalysisAutomationResponse, status_code=status.HTTP_201_CREATED)
async def create_automation(
    payload: MassAnalysisAutomationCreate,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Create a new automation configuration."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para crear automatizaciones."
        )

    # Scoping validation
    if not context.is_super_admin:
        if context.allowed_service_ids is not None:
            if payload.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: servicio no asignado."
                )
        else:
            stmt_svc = select(Service.company_id).where(Service.service_id == payload.service_id)
            res_svc = await db.execute(stmt_svc)
            svc_comp_id = res_svc.scalar()
            if svc_comp_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: servicio de otra empresa."
                )

        stmt_p = select(Prompt).where(Prompt.prompt_id == payload.prompt_id)
        res_p = await db.execute(stmt_p)
        prompt = res_p.scalars().first()
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Estructura seleccionada no existe."
            )
        if prompt.company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: la estructura pertenece a otra empresa."
            )
        if context.allowed_service_ids is not None and prompt.service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: la estructura pertenece a un servicio no asignado."
            )

    try:
        return await MassEvaluationService.create_automation(db, payload=payload)
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create automation: {str(e)}"
        )


@router.patch("/mass-analysis/automations/{automation_id}", response_model=MassAnalysisAutomationResponse)
async def update_automation(
    automation_id: int,
    payload: MassAnalysisAutomationUpdate,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Update an automation configuration."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para modificar automatizaciones."
        )
    automation = await MassEvaluationService.get_automation(db, automation_id=automation_id)
    if not automation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
        )

    # Scoping validation
    if not context.is_super_admin:
        if context.allowed_service_ids is not None:
            if automation.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a un servicio no asignado."
                )
        else:
            stmt_svc = select(Service.company_id).where(Service.service_id == automation.service_id)
            res_svc = await db.execute(stmt_svc)
            svc_comp_id = res_svc.scalar()
            if svc_comp_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a otra empresa."
                )

    if payload.prompt_id is not None:
        stmt_p = select(Prompt).where(Prompt.prompt_id == payload.prompt_id)
        res_p = await db.execute(stmt_p)
        prompt = res_p.scalars().first()
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Estructura seleccionada no existe."
            )
        if not context.is_super_admin:
            if prompt.company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: la estructura pertenece a otra empresa."
                )
            if context.allowed_service_ids is not None and prompt.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: la estructura pertenece a un servicio no asignado."
                )

    updated = await MassEvaluationService.update_automation(db, automation_id=automation_id, payload=payload)
    return updated


@router.delete("/mass-analysis/automations/{automation_id}", status_code=status.HTTP_200_OK)
async def delete_automation(
    automation_id: int,
    soft: bool = True,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Deactivate or delete an automation configuration."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para borrar automatizaciones."
        )
    automation = await MassEvaluationService.get_automation(db, automation_id=automation_id)
    if not automation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
        )

    # Scoping validation
    if not context.is_super_admin:
        if context.allowed_service_ids is not None:
            if automation.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a un servicio no asignado."
                )
        else:
            stmt_svc = select(Service.company_id).where(Service.service_id == automation.service_id)
            res_svc = await db.execute(stmt_svc)
            svc_comp_id = res_svc.scalar()
            if svc_comp_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a otra empresa."
                )

    await MassEvaluationService.delete_automation(db, automation_id=automation_id, soft_delete=soft)
    return {"message": f"Automation configuration ID {automation_id} successfully deleted."}


@router.post("/mass-analysis/automations/{automation_id}/run-now", response_model=MassAnalysisAutomationRunResponse)
async def run_automation_now(
    automation_id: int,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Trigger an automation execution run immediately."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para ejecutar automatizaciones."
        )
    automation = await MassEvaluationService.get_automation(db, automation_id=automation_id)
    if not automation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
        )

    # Scoping validation
    if not context.is_super_admin:
        if context.allowed_service_ids is not None:
            if automation.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a un servicio no asignado."
                )
        else:
            stmt_svc = select(Service.company_id).where(Service.service_id == automation.service_id)
            res_svc = await db.execute(stmt_svc)
            svc_comp_id = res_svc.scalar()
            if svc_comp_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a otra empresa."
                )

    try:
        return await MassEvaluationService.run_automation_run(db, automation=automation, trigger_type="manual")
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to launch automation run: {str(e)}"
        )


@router.get("/mass-analysis/automations/{automation_id}/runs", response_model=list[MassAnalysisAutomationRunResponse])
async def list_automation_runs(
    automation_id: int,
    limit: int = Query(100, ge=1, le=1000),
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """List execution logs / runs for a given automation configuration."""
    if context.normalized_role in (InternalRole.AGENT, InternalRole.TEAM_COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para gestionar automatizaciones."
        )
    automation = await MassEvaluationService.get_automation(db, automation_id=automation_id)
    if not automation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
        )

    # Scoping validation
    if not context.is_super_admin:
        if context.allowed_service_ids is not None:
            if automation.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a un servicio no asignado."
                )
        else:
            stmt_svc = select(Service.company_id).where(Service.service_id == automation.service_id)
            res_svc = await db.execute(stmt_svc)
            svc_comp_id = res_svc.scalar()
            if svc_comp_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: esta automatización pertenece a otra empresa."
                )

    return await MassEvaluationService.list_automation_runs(db, automation_id=automation_id, limit=limit)
