"""
CloudTalk Webhook and Integration Router.
Endpoints:
- POST /bm/integrations/cloudtalk/call-ended (CloudTalk Workflow Automation trigger)
- GET /bm/integrations/cloudtalk/jobs (Admin/diagnostic: list jobs)
- GET /bm/integrations/cloudtalk/jobs/{job_id} (Admin/diagnostic: get job details)
- POST /bm/integrations/cloudtalk/jobs/{job_id}/retry (Admin/diagnostic: retry failed job)
"""
import asyncio
from datetime import datetime, timezone
import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, require_admin
from app.models.cloudtalk_jobs import CloudTalkEvaluationJob
from app.models.users import User
from app.schemas.cloudtalk import CloudTalkJobOut, CloudTalkWebhookPayload, CloudTalkWebhookResponse
from app.services.cloudtalk_webhook_service import (
    handle_call_ended_webhook,
    process_cloudtalk_job_background,
    validate_webhook_secret,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bm/integrations/cloudtalk", tags=["CloudTalk Integration"])


@router.post(
    "/call-ended",
    response_model=CloudTalkWebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive CloudTalk Call Ended webhook and enqueue automatic evaluation",
)
async def receive_cloudtalk_call_ended(
    request: Request,
    payload: CloudTalkWebhookPayload,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_cloudtalk_webhook_key: Optional[str] = Header(None, alias="X-CloudTalk-Webhook-Key"),
):
    """
    Webhook endpoint called by CloudTalk Workflow Automation on Call -> Ended.
    Secured via static shared secret header 'X-CloudTalk-Webhook-Key'.
    Enqueues the job persistently in PostgreSQL and immediately triggers background processing.
    """
    if not validate_webhook_secret(x_cloudtalk_webhook_key):
        logger.warning(
            "Rejected unauthorized CloudTalk webhook request from %s: invalid or missing X-CloudTalk-Webhook-Key header.",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación inválida para webhook de CloudTalk.",
        )

    # Ingest payload and schedule background processing
    result = await handle_call_ended_webhook(db, payload.model_dump())

    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=result.get("message", "Error al procesar payload de CloudTalk."),
        )

    return CloudTalkWebhookResponse(
        ok=True,
        status=result.get("status", "accepted"),
        job_id=result.get("job_id"),
        call_id=result.get("call_id"),
        duplicate=result.get("duplicate", False),
        retrying=result.get("retrying", False),
        message=result.get("message"),
        analysis_id=result.get("analysis_id"),
    )


# ── Protected Admin / Diagnostic Endpoints ────────────────────────────────────

@router.get(
    "/jobs",
    response_model=List[CloudTalkJobOut],
    summary="List CloudTalk evaluation jobs (diagnostic/admin)",
)
async def list_cloudtalk_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List recent CloudTalk evaluation jobs with optional status filter."""
    stmt = select(CloudTalkEvaluationJob).order_by(desc(CloudTalkEvaluationJob.id))
    if status_filter:
        stmt = stmt.where(CloudTalkEvaluationJob.status == status_filter.strip())
    stmt = stmt.offset(offset).limit(limit)

    res = await db.execute(stmt)
    jobs = res.scalars().all()
    return jobs


@router.get(
    "/jobs/{job_id}",
    response_model=CloudTalkJobOut,
    summary="Get single CloudTalk evaluation job details",
)
async def get_cloudtalk_job(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Retrieve diagnostic details for a specific CloudTalk evaluation job."""
    job = await db.get(CloudTalkEvaluationJob, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} no encontrado.",
        )
    return job


@router.post(
    "/jobs/{job_id}/retry",
    response_model=CloudTalkJobOut,
    summary="Manually retry a failed or configuration_missing CloudTalk evaluation job",
)
async def retry_cloudtalk_job(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[User, Depends(require_admin)],
):
    """Manually re-enqueues a failed or configuration_missing job for evaluation."""
    job = await db.get(CloudTalkEvaluationJob, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} no encontrado.",
        )

    now_utc = datetime.now(timezone.utc)
    job.status = "pending"
    job.attempt_count = 0
    job.next_attempt_at = now_utc
    job.last_error_code = None
    job.last_error_message = None
    job.updated_at = now_utc
    await db.commit()
    await db.refresh(job)

    # Spawn background worker
    asyncio.create_task(process_cloudtalk_job_background(job.id))
    logger.info("Admin %s triggered manual retry for cloudtalk job_id=%d", admin_user.email, job.id)

    return job
