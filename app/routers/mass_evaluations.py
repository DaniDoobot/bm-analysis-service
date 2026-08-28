"""API router for automated mass evaluations."""
from datetime import datetime
from typing import Any, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status, status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, get_tenant_context
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.users import User
from app.models.prompts import Prompt
from app.models.services import Service
import time
from app.utils.hubspot_owners import resolve_owner_id_by_email
from app.utils.normalizers import normalize_direction, normalize_typology, normalize_status, normalize_sort
from app.utils.dates import parse_madrid_date_bounds


def _fix_date_to_end_of_day(dt: "datetime | None") -> "datetime | None":
    """Adjust date_to / created_to to end-of-day when the caller passed a bare date.

    FastAPI automatically converts 'YYYY-MM-DD' query strings to
    datetime(YYYY, MM, DD, 0, 0, 0) (midnight).  Passing midnight as an
    upper-bound means the WHERE clause excludes every record created *during*
    that day.  We detect the midnight pattern and slide the bound to
    23:59:59.999999 so that single-day filters are fully inclusive.
    """
    if dt is None:
        return None
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt
from app.schemas.mass_evaluations import (
    MassEvaluationJobCreate,
    MassEvaluationJobManualRunRequest,
    MassEvaluationJobResponse,
    MassEvaluationJobUpdate,
    MassEvaluationResultResponse,
    MassEvaluationResultListItemResponse,
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
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para ver jobs masivos."
        )
    jobs = await MassEvaluationService.list_jobs(
        db,
        limit=limit,
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids
    )
    if context.normalized_role == InternalRole.TEAM_COORDINATOR and context.allowed_agent_ids is not None:
        filtered = []
        for j in jobs:
            if j.agent_owner_ids:
                if any(a in context.allowed_agent_ids for a in j.agent_owner_ids):
                    filtered.append(j)
            else:
                filtered.append(j)
        return filtered
    return jobs


