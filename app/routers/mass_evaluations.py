"""API router for automated mass evaluations."""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.mass_evaluations import (
    MassEvaluationJobCreate,
    MassEvaluationJobManualRunRequest,
    MassEvaluationJobResponse,
    MassEvaluationJobUpdate,
    MassEvaluationResultResponse,
    MassEvaluationRunResponse,
    MassEvaluationRunLaunchResponse,
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
    job = await MassEvaluationService.update_job(db, job_id=job_id, payload=payload)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job ID {job_id} not found."
        )
    return job


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


# ── Results Endpoints ─────────────────────────────────────────────────────────

@router.get("/mass-evaluation-results", response_model=list[MassEvaluationResultResponse])
async def list_results(
    run_id: int | None = Query(None),
    job_id: int | None = Query(None),
    agent_owner_id: str | None = Query(None),
    call_id: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db)
):
    """List detailed mass analysis call results with advanced filtering options."""
    return await MassEvaluationService.list_results(
        db,
        run_id=run_id,
        job_id=job_id,
        agent_owner_id=agent_owner_id,
        call_id=call_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit
    )


@router.get("/mass-evaluation-results/{mass_analysis_id}", response_model=MassEvaluationResultResponse)
async def get_result(
    mass_analysis_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Retrieve full analysis result and normalized prompt snapshot elements of a call."""
    result = await MassEvaluationService.get_result(db, mass_analysis_id=mass_analysis_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mass analysis result ID {mass_analysis_id} not found."
        )
    return result
