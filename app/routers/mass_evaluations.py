"""API router for automated mass evaluations."""
from datetime import datetime
from typing import Any, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user
from app.models.users import User
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
    db: AsyncSession = Depends(get_db)
):
    """List all active mass evaluation jobs."""
    return await MassEvaluationService.list_jobs(db, limit=limit)


@router.get("/mass-evaluation-jobs/{job_id}", response_model=MassEvaluationJobResponse)
async def get_job(
    job_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a single mass evaluation job."""
    job = await MassEvaluationService.get_job(db, job_id=job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job ID {job_id} not found."
        )
    return job


@router.post("/mass-evaluation-jobs", response_model=MassEvaluationJobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: MassEvaluationJobCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new mass evaluation job."""
    try:
        return await MassEvaluationService.create_job(db, payload=payload)
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
    db: AsyncSession = Depends(get_db)
):
    """Update an existing mass evaluation job."""
    try:
        job = await MassEvaluationService.update_job(db, job_id=job_id, payload=payload)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job ID {job_id} not found."
            )
        return job
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
    db: AsyncSession = Depends(get_db)
):
    """Soft delete (deactivate) or hard delete a mass evaluation job."""
    success = await MassEvaluationService.delete_job(db, job_id=job_id, soft_delete=soft_delete)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job ID {job_id} not found."
        )
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
    db: AsyncSession = Depends(get_db)
):
    """
    Trigger immediate execution of a mass evaluation job.
    Supports dry run mode to inspect HubSpot calls found without launching analysis.
    """
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
            # Handles active execution locks or job not found
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
    db: AsyncSession = Depends(get_db)
):
    """List mass evaluation executions, optionally filtering by job and status."""
    return await MassEvaluationService.list_runs(db, job_id=job_id, status=status, limit=limit)


@router.get("/mass-evaluation-runs/{run_id}", response_model=MassEvaluationRunResponse)
async def get_run(
    run_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get details and summary stats of a single mass evaluation run."""
    run = await MassEvaluationService.get_run(db, run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run ID {run_id} not found."
        )
    return run


@router.post("/mass-evaluation-runs/{run_id}/cancel", response_model=MassEvaluationRunResponse)
async def cancel_run(
    run_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Cancel a running mass evaluation run cooperatively."""
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
    current_user: Annotated[User, Depends(get_current_user)],
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
    db: AsyncSession = Depends(get_db)
):
    """List detailed mass analysis call results for the logged-in agent with filters."""
    normalized_role = (current_user.role or "").strip().lower()
    is_admin = normalized_role in {"admin", "administrador"}
    is_agent = normalized_role in {"agent", "agente"}

    if not is_admin and not is_agent:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para este rol."
        )

    if is_admin:
        effective_owner_id = agent_owner_id or resolve_agent_owner_id(current_user)
    else: # is_agent
        effective_owner_id = resolve_agent_owner_id(current_user)
        if not effective_owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No hay agente asociado a este usuario."
            )

    if global_score_min is not None and global_score_max is not None:
        if global_score_min > global_score_max:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="global_score_min cannot be greater than global_score_max",
            )

    # 1. Get total count for metadata
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
    )

    # 2. Retrieve items page
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
    current_user: Annotated[User, Depends(get_current_user)],
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
    db: AsyncSession = Depends(get_db)
):
    """List detailed mass analysis call results with advanced filtering options."""
    normalized_role = (current_user.role or "").strip().lower()
    is_admin = normalized_role in {"admin", "administrador"}
    is_agent = normalized_role in {"agent", "agente"}

    if not is_admin and not is_agent:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para este rol."
        )

    if not is_admin: # is_agent
        resolved_id = resolve_agent_owner_id(current_user)
        if not resolved_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No hay agente asociado a este usuario."
            )
        agent_owner_id = resolved_id

    if global_score_min is not None and global_score_max is not None:
        if global_score_min > global_score_max:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="global_score_min cannot be greater than global_score_max",
            )

    from app.utils.visual_formatters import build_items_visual
    results = await MassEvaluationService.list_results(
        db,
        run_id=run_id,
        job_id=job_id,
        agent_owner_id=agent_owner_id,
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
    )
    
    out = []
    for r in results:
        # Avoid relying on model_validator, build response model explicitly
        d = MassEvaluationResultResponse.model_validate(r)
        d.items_visual = build_items_visual(r.items_json)
        if d.execution_source is None:
            d.execution_source = "on_demand"
        out.append(d)
    return out



@router.get("/mass-evaluation-results/{mass_analysis_id}", response_model=MassEvaluationResultResponse)
async def get_result(
    mass_analysis_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
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

    normalized_role = (current_user.role or "").strip().lower()
    is_admin = normalized_role in {"admin", "administrador"}
    is_agent = normalized_role in {"agent", "agente"}

    if not is_admin and not is_agent:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para este rol."
        )

    if not is_admin: # is_agent
        resolved_id = resolve_agent_owner_id(current_user)
        if not resolved_id or result.hubspot_owner_id != resolved_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para ver este análisis."
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
    db: AsyncSession = Depends(get_db)
):
    """List all active automation configurations."""
    return await MassEvaluationService.list_automations(db, limit=limit)


@router.get("/mass-analysis/automations/{automation_id}", response_model=MassAnalysisAutomationResponse)
async def get_automation(
    automation_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a single automation configuration."""
    automation = await MassEvaluationService.get_automation(db, automation_id=automation_id)
    if not automation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
        )
    return automation


@router.post("/mass-analysis/automations", response_model=MassAnalysisAutomationResponse, status_code=status.HTTP_201_CREATED)
async def create_automation(
    payload: MassAnalysisAutomationCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new automation configuration."""
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
    db: AsyncSession = Depends(get_db)
):
    """Update an automation configuration."""
    automation = await MassEvaluationService.update_automation(db, automation_id=automation_id, payload=payload)
    if not automation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
        )
    return automation


@router.delete("/mass-analysis/automations/{automation_id}", status_code=status.HTTP_200_OK)
async def delete_automation(
    automation_id: int,
    soft: bool = True,
    db: AsyncSession = Depends(get_db)
):
    """Deactivate or delete an automation configuration."""
    success = await MassEvaluationService.delete_automation(db, automation_id=automation_id, soft_delete=soft)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
        )
    return {"message": f"Automation configuration ID {automation_id} successfully deleted."}


@router.post("/mass-analysis/automations/{automation_id}/run-now", response_model=MassAnalysisAutomationRunResponse)
async def run_automation_now(
    automation_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Trigger an automation execution run immediately."""
    automation = await MassEvaluationService.get_automation(db, automation_id=automation_id)
    if not automation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
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
    db: AsyncSession = Depends(get_db)
):
    """List execution logs / runs for a given automation configuration."""
    automation = await MassEvaluationService.get_automation(db, automation_id=automation_id)
    if not automation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation configuration ID {automation_id} not found."
        )
    return await MassEvaluationService.list_automation_runs(db, automation_id=automation_id, limit=limit)