@router.get("/mass-evaluation-jobs/{job_id}", response_model=MassEvaluationJobResponse)
async def get_job(
    job_id: int,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a single mass evaluation job."""
    if context.normalized_role == InternalRole.AGENT:
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
        if context.normalized_role == InternalRole.TEAM_COORDINATOR and context.allowed_agent_ids is not None:
            if job.agent_owner_ids:
                if not any(a in context.allowed_agent_ids for a in job.agent_owner_ids):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Acceso denegado: este job pertenece a agentes fuera de tus equipos."
                    )
    return job


@router.post("/mass-evaluation-jobs", response_model=MassEvaluationJobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: MassEvaluationJobCreate,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Create a new mass evaluation job."""
    if context.normalized_role == InternalRole.AGENT:
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

    if not context.is_super_admin and context.allowed_agent_ids is not None:
        if payload.agent_owner_ids:
            for a_id in payload.agent_owner_ids:
                if a_id not in context.allowed_agent_ids:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Acceso denegado: no tienes permisos para incluir al agente {a_id}."
                    )
        elif context.normalized_role == InternalRole.TEAM_COORDINATOR:
            payload.agent_owner_ids = context.allowed_agent_ids

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
    if context.normalized_role == InternalRole.AGENT:
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
        if context.allowed_agent_ids is not None and payload.agent_owner_ids:
            for a_id in payload.agent_owner_ids:
                if a_id not in context.allowed_agent_ids:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Acceso denegado: no tienes permisos para incluir al agente {a_id}."
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
    if context.normalized_role == InternalRole.AGENT:
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
    if context.normalized_role == InternalRole.AGENT:
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
        if context.normalized_role == InternalRole.TEAM_COORDINATOR and context.allowed_agent_ids is not None:
            if job.agent_owner_ids:
                if not any(a in context.allowed_agent_ids for a in job.agent_owner_ids):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Acceso denegado: este job pertenece a agentes fuera de tus equipos."
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
    run_status: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=1000),
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """List mass evaluation executions, optionally filtering by job and status."""
    if context.normalized_role == InternalRole.AGENT:
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
        status=run_status,
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
    if context.normalized_role == InternalRole.AGENT:
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
    if context.normalized_role == InternalRole.AGENT:
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
@router.get("/mass-evaluations/results/my-results", response_model=PagedMassEvaluationResultResponse)
async def get_my_analysis_results(
    context: TenantContext = Depends(get_tenant_context),
    run_id: Annotated[int | None, Query()] = None,
    job_id: Annotated[int | None, Query()] = None,
    automation_id: Annotated[int | None, Query(description="Filter by automation ID")] = None,
    agent_owner_id: Annotated[str | None, Query(description="For backwards compatibility, ignored for agents")] = None,
    call_id: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | datetime | None, Query()] = None,
    date_to: Annotated[str | datetime | None, Query(description="Inclusive upper-bound on call_timestamp.")] = None,
    period: Annotated[str | None, Query(description="24h | 7d | 30d | 90d")] = None,
    created_from: Annotated[datetime | None, Query(description="Filter by result creation date from")] = None,
    created_to: Annotated[datetime | None, Query(description="Inclusive upper-bound on created_at.")] = None,
    execution_source: Annotated[str | None, Query(description="on_demand | automation")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    global_score_min: Annotated[float | None, Query(ge=0.0, le=10.0)] = None,
    eval_min: Annotated[float | None, Query(ge=0.0, le=10.0, description="Alias for global_score_min")] = None,
    score_min: Annotated[float | None, Query(ge=0.0, le=10.0, description="Alias for global_score_min")] = None,
    global_score_max: Annotated[float | None, Query(ge=0.0, le=10.0)] = None,
    eval_max: Annotated[float | None, Query(ge=0.0, le=10.0, description="Alias for global_score_max")] = None,
    score_max: Annotated[float | None, Query(ge=0.0, le=10.0, description="Alias for global_score_max")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    service: Annotated[str | None, Query(description="Filter by service ID, key or slug")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    typology: Annotated[str | None, Query(description="Alias for typology_key")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Alias for typology_key")] = None,
    call_type: Annotated[str | None, Query(description="Alias for typology_key")] = None,
    selected_typology: Annotated[str | None, Query(description="Alias for typology_key")] = None,
    typologies: Annotated[str | None, Query(description="Alias for typology_key")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs to filter")] = None,
    direction: Annotated[str | None, Query(description="all | inbound | outbound")] = None,
    call_direction: Annotated[str | None, Query(description="Filter by call direction")] = None,
    inbound_outbound: Annotated[str | None, Query(description="Filter by inbound/outbound")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    min_duration: Annotated[int | None, Query(description="Alias for duration_min_seconds")] = None,
    min_duration_seconds: Annotated[int | None, Query(description="Alias for duration_min_seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    max_duration: Annotated[int | None, Query(description="Alias for duration_max_seconds")] = None,
    max_duration_seconds: Annotated[int | None, Query(description="Alias for duration_max_seconds")] = None,
    status: Annotated[str | None, Query(description="Filter by result status: completed | failed | all")] = None,
    result_status: Annotated[str | None, Query(description="Alias for status")] = None,
    item_filters: Annotated[str | None, Query(description="JSON url-encoded item score/boolean filters")] = None,
    criterion_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    item_score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    sort_by: Annotated[str | None, Query(description="Field to sort by: date, agent, call_id, duration, score, typology, direction, status, service, execution_source")] = None,
    sort_order: Annotated[str | None, Query(description="Sort direction: asc or desc (default: desc)")] = None,
    order_by: Annotated[str | None, Query(description="Alias for sort_by")] = None,
    order: Annotated[str | None, Query(description="Alias for sort_order")] = None,
    sort_direction: Annotated[str | None, Query(description="Alias for sort_order")] = None,
    include_detail: Annotated[bool, Query(description="Include heavy prompt_snapshot, result_json, items_json if True")] = False,
    db: AsyncSession = Depends(get_db)
):
    """List detailed mass analysis call results for the logged-in agent with filters."""
    effective_item_filters = item_filters or criterion_filters or score_filters or item_score_filters
    raw_status = status or result_status
    norm_status = normalize_status(raw_status)
    eff_status = norm_status if norm_status != "all" else None
    raw_sort_by = sort_by or order_by
    raw_sort_order = sort_order or order or sort_direction
    norm_sort_by, norm_sort_order = normalize_sort(raw_sort_by, raw_sort_order)
    if automation_id is not None and job_id is None:
        from app.models.mass_evaluations import MassAnalysisAutomation
        aut_stmt = select(MassAnalysisAutomation.job_id).where(MassAnalysisAutomation.automation_id == automation_id)
        aut_res = await db.execute(aut_stmt)
        job_id = aut_res.scalar()

    if service and not service_id and not service_key:
        from app.utils.service_resolvers import resolve_service_id
        resolved_id, resolved_key = await resolve_service_id(db, service_param=service)
        service_id = resolved_id or service_id
        service_key = resolved_key or service_key

    # Consolidate alias inputs
    raw_typology = typology_key or typology or tipo_llamada or call_type or selected_typology or typologies
    norm_typology_key = normalize_typology(raw_typology)
    raw_direction = direction or call_direction or inbound_outbound
    norm_d = normalize_direction(raw_direction)

    eff_dur_min = duration_min_seconds if duration_min_seconds is not None else (min_duration if min_duration is not None else min_duration_seconds)
    eff_dur_max = duration_max_seconds if duration_max_seconds is not None else (max_duration if max_duration is not None else max_duration_seconds)

    eff_score_min = global_score_min if global_score_min is not None else (eval_min if eval_min is not None else score_min)
    eff_score_max = global_score_max if global_score_max is not None else (eval_max if eval_max is not None else score_max)

    # Timezone & date handling: convert to Europe/Madrid local bounds in UTC
    dt_from, dt_to = parse_madrid_date_bounds(date_from, date_to, period)
    created_to = _fix_date_to_end_of_day(created_to)

    # Enforce agent scope
    if context.normalized_role == InternalRole.AGENT:
        effective_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
        if not effective_owner_id:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="No hay agente asociado a este usuario."
            )
        if agent_owner_id and agent_owner_id != effective_owner_id:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para ver resultados de este agente."
            )
    else:
        effective_owner_id = agent_owner_id
        if effective_owner_id and context.allowed_agent_ids is not None:
            if effective_owner_id not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="No tienes permiso para ver resultados de este agente."
                )

    if eff_score_min is not None and eff_score_max is not None:
        if eff_score_min > eff_score_max:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="global_score_min cannot be greater than global_score_max",
            )

    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

    # Validate query service_id if provided
    if service_id is not None and context.allowed_service_ids is not None:
        if service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="No tienes acceso al servicio seleccionado."
            )

    from app.utils.memory_utils import track_memory_async

    async with track_memory_async("mass_evaluation_results"):
        t_start = time.perf_counter()
        total = await MassEvaluationService.count_results(
            db,
            run_id=run_id,
            job_id=job_id,
            agent_owner_id=effective_owner_id,
            call_id=call_id,
            date_from=dt_from,
            date_to=dt_to,
            created_from=created_from,
            created_to=created_to,
            execution_source=execution_source,
            global_score_min=eff_score_min,
            global_score_max=eff_score_max,
            service_id=service_id,
            service_key=service_key,
            typology_key=norm_typology_key,
            typology_ids=typo_ids,
            duration_min_seconds=eff_dur_min,
            duration_max_seconds=eff_dur_max,
            direction=norm_d,
            company_ids=None if context.is_super_admin else context.allowed_company_ids,
            service_ids=None if context.normalized_role == InternalRole.AGENT else context.allowed_service_ids,
            allowed_agent_ids=context.allowed_agent_ids if not effective_owner_id else None,
            status=eff_status,
            item_filters=effective_item_filters,
        )

        from app.utils.visual_formatters import build_items_visual
        results = await MassEvaluationService.list_results(
            db,
            run_id=run_id,
            job_id=job_id,
            agent_owner_id=effective_owner_id,
            call_id=call_id,
            date_from=dt_from,
            date_to=dt_to,
            created_from=created_from,
            created_to=created_to,
            execution_source=execution_source,
            limit=limit,
            global_score_min=eff_score_min,
            global_score_max=eff_score_max,
            service_id=service_id,
            service_key=service_key,
            typology_key=norm_typology_key,
            offset=offset,
            typology_ids=typo_ids,
            duration_min_seconds=eff_dur_min,
            duration_max_seconds=eff_dur_max,
            direction=norm_d,
            company_ids=None if context.is_super_admin else context.allowed_company_ids,
            service_ids=None if context.normalized_role == InternalRole.AGENT else context.allowed_service_ids,
            allowed_agent_ids=context.allowed_agent_ids if not effective_owner_id else None,
            status=eff_status,
            item_filters=effective_item_filters,
            sort_by=norm_sort_by,
            sort_order=norm_sort_order,
        )
        
        items_out = []
        for r in results:
            if include_detail:
                d = MassEvaluationResultResponse.model_validate(r)
            else:
                d = MassEvaluationResultListItemResponse.model_validate(r)
            d.items_visual = build_items_visual(r.items_json)
            if d.execution_source is None:
                d.execution_source = "on_demand"
            items_out.append(d)

        total_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
        import logging
        logging.getLogger(__name__).info(
            "[perf.mass_evaluation_results] endpoint=/bm/me/analysis-results total_ms=%.1f rows=%d total=%d limit=%d offset=%d direction=%s",
            total_ms, len(items_out), total, limit, offset, norm_d
        )

        return PagedMassEvaluationResultResponse(
            items=items_out,
            total=total,
            limit=limit,
            offset=offset
        )


@router.get("/mass-evaluation-results", response_model=PagedMassEvaluationResultResponse)
@router.get("/mass-evaluations/results", response_model=PagedMassEvaluationResultResponse)
async def list_results(
    context: TenantContext = Depends(get_tenant_context),
    run_id: int | None = Query(None),
    job_id: int | None = Query(None),
    automation_id: int | None = Query(None, description="Filter by automation ID"),
    agent_owner_id: str | None = Query(None),
    agent_id: str | None = Query(None, description="Alias for agent_owner_id"),
    owner_id: str | None = Query(None, description="Alias for agent_owner_id"),
    agent: str | None = Query(None, description="Alias for agent_owner_id"),
    call_id: str | None = Query(None),
    date_from: str | datetime | None = Query(None),
    date_to: str | datetime | None = Query(None, description="Inclusive upper-bound on call_timestamp."),
    period: str | None = Query(None, description="24h | 7d | 30d | 90d"),
    created_from: datetime | None = Query(None, description="Filter by result creation date from"),
    created_to: datetime | None = Query(None, description="Inclusive upper-bound on created_at."),
    execution_source: str | None = Query(None, description="on_demand | automation"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    global_score_min: float | None = Query(None, ge=0.0, le=10.0),
    eval_min: float | None = Query(None, ge=0.0, le=10.0, description="Alias for global_score_min"),
    score_min: float | None = Query(None, ge=0.0, le=10.0, description="Alias for global_score_min"),
    global_score_max: float | None = Query(None, ge=0.0, le=10.0),
    eval_max: float | None = Query(None, ge=0.0, le=10.0, description="Alias for global_score_max"),
    score_max: float | None = Query(None, ge=0.0, le=10.0, description="Alias for global_score_max"),
    service_id: int | None = Query(None, description="Filter by service ID"),
    service_key: str | None = Query(None, description="Filter by service key"),
    service: str | None = Query(None, description="Filter by service ID, key or slug"),
    typology_key: str | None = Query(None, description="Filter by typology key"),
    typology: str | None = Query(None, description="Alias for typology_key"),
    tipo_llamada: str | None = Query(None, description="Alias for typology_key"),
    call_type: str | None = Query(None, description="Alias for typology_key"),
    selected_typology: str | None = Query(None, description="Alias for typology_key"),
    typologies: str | None = Query(None, description="Alias for typology_key"),
    typology_ids: str | None = Query(None, description="Comma-separated typology IDs to filter"),
    direction: str | None = Query(None, description="all | inbound | outbound"),
    call_direction: str | None = Query(None, description="Filter by call direction"),
    inbound_outbound: str | None = Query(None, description="Filter by inbound/outbound"),
    duration_min_seconds: int | None = Query(None, description="Min duration in seconds"),
    min_duration: int | None = Query(None, description="Alias for duration_min_seconds"),
    min_duration_seconds: int | None = Query(None, description="Alias for duration_min_seconds"),
    duration_max_seconds: int | None = Query(None, description="Max duration in seconds"),
    max_duration: int | None = Query(None, description="Alias for duration_max_seconds"),
    max_duration_seconds: int | None = Query(None, description="Alias for duration_max_seconds"),
    result_status: str | None = Query(None, alias="status", description="Filter by result status (e.g. completed)"),
    item_filters: str | None = Query(None, description="JSON url-encoded item score/boolean filters"),
    criterion_filters: str | None = Query(None, description="Alias for item_filters"),
    score_filters: str | None = Query(None, description="Alias for item_filters"),
    item_score_filters: str | None = Query(None, description="Alias for item_filters"),
    sort_by: str | None = Query(None, description="Field to sort by: date, agent, call_id, duration, score, typology, direction, status, service, execution_source"),
    sort_order: str | None = Query(None, description="Sort direction: asc or desc (default: desc)"),
    order_by: str | None = Query(None, description="Alias for sort_by"),
    order: str | None = Query(None, description="Alias for sort_order"),
    sort_direction: str | None = Query(None, description="Alias for sort_order"),
    include_detail: bool = Query(False, description="Include heavy prompt_snapshot, result_json, items_json if True"),
    db: AsyncSession = Depends(get_db)
):
    """List detailed mass analysis call results with advanced filtering and full pagination metadata."""
    effective_item_filters = item_filters or criterion_filters or score_filters or item_score_filters
    raw_sort_by = sort_by or order_by
    raw_sort_order = sort_order or order or sort_direction
    norm_sort_by, norm_sort_order = normalize_sort(raw_sort_by, raw_sort_order)
    t_start = time.perf_counter()
    if automation_id is not None and job_id is None:
        from app.models.mass_evaluations import MassAnalysisAutomation
        aut_stmt = select(MassAnalysisAutomation.job_id).where(MassAnalysisAutomation.automation_id == automation_id)
        aut_res = await db.execute(aut_stmt)
        job_id = aut_res.scalar()

    if service and not service_id and not service_key:
        from app.utils.service_resolvers import resolve_service_id
        resolved_id, resolved_key = await resolve_service_id(db, service_param=service)
        service_id = resolved_id or service_id
        service_key = resolved_key or service_key

    # Consolidate alias inputs
    raw_owner_id = agent_owner_id or agent_id or owner_id or agent
    raw_typology = typology_key or typology or tipo_llamada or call_type or selected_typology or typologies
    norm_typology_key = normalize_typology(raw_typology)
    raw_direction = direction or call_direction or inbound_outbound
    norm_d = normalize_direction(raw_direction)

    eff_dur_min = duration_min_seconds if duration_min_seconds is not None else (min_duration if min_duration is not None else min_duration_seconds)
    eff_dur_max = duration_max_seconds if duration_max_seconds is not None else (max_duration if max_duration is not None else max_duration_seconds)

    eff_score_min = global_score_min if global_score_min is not None else (eval_min if eval_min is not None else score_min)
    eff_score_max = global_score_max if global_score_max is not None else (eval_max if eval_max is not None else score_max)

    norm_status = normalize_status(result_status)
    eff_status = norm_status if norm_status != "all" else None

    # Timezone & date handling: convert to Europe/Madrid local bounds in UTC
    dt_from, dt_to = parse_madrid_date_bounds(date_from, date_to, period)
    created_to = _fix_date_to_end_of_day(created_to)

    if context.normalized_role == InternalRole.AGENT:
        effective_owner_id = context.allowed_agent_ids[0] if context.allowed_agent_ids else None
        if not effective_owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No hay agente asociado a este usuario."
            )
        if raw_owner_id and raw_owner_id != effective_owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para ver resultados de este agente."
            )
    else:
        effective_owner_id = raw_owner_id
        if effective_owner_id and context.allowed_agent_ids is not None:
            if effective_owner_id not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permiso para ver resultados de este agente."
                )

    if eff_score_min is not None and eff_score_max is not None:
        if eff_score_min > eff_score_max:
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

    t_db_start = time.perf_counter()
    total = await MassEvaluationService.count_results(
        db,
        run_id=run_id,
        job_id=job_id,
        agent_owner_id=effective_owner_id,
        call_id=call_id,
        date_from=dt_from,
        date_to=dt_to,
        created_from=created_from,
        created_to=created_to,
        execution_source=execution_source,
        global_score_min=eff_score_min,
        global_score_max=eff_score_max,
        service_id=service_id,
        service_key=service_key,
        typology_key=norm_typology_key,
        typology_ids=typo_ids,
        duration_min_seconds=eff_dur_min,
        duration_max_seconds=eff_dur_max,
        direction=norm_d,
        company_ids=None if context.is_super_admin else context.allowed_company_ids,
        service_ids=context.allowed_service_ids,
        allowed_agent_ids=context.allowed_agent_ids if not effective_owner_id else None,
        status=eff_status,
        item_filters=effective_item_filters,
    )

    results = await MassEvaluationService.list_results(
        db,
        run_id=run_id,
        job_id=job_id,
        agent_owner_id=effective_owner_id,
        call_id=call_id,
        date_from=dt_from,
        date_to=dt_to,
        created_from=created_from,
        created_to=created_to,
        execution_source=execution_source,
        limit=limit,
        offset=offset,
        global_score_min=eff_score_min,
        global_score_max=eff_score_max,
        service_id=service_id,
        service_key=service_key,
        typology_key=norm_typology_key,
        typology_ids=typo_ids,
        duration_min_seconds=eff_dur_min,
        duration_max_seconds=eff_dur_max,
        direction=norm_d,
        company_ids=None if context.is_super_admin else context.allowed_company_ids,
        service_ids=context.allowed_service_ids,
        allowed_agent_ids=context.allowed_agent_ids if not effective_owner_id else None,
        status=eff_status,
        item_filters=effective_item_filters,
        sort_by=norm_sort_by,
        sort_order=norm_sort_order,
    )
    db_ms = round((time.perf_counter() - t_db_start) * 1000.0, 1)

    items_out = []
    response_mode = "list_full" if include_detail else "list_light"
    for r in results:
        if include_detail:
            d = MassEvaluationResultResponse.model_validate(r)
        else:
            d = MassEvaluationResultListItemResponse.model_validate(r)
        d.items_visual = build_items_visual(r.items_json)
        if d.execution_source is None:
            d.execution_source = "on_demand"
        items_out.append(d)

    total_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
    import logging, json
    # Rough estimate of response byte size
    try:
        sample_json = json.dumps([item.model_dump(mode="json") for item in items_out[:5]])
        estimated_bytes = (len(sample_json) // max(len(items_out[:5]), 1)) * len(items_out)
    except Exception:
        estimated_bytes = len(items_out) * (150000 if include_detail else 1000)

    filters_summary = {
        "run_id": run_id, "job_id": job_id, "agent_owner_id": effective_owner_id,
        "date_from": dt_from.isoformat() if dt_from else None, "date_to": dt_to.isoformat() if dt_to else None,
        "service_id": service_id, "typology_key": norm_typology_key, "direction": norm_d,
        "status": result_status, "include_detail": include_detail
    }
    logging.getLogger(__name__).info(
        "[perf.mass_results] response_mode=%s response_bytes_estimated=%d total_ms=%.1f db_ms=%.1f total=%d returned=%d limit=%d offset=%d filters=%s",
        response_mode, estimated_bytes, total_ms, db_ms, total, len(items_out), limit, offset, filters_summary
    )

    return PagedMassEvaluationResultResponse(
        items=items_out,
        total=total,
        limit=limit,
        offset=offset
    )



@router.get("/mass-evaluation-results/{mass_analysis_id}", response_model=MassEvaluationResultResponse)
@router.get("/mass-evaluations/results/{mass_analysis_id}", response_model=MassEvaluationResultResponse)
@router.get("/me/analysis-results/{mass_analysis_id}", response_model=MassEvaluationResultResponse)
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
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: este resultado pertenece a otra empresa."
            )
        if context.normalized_role == InternalRole.AGENT:
            agent_owners = [str(a).strip() for a in context.allowed_agent_ids] if context.allowed_agent_ids else []
            res_owner = str(result.hubspot_owner_id).strip() if result.hubspot_owner_id is not None else None
            if not agent_owners or not res_owner or res_owner not in agent_owners:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="No tienes permiso para consultar este análisis."
                )
        else:
            if context.allowed_service_ids is not None and result.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: este resultado pertenece a un servicio no asignado."
                )
            if context.allowed_agent_ids is not None:
                allowed_str_ids = [str(a).strip() for a in context.allowed_agent_ids]
                res_owner = str(result.hubspot_owner_id).strip() if result.hubspot_owner_id is not None else None
                if not res_owner or res_owner not in allowed_str_ids:
                    raise HTTPException(
                        status_code=http_status.HTTP_403_FORBIDDEN,
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
    active: str | None = Query(None, description="true | false | all"),
    include_inactive: bool | None = Query(None, description="Include inactive automations if true"),
    include_archived: bool = Query(False, description="Include archived automations if true"),
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """List automation configurations (active and inactive by default)."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para gestionar automatizaciones."
        )
    return await MassEvaluationService.list_automations(
        db,
        limit=limit,
        active=active,
        include_inactive=include_inactive,
        include_archived=include_archived,
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids
    )


from datetime import datetime, timedelta, timezone


@router.get("/mass-analysis/automations/scheduler-status")
async def get_automation_scheduler_status(
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve diagnostic status for the automation scheduler background worker and due automations count."""
    from app.config import get_settings
    from app.models.mass_evaluations import MassAnalysisAutomation, MassAnalysisAutomationRun
    from sqlalchemy import or_
    settings = get_settings()

    stale_threshold_min = settings.automation_running_stale_after_minutes or 60

    all_automations = await MassEvaluationService.list_automations(
        db,
        limit=1000,
        active="all",
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids
    )

    active_automations = [aut for aut in all_automations if aut.is_active]
    inactive_automations = [aut for aut in all_automations if not aut.is_active]

    now = datetime.now(timezone.utc)
    due_count = 0
    for aut in active_automations:
        interval_min = aut.interval_minutes or 30
        last_at = aut.last_run_at
        if last_at and last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        if last_at is None or (now - last_at) >= timedelta(minutes=interval_min):
            due_count += 1

    # Query active running automation runs
    running_stmt = select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.status == "running")
    if context.allowed_service_ids is not None or context.allowed_company_ids is not None:
        from app.models.services import Service
        running_stmt = running_stmt.join(MassAnalysisAutomation, MassAnalysisAutomation.automation_id == MassAnalysisAutomationRun.automation_id)
        if context.allowed_service_ids is not None:
            running_stmt = running_stmt.where(MassAnalysisAutomation.service_id.in_(context.allowed_service_ids))
        elif context.allowed_company_ids:
            running_stmt = running_stmt.join(Service, Service.service_id == MassAnalysisAutomation.service_id).where(Service.company_id.in_(context.allowed_company_ids))

    res_running = await db.execute(running_stmt)
    running_runs = res_running.scalars().all()

    running_automations_count = len(running_runs)
    stale_running_automations_count = 0
    blocked_automations_count = len(set(r.automation_id for r in running_runs))
    blocked_runs = []

    for r in running_runs:
        st_at = r.started_at
        if st_at and st_at.tzinfo is None:
            st_at = st_at.replace(tzinfo=timezone.utc)
        age_min = int((now - st_at).total_seconds() / 60) if st_at else 0
        if age_min >= stale_threshold_min:
            stale_running_automations_count += 1

        blocked_runs.append({
            "automation_id": r.automation_id,
            "automation_run_id": r.automation_run_id,
            "run_id": r.run_id,
            "status": r.status,
            "age_minutes": age_min,
            "is_stale": age_min >= stale_threshold_min,
            "started_at": r.started_at.isoformat() if r.started_at else None
        })

    return {
        "enabled": settings.enable_automation_scheduler,
        "mode": "background_loop" if settings.enable_automation_scheduler else "disabled_manual_or_cron",
        "interval_seconds": 60,
        "active_automations_count": len(active_automations),
        "inactive_automations_count": len(inactive_automations),
        "total_automations_count": len(all_automations),
        "due_automations_count": due_count,
        "running_automations_count": running_automations_count,
        "stale_running_automations_count": stale_running_automations_count,
        "blocked_automations_count": blocked_automations_count,
        "blocked_runs": blocked_runs,
        "stale_threshold_minutes": stale_threshold_min,
        "hint": "Set ENABLE_AUTOMATION_SCHEDULER=true in .env to activate the internal background worker loop."
    }


