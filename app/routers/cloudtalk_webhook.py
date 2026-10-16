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

import json
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

# ── TEMPORARY DIAGNOSTIC: CloudTalk webhook payload validation ─────────────────
SENSITIVE_FIELD_NAMES = {
    "key", "secret", "token", "password", "auth", "authorization",
    "cookie", "credential", "x-cloudtalk-webhook-key", "x_cloudtalk_webhook_key",
}
PII_FIELD_NAMES = {
    "external_number", "internal_number", "agent_email", "contact_number",
    "number", "public_external", "public_internal",
}
PRIORITIZED_SAFE_FIELDS = (
    "call_id", "cdr_id", "id", "call_uuid", "uuid",
    "talking_time", "billsec", "duration",
    "direction", "type", "started_at", "ended_at",
    "recording_url",
)


def _scrub_webhook_secrets(text: str) -> str:
    """Extra safety net: scrub configured secret from output if present."""
    try:
        from app.config import get_settings
        settings = get_settings()
        secret = (settings.cloudtalk_webhook_secret or "").strip()
        if secret and len(secret) > 3 and secret in text:
            text = text.replace(secret, "[REDACTED_WEBHOOK_SECRET]")
    except Exception:
        pass
    return text


def _sanitize_validation_errors(errors: list) -> list[dict]:
    """Sanitize Pydantic validation errors list: redacts secrets and masks PII."""
    sanitized = []
    for err in errors:
        loc = list(err.get("loc", []))
        msg = err.get("msg")
        err_type = err.get("type")
        raw_input = err.get("input")

        loc_str_parts = [str(p).lower() for p in loc]
        is_sensitive = any(any(s in part for s in SENSITIVE_FIELD_NAMES) for part in loc_str_parts)
        is_pii = any(any(p == part for p in PII_FIELD_NAMES) for part in loc_str_parts)

        if is_sensitive:
            safe_input = "[REDACTED]"
        elif is_pii:
            safe_input = "[MASKED_PII]"
        elif loc and str(loc[0]).lower() == "header":
            if len(loc) > 1 and str(loc[1]).lower() in ("content-type", "user-agent"):
                safe_input = raw_input
            else:
                safe_input = "[REDACTED_HEADER]"
        else:
            if isinstance(raw_input, str) and len(raw_input) > 250:
                safe_input = raw_input[:250] + "... [truncated]"
            elif isinstance(raw_input, dict):
                safe_input = {
                    k: "[REDACTED]" if any(s in str(k).lower() for s in SENSITIVE_FIELD_NAMES)
                    else ("[MASKED_PII]" if str(k).lower() in PII_FIELD_NAMES else v)
                    for k, v in raw_input.items()
                }
            else:
                safe_input = raw_input

        sanitized.append({
            "loc": loc,
            "msg": msg,
            "type": err_type,
            "input": safe_input,
        })
    return sanitized


async def _extract_safe_body_summary(request: Request) -> dict:
    """
    Extracts high-level type and prioritized non-sensitive fields from the request body.
    Never extracts credentials, tokens, or PII.
    """
    try:
        raw_body = await request.body()
        if not raw_body:
            return {"type": "empty", "length_bytes": 0}

        body_text = raw_body.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body_text)
        except Exception as json_err:
            return {
                "type": "non_json_or_malformed",
                "length_bytes": len(raw_body),
                "preview": body_text[:120].strip() if body_text else "",
                "parse_error": str(json_err),
            }

        if isinstance(parsed, dict):
            safe_sample = {}
            for k in PRIORITIZED_SAFE_FIELDS:
                if k in parsed:
                    v = parsed[k]
                    safe_sample[k] = {
                        "value": str(v)[:100] if v is not None else None,
                        "type": type(v).__name__,
                    }

            safe_keys = []
            for k in parsed.keys():
                k_lower = str(k).lower()
                if any(s in k_lower for s in SENSITIVE_FIELD_NAMES):
                    safe_keys.append("[REDACTED_KEY]")
                elif any(p == k_lower for p in PII_FIELD_NAMES):
                    safe_keys.append(f"{k} [PII]")
                else:
                    safe_keys.append(str(k))

            return {
                "type": "dict",
                "keys": safe_keys,
                "prioritized_fields": safe_sample,
            }
        elif isinstance(parsed, list):
            return {
                "type": "list",
                "length": len(parsed),
                "item_types": [type(item).__name__ for item in parsed[:5]],
            }
        else:
            return {
                "type": type(parsed).__name__,
                "value_repr": str(parsed)[:100],
            }
    except Exception as e:
        return {"type": "error_reading_body", "error": str(e)}


async def log_cloudtalk_request_validation_error(request: Request, exc: Any) -> None:
    """
    # TEMPORARY DIAGNOSTIC: CloudTalk webhook payload validation
    Logs 422 validation errors with safe allowlisted headers and masked PII/secrets.
    """
    endpoint = request.url.path
    content_type = request.headers.get("content-type", "none")
    user_agent = request.headers.get("user-agent", "none")

    raw_errors = exc.errors() if hasattr(exc, "errors") else []
    sanitized_errors = _sanitize_validation_errors(raw_errors)
    body_summary = await _extract_safe_body_summary(request)

    log_entry = (
        "[cloudtalk_webhook_validation_error]\n"
        f"endpoint={endpoint}\n"
        "status=422\n"
        f"content_type={content_type}\n"
        f"user_agent={user_agent}\n"
        f"errors={json.dumps(sanitized_errors, indent=2, ensure_ascii=False, default=str)}\n"
        f"body_summary={json.dumps(body_summary, indent=2, ensure_ascii=False, default=str)}"
    )

    logger.warning(_scrub_webhook_secrets(log_entry))


def log_cloudtalk_processing_validation_error(
    request: Request,
    result: dict,
    payload: Optional[CloudTalkWebhookPayload] = None,
) -> None:
    """
    # TEMPORARY DIAGNOSTIC: CloudTalk webhook payload validation
    Logs 422 processing errors when handle_call_ended_webhook returns ok=False.
    """
    endpoint = request.url.path
    content_type = request.headers.get("content-type", "none")
    user_agent = request.headers.get("user-agent", "none")
    message = result.get("message", "Validation error during processing")

    payload_summary = {}
    if payload:
        p_dict = payload.model_dump(exclude_unset=True)
        for k in PRIORITIZED_SAFE_FIELDS:
            if k in p_dict:
                v = p_dict[k]
                payload_summary[k] = {"value": str(v)[:100] if v is not None else None, "type": type(v).__name__}

    error_item = [{
        "loc": ["body", "call_id"],
        "msg": message,
        "type": "processing_validation_error",
        "input": payload_summary.get("call_id", {}).get("value") if payload_summary else None,
    }]

    log_entry = (
        "[cloudtalk_webhook_validation_error]\n"
        f"endpoint={endpoint}\n"
        "status=422\n"
        f"content_type={content_type}\n"
        f"user_agent={user_agent}\n"
        f"errors={json.dumps(error_item, indent=2, ensure_ascii=False, default=str)}\n"
        f"body_summary={json.dumps({'prioritized_fields': payload_summary}, indent=2, ensure_ascii=False, default=str)}"
    )

    logger.warning(_scrub_webhook_secrets(log_entry))
# ── END TEMPORARY DIAGNOSTIC ───────────────────────────────────────────────────

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
        # TEMPORARY DIAGNOSTIC: CloudTalk webhook payload validation
        log_cloudtalk_processing_validation_error(request, result, payload)
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