@router.post("/mass-analysis/automations/run-due")
async def trigger_run_due_automations(
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Manually or via cron trigger execution of all active automations that are due."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para ejecutar automatizaciones."
        )

    result = await MassEvaluationService.run_due_automations(
        db,
        company_ids=context.allowed_company_ids,
        service_ids=context.allowed_service_ids
    )
    return result


@router.post("/mass-analysis/automations/runs/{run_id}/mark-stale-failed", response_model=MassAnalysisAutomationRunResponse)
async def mark_automation_run_stale_failed(
    run_id: int,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Manually mark a stuck or stale running automation execution run as failed."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para modificar ejecuciones de automatizaciones."
        )
    run = await MassEvaluationService.mark_automation_run_stale_failed(db, run_id=run_id, context=context)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Automation run with ID {run_id} not found."
        )
    return run


@router.get("/mass-analysis/automations/{automation_id}", response_model=MassAnalysisAutomationResponse)
async def get_automation(
    automation_id: int,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve details of a single automation configuration."""
    if context.normalized_role == InternalRole.AGENT:
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
    if context.normalized_role == InternalRole.AGENT:
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

        if context.allowed_agent_ids is not None:
            if payload.agent_owner_ids is not None:
                for a_id in payload.agent_owner_ids:
                    if a_id not in context.allowed_agent_ids:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Acceso denegado: no tienes permisos para incluir al agente {a_id}."
                        )
            elif context.normalized_role == InternalRole.TEAM_COORDINATOR:
                payload.agent_owner_ids = context.allowed_agent_ids

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
    if context.normalized_role == InternalRole.AGENT:
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
    if context.normalized_role == InternalRole.AGENT:
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
    if context.normalized_role == InternalRole.AGENT:
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
    if context.normalized_role == InternalRole.AGENT:
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

