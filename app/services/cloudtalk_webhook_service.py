"""
CloudTalk Webhook Service.
Handles CloudTalk Workflow Automation 'Call -> Ended' webhooks, idempotency,
recording readiness polling/retries, agent resolution, and automatic evaluation.
"""
import asyncio
from datetime import datetime, timezone, timedelta
import hmac
import logging
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session_factory
from app.models.cloudtalk_jobs import CloudTalkEvaluationJob
from app.models.services import Service
from app.models.users import User
from app.services.audio_download_service import AudioDownloadError
from app.services.cloudtalk_service import CloudTalkService
from app.services.openai_service import analyze_audio_bytes
from app.services.prompts_service import get_active_prompt
from app.services.analysis_persistence import save_analysis
from app.utils.hubspot_owners import resolve_owner_id_by_email, OWNER_TO_NAME
from app.utils.json_utils import safe_parse_json

logger = logging.getLogger(__name__)

RECORDING_RETRY_DELAYS = [3, 5, 10, 20, 40]  # Delays in seconds for attempts 1..5


def validate_webhook_secret(header_secret: Optional[str]) -> bool:
    """
    Validates X-CloudTalk-Webhook-Key against CLOUDTALK_WEBHOOK_SECRET using constant-time comparison.
    Never logs or exposes the secret.
    """
    settings = get_settings()
    configured_secret = (settings.cloudtalk_webhook_secret or "").strip()
    if not configured_secret:
        logger.warning("CLOUDTALK_WEBHOOK_SECRET is not configured on the server.")
        return False
    if not header_secret or not header_secret.strip():
        return False

    return hmac.compare_digest(header_secret.strip().encode("utf-8"), configured_secret.encode("utf-8"))


async def get_cloudtalk_bot_service(db: AsyncSession, company_id: int = 1) -> Service:
    """
    Resolves the 'Cloudtalk Bot' service record for the given company_id.
    """
    settings = get_settings()
    service_key = settings.cloudtalk_bot_service_key or "cloudtalk_bot"

    stmt = select(Service).where(Service.service_key == service_key)
    res = await db.execute(stmt)
    service = res.scalars().first()

    if not service:
        # Fallback to lookup by name or ID if key not yet matched
        stmt_name = select(Service).where(
            and_(Service.company_id == company_id, Service.service_name.ilike("%cloudtalk bot%"))
        )
        res_name = await db.execute(stmt_name)
        service = res_name.scalars().first()

    if not service:
        # Create service record dynamically if not found
        service = Service(
            company_id=company_id,
            service_key=service_key,
            service_name="Cloudtalk Bot",
            description="Evaluación automática de llamadas de CloudTalk",
            is_active=True,
        )
        db.add(service)
        await db.flush()

    return service


def _parse_int_safely(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return None


def _parse_datetime_safely(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        # Try ISO format
        s = str(val).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        pass
    # Try common timestamp strings
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except Exception:
            continue
    return None


def extract_webhook_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts normalized call attributes from a CloudTalk webhook payload,
    inspecting top-level and nested (Cdr, Call, Agent, Contact) keys.
    """
    cdr = payload.get("Cdr") if isinstance(payload.get("Cdr"), dict) else {}
    call_obj = payload.get("Call") if isinstance(payload.get("Call"), dict) else {}
    agent_obj = payload.get("Agent") if isinstance(payload.get("Agent"), dict) else {}
    contact_obj = payload.get("Contact") if isinstance(payload.get("Contact"), dict) else {}

    # Call ID / CDR ID
    raw_id = (
        payload.get("call_id")
        or payload.get("cdr_id")
        or payload.get("id")
        or cdr.get("id")
        or call_obj.get("id")
    )
    call_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else None

    # Call UUID
    raw_uuid = payload.get("call_uuid") or payload.get("uuid") or cdr.get("call_uuid") or call_obj.get("uuid")
    call_uuid = str(raw_uuid).strip() if raw_uuid else None

    # Talking time / Duration
    raw_tt = (
        payload.get("talking_time")
        or payload.get("billsec")
        or payload.get("duration")
        or cdr.get("talking_time")
        or cdr.get("billsec")
        or call_obj.get("talking_time")
        or call_obj.get("billsec")
    )
    talking_time = _parse_int_safely(raw_tt)

    # Direction / Type
    direction = (
        payload.get("direction")
        or payload.get("type")
        or cdr.get("type")
        or call_obj.get("type")
    )
    if direction:
        direction = str(direction).strip().lower()

    # Agent details
    raw_agent_id = (
        payload.get("agent_id")
        or agent_obj.get("id")
        or cdr.get("agent_id")
    )
    agent_id = str(raw_agent_id).strip() if raw_agent_id is not None else None

    agent_name = payload.get("agent_name") or agent_obj.get("fullname")
    if not agent_name and (agent_obj.get("firstname") or agent_obj.get("lastname")):
        agent_name = f"{agent_obj.get('firstname', '')} {agent_obj.get('lastname', '')}".strip()
    if agent_name:
        agent_name = str(agent_name).strip()

    agent_email = payload.get("agent_email") or agent_obj.get("email")
    if agent_email:
        agent_email = str(agent_email).strip().lower()

    # Numbers
    external_number = (
        payload.get("external_number")
        or payload.get("contact_number")
        or payload.get("public_external")
        or payload.get("number")
        or cdr.get("public_external")
        or contact_obj.get("number")
    )
    if external_number:
        external_number = str(external_number).strip()

    internal_number = (
        payload.get("internal_number")
        or payload.get("public_internal")
        or cdr.get("public_internal")
    )
    if internal_number:
        internal_number = str(internal_number).strip()

    # Timestamps
    started_at = _parse_datetime_safely(
        payload.get("started_at") or cdr.get("started_at") or call_obj.get("started_at")
    )
    ended_at = _parse_datetime_safely(
        payload.get("ended_at") or cdr.get("ended_at") or call_obj.get("ended_at")
    )

    return {
        "call_id": call_id,
        "call_uuid": call_uuid,
        "talking_time": talking_time,
        "direction": direction,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agent_email": agent_email,
        "external_number": external_number,
        "internal_number": internal_number,
        "started_at": started_at,
        "ended_at": ended_at,
    }


def sanitize_payload_for_storage(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove any sensitive keys/tokens from payload before saving to DB."""
    sensitive_keys = {"token", "secret", "password", "key", "authorization", "api_key"}
    sanitized = {}
    for k, v in payload.items():
        if any(s in k.lower() for s in sensitive_keys):
            continue
        if isinstance(v, dict):
            sanitized[k] = sanitize_payload_for_storage(v)
        else:
            sanitized[k] = v
    return sanitized


async def handle_call_ended_webhook(
    db: AsyncSession,
    payload_data: Dict[str, Any],
    company_id: int = 1,
) -> Dict[str, Any]:
    """
    Ingests and processes a CloudTalk Call -> Ended webhook:
    1. Extracts and normalizes metadata.
    2. Enforces duration filter (> 15 seconds).
    3. Handles idempotency.
    4. Enqueues job and triggers background evaluation.
    """
    extracted = extract_webhook_fields(payload_data)
    call_id = extracted["call_id"]

    if not call_id:
        return {
            "ok": False,
            "status": "error",
            "message": "Missing call_id/cdr_id in webhook payload.",
        }

    # Resolve service record
    service = await get_cloudtalk_bot_service(db, company_id=company_id)
    service_id = service.service_id

    # Validate talking_time duration filter
    talking_time = extracted["talking_time"]
    if talking_time is None:
        logger.info(
            "talking_time not present in webhook for call_id=%s. Querying CloudTalk API metadata...",
            call_id,
        )
        try:
            ct_call = await CloudTalkService().get_call(call_id)
            if ct_call:
                talking_time = _parse_int_safely(ct_call.get("talking_time") or ct_call.get("billsec"))
                extracted["talking_time"] = talking_time
                if not extracted["direction"]:
                    extracted["direction"] = ct_call.get("type")
                if not extracted["agent_name"] and isinstance(ct_call.get("Agent"), dict):
                    ag = ct_call["Agent"]
                    extracted["agent_name"] = ag.get("fullname") or f"{ag.get('firstname', '')} {ag.get('lastname', '')}".strip()
                    extracted["agent_email"] = ag.get("email")
                    extracted["agent_id"] = str(ag.get("id"))
        except Exception as e_meta:
            logger.warning("Could not resolve metadata for call_id=%s from CloudTalk: %s", call_id, e_meta)

    # Rule: only evaluate calls with talking_time > 15 seconds
    if talking_time is not None and talking_time <= 15:
        logger.info(
            "Call skipped by duration filter: provider=cloudtalk, call_id=%s, talking_time=%ds <= 15s",
            call_id,
            talking_time,
        )
        # Check if job already exists
        stmt_exist = select(CloudTalkEvaluationJob).where(
            CloudTalkEvaluationJob.company_id == company_id,
            CloudTalkEvaluationJob.service_id == service_id,
            CloudTalkEvaluationJob.cloudtalk_call_id == call_id,
        )
        res_exist = await db.execute(stmt_exist)
        existing_job = res_exist.scalars().first()
        if not existing_job:
            skipped_job = CloudTalkEvaluationJob(
                company_id=company_id,
                service_id=service_id,
                cloudtalk_call_id=call_id,
                call_uuid=extracted["call_uuid"],
                talking_time=talking_time,
                direction=extracted["direction"],
                agent_id=extracted["agent_id"],
                agent_name=extracted["agent_name"],
                agent_email=extracted["agent_email"],
                external_number=extracted["external_number"],
                internal_number=extracted["internal_number"],
                started_at=extracted["started_at"],
                ended_at=extracted["ended_at"],
                webhook_payload=sanitize_payload_for_storage(payload_data),
                status="skipped",
                last_error_code="duration_too_short",
                last_error_message=f"Call duration ({talking_time}s) <= 15s",
                completed_at=datetime.now(timezone.utc),
            )
            db.add(skipped_job)
            await db.commit()
            await db.refresh(skipped_job)
            job_id = skipped_job.id
        else:
            job_id = existing_job.id

        return {
            "ok": True,
            "status": "skipped",
            "job_id": job_id,
            "call_id": call_id,
            "message": f"talking_time ({talking_time}s) <= 15s; call not evaluable.",
        }

    # Idempotency check: (company_id, service_id, cloudtalk_call_id)
    stmt = select(CloudTalkEvaluationJob).where(
        CloudTalkEvaluationJob.company_id == company_id,
        CloudTalkEvaluationJob.service_id == service_id,
        CloudTalkEvaluationJob.cloudtalk_call_id == call_id,
    )
    res = await db.execute(stmt)
    existing_job = res.scalars().first()

    now_utc = datetime.now(timezone.utc)

    if existing_job:
        logger.info(
            "CloudTalk duplicate webhook received: provider=cloudtalk, call_id=%s, existing_status=%s, job_id=%s",
            call_id,
            existing_job.status,
            existing_job.id,
        )
        if existing_job.status in ("completed", "skipped"):
            return {
                "ok": True,
                "status": existing_job.status,
                "job_id": existing_job.id,
                "call_id": call_id,
                "duplicate": True,
                "analysis_id": existing_job.analysis_id,
                "message": f"Call already {existing_job.status}.",
            }

        if existing_job.status in ("pending", "waiting_recording", "processing"):
            return {
                "ok": True,
                "status": existing_job.status,
                "job_id": existing_job.id,
                "call_id": call_id,
                "duplicate": True,
                "message": f"Call is already queued with status '{existing_job.status}'.",
            }

        # If job previously failed or was paused due to configuration_missing, allow webhook retry
        if existing_job.status in ("failed", "configuration_missing"):
            logger.info("Retrying existing %s job_id=%s for call_id=%s", existing_job.status, existing_job.id, call_id)
            existing_job.status = "pending"
            existing_job.attempt_count = 0
            existing_job.next_attempt_at = now_utc
            existing_job.last_error_code = None
            existing_job.last_error_message = None
            existing_job.updated_at = now_utc
            if talking_time is not None:
                existing_job.talking_time = talking_time
            await db.commit()
            await db.refresh(existing_job)

            # Spawn background execution
            asyncio.create_task(process_cloudtalk_job_background(existing_job.id))

            return {
                "ok": True,
                "status": "accepted",
                "job_id": existing_job.id,
                "call_id": call_id,
                "retrying": True,
                "message": "Job re-enqueued for evaluation.",
            }

    # Create new persistent job
    new_job = CloudTalkEvaluationJob(
        company_id=company_id,
        service_id=service_id,
        cloudtalk_call_id=call_id,
        call_uuid=extracted["call_uuid"],
        talking_time=talking_time,
        direction=extracted["direction"],
        agent_id=extracted["agent_id"],
        agent_name=extracted["agent_name"],
        agent_email=extracted["agent_email"],
        external_number=extracted["external_number"],
        internal_number=extracted["internal_number"],
        started_at=extracted["started_at"],
        ended_at=extracted["ended_at"],
        webhook_payload=sanitize_payload_for_storage(payload_data),
        status="pending",
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=now_utc,
        webhook_received_at=now_utc,
        created_at=now_utc,
        updated_at=now_utc,
    )
    db.add(new_job)
    await db.commit()
    await db.refresh(new_job)

    logger.info(
        "CloudTalk evaluation job enqueued: provider=cloudtalk, event=call_ended, call_id=%s, talking_time=%s, job_id=%s, status=pending",
        call_id,
        talking_time,
        new_job.id,
    )

    # Spawn background task for immediate execution without blocking the webhook response
    asyncio.create_task(process_cloudtalk_job_background(new_job.id))

    return {
        "ok": True,
        "status": "accepted",
        "job_id": new_job.id,
        "call_id": call_id,
        "duplicate": False,
        "message": "Call queued for automatic evaluation.",
    }


async def process_cloudtalk_job_background(job_id: int) -> None:
    """Entry point for running a job in a detached background task with its own DB session."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            await process_cloudtalk_job(session, job_id)
        except Exception as e:
            logger.error("Error in background worker for cloudtalk job_id=%s: %s", job_id, e, exc_info=True)


async def process_cloudtalk_job(db: AsyncSession, job_id: int) -> None:
    """
    Main evaluation worker:
    1. Resolves active prompt for Cloudtalk Bot.
       If missing, flags job as 'configuration_missing' (does NOT evaluate with Front prompt).
    2. Downloads recording with retry schedule ([3, 5, 10, 20, 40] seconds).
    3. Resolves agent metadata (webhook -> CloudTalk -> HubSpot -> bm_users).
    4. Evaluates call using multimodal Gemini audio engine.
    5. Saves analysis under Cloudtalk Bot service.
    """
    job = await db.get(CloudTalkEvaluationJob, job_id)
    if not job:
        logger.warning("CloudTalk job_id=%s not found.", job_id)
        return

    if job.status in ("completed", "skipped"):
        logger.info("CloudTalk job_id=%s already finished with status=%s.", job_id, job.status)
        return

    now_utc = datetime.now(timezone.utc)
    settings = get_settings()

    # ── 1. Resolve Active Audio Prompt for Cloudtalk Bot ──────────────────────
    prompt_dict = await get_active_prompt(
        db=db,
        prompt_type="audio",
        service_id=job.service_id,
        company_id=job.company_id,
    )

    if not prompt_dict or not prompt_dict.get("prompt"):
        logger.warning(
            "CloudTalk job %d paused: configuration_missing. No active audio prompt found for service_id=%d, company_id=%d.",
            job.id,
            job.service_id,
            job.company_id,
        )
        job.status = "configuration_missing"
        job.last_error_code = "configuration_missing"
        job.last_error_message = (
            f"No active audio prompt configured for service_id={job.service_id}. "
            "Please configure and activate an audio prompt in Speech BM before evaluating."
        )
        job.updated_at = now_utc
        await db.commit()
        return

    prompt_text = prompt_dict["prompt"]
    resolved_prompt_id = prompt_dict["prompt_id"]
    resolved_version_id = prompt_dict.get("prompt_version_id")

    # Fetch active criteria objects for defensive keys guard & criteria results
    from app.models.criteria import PromptCriterion
    c_stmt = select(PromptCriterion).where(
        PromptCriterion.prompt_id == resolved_prompt_id,
        PromptCriterion.is_active == True,
    )
    c_res = await db.execute(c_stmt)
    active_criteria = c_res.scalars().all()

    # ── 2. Download Call Recording from CloudTalk ──────────────────────────────
    cloudtalk_service = CloudTalkService()
    wav_bytes: Optional[bytes] = None

    try:
        wav_bytes = await cloudtalk_service.download_recording(job.cloudtalk_call_id)
    except AudioDownloadError as ade:
        is_not_ready = ade.status_code in (404, 429) or "no encontrada" in str(ade).lower() or "not ready" in str(ade).lower()
        job.attempt_count += 1
        job.updated_at = datetime.now(timezone.utc)

        if is_not_ready and job.attempt_count < job.max_attempts:
            delay_idx = min(job.attempt_count - 1, len(RECORDING_RETRY_DELAYS) - 1)
            retry_delay = RECORDING_RETRY_DELAYS[delay_idx]
            job.status = "waiting_recording"
            job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay)
            job.last_error_code = "waiting_recording"
            job.last_error_message = f"Attempt {job.attempt_count}/{job.max_attempts}: recording not ready, retry in {retry_delay}s."
            await db.commit()

            logger.info(
                "CloudTalk recording not ready: provider=cloudtalk, call_id=%s, attempt=%d/%d, recording_ready=false, retry_in=%ds",
                job.cloudtalk_call_id,
                job.attempt_count,
                job.max_attempts,
                retry_delay,
            )
            return
        else:
            job.status = "failed"
            job.last_error_code = "recording_not_available" if is_not_ready else "recording_download_error"
            job.last_error_message = f"Recording failed after {job.attempt_count} attempts: {ade.message}"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()

            logger.error(
                "CloudTalk recording download failed: provider=cloudtalk, call_id=%s, attempt=%d/%d, recording_ready=false, error=%s",
                job.cloudtalk_call_id,
                job.attempt_count,
                job.max_attempts,
                ade.message,
            )
            return
    except Exception as e_down:
        job.attempt_count += 1
        job.status = "failed"
        job.last_error_code = "recording_download_error"
        job.last_error_message = f"Unexpected download error: {str(e_down)[:400]}"
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        await db.commit()
        logger.error("CloudTalk unexpected recording download error for call_id=%s: %s", job.cloudtalk_call_id, e_down)
        return

    logger.info(
        "CloudTalk recording ready: provider=cloudtalk, call_id=%s, attempt=%d, recording_ready=true, size_bytes=%d",
        job.cloudtalk_call_id,
        job.attempt_count,
        len(wav_bytes),
    )

    # ── 3. Resolve Agent Association & Optional HubSpot Enrichment ────────────
    job.status = "processing"
    job.updated_at = datetime.now(timezone.utc)
    await db.commit()

    hubspot_url: Optional[str] = None
    hubspot_owner_id: Optional[str] = None
    agente_telefonico: Optional[str] = job.agent_name
    hs_data: Optional[Dict[str, Any]] = None

    # Optional non-blocking HubSpot enrichment
    try:
        from app.services.hubspot_service import HubSpotService
        hs = HubSpotService(is_demo=False)
        hs_call = await hs.search_call_by_external_id(job.cloudtalk_call_id, is_demo=False)
        if hs_call:
            hubspot_url = hs_call.get("hubspot_url")
            hubspot_owner_id = hs_call.get("hubspot_owner_id")
            if not agente_telefonico and hs_call.get("agente_telefonico"):
                agente_telefonico = hs_call.get("agente_telefonico")
            hs_data = hs_call
            logger.info("Call %s enriched via HubSpot: owner_id=%s", job.cloudtalk_call_id, hubspot_owner_id)
    except Exception as e_hs:
        logger.info("Non-blocking HubSpot lookup skipped or not found for call_id=%s: %s", job.cloudtalk_call_id, e_hs)

    # Agent matching priority:
    # 1. Webhook email
    # 2. CloudTalk Agent email
    # 3. bm_users lookup by email
    # 4. resolve_owner_id_by_email
    if not hubspot_owner_id and job.agent_email:
        hubspot_owner_id = resolve_owner_id_by_email(job.agent_email)

    if not hubspot_owner_id and job.agent_email:
        u_stmt = select(User).where(
            User.company_id == job.company_id,
            func.lower(User.email) == job.agent_email.lower(),
        )
        u_res = await db.execute(u_stmt)
        user_match = u_res.scalars().first()
        if user_match:
            hubspot_owner_id = user_match.hubspot_owner_id
            if not agente_telefonico:
                agente_telefonico = user_match.name

    if not agente_telefonico:
        if hubspot_owner_id and hubspot_owner_id in OWNER_TO_NAME:
            agente_telefonico = OWNER_TO_NAME[hubspot_owner_id]
        elif job.agent_id:
            agente_telefonico = f"CloudTalk Agent {job.agent_id}"
        else:
            agente_telefonico = "CloudTalk Agent"

    # ── 4. Call Multimodal Gemini AI Analysis ─────────────────────────────────
    try:
        raw_response = await analyze_audio_bytes(
            audio_bytes=wav_bytes,
            prompt_text=prompt_text,
            audio_format="wav",
            company_id=job.company_id,
            service_id=job.service_id,
            module="cloudtalk_bot",
            call_id=str(job.cloudtalk_call_id),
            audio_duration_seconds=float(job.talking_time) if job.talking_time else None,
        )
    except Exception as e_ai:
        job.status = "failed"
        job.last_error_code = "ai_evaluation_error"
        job.last_error_message = f"AI inference error: {str(e_ai)[:500]}"
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        await db.commit()
        logger.error("AI inference error on cloudtalk job %d (call_id=%s): %s", job.id, job.cloudtalk_call_id, e_ai, exc_info=True)
        return

    # ── 5. Parse & Sanitize Result JSON ───────────────────────────────────────
    parsed = safe_parse_json(raw_response)
    if not parsed or not isinstance(parsed, dict):
        logger.error("Failed to parse AI JSON response for call_id=%s. Raw: %r", job.cloudtalk_call_id, str(raw_response)[:200])
        job.status = "failed"
        job.last_error_code = "ai_invalid_json"
        job.last_error_message = "AI model did not return a valid JSON object."
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return

    # Dynamic typology guard
    tipo_llamada = parsed.get("tipo_llamada")
    from app.models.typologies import Typology
    t_stmt = select(Typology.typology_key).where(
        Typology.service_id == job.service_id,
        Typology.is_active == True,
    )
    t_res = await db.execute(t_stmt)
    active_keys = set(t_res.scalars().all())
    if not active_keys:
        active_keys = {"cita", "confirmacion", "cancelacion", "reagendo", "falta", "otros"}

    if tipo_llamada not in active_keys:
        logger.info(
            "tipo_llamada '%s' not active for service_id=%d; defaulting to 'otros'.",
            tipo_llamada,
            job.service_id,
        )
        parsed["tipo_llamada"] = "otros"

    # Defensive keys guard for prompt criteria
    if active_criteria:
        for c in active_criteria:
            if c.criterion_key and c.criterion_key not in parsed:
                parsed[c.criterion_key] = False
            if c.feed_key and c.feed_key not in parsed:
                parsed[c.feed_key] = None

    # ── 6. Persist Analysis Record ────────────────────────────────────────────
    call_metadata = {
        "call_id": str(job.cloudtalk_call_id),
        "hubspot_url": hubspot_url,
        "call_direction": job.direction,
        "call_timestamp": job.started_at,
        "agente_telefonico": agente_telefonico,
        "hubspot_owner_id": hubspot_owner_id,
        "duracion_llamada": job.talking_time,
        "source": "cloudtalk_webhook",
        "service_id": job.service_id,
        "company_id": job.company_id,
        "run_ts": datetime.now(timezone.utc),
        "fecha_eval": datetime.now(timezone.utc),
    }

    try:
        analysis_record = await save_analysis(
            db=db,
            analysis_type="audio",
            call_metadata=call_metadata,
            prompt_metadata={
                "prompt_id": resolved_prompt_id,
                "prompt_version_id": resolved_version_id,
            },
            model_metadata={
                "model_provider": settings.ai_provider,
                "model_name": settings.gemini_analysis_model if settings.ai_provider == "gemini" else "gpt-audio-1.5",
            },
            result_json=parsed,
            payload={
                "cloudtalk_id": str(job.cloudtalk_call_id),
                "call_uuid": job.call_uuid,
                "direction": job.direction,
                "talking_time": job.talking_time,
                "agent_id": job.agent_id,
                "number": job.external_number,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "ended_at": job.ended_at.isoformat() if job.ended_at else None,
                "webhook_received_at": job.webhook_received_at.isoformat() if job.webhook_received_at else None,
                "hubspot_data": hs_data,
                "raw_response": raw_response if isinstance(raw_response, str) else None,
            },
            transcription=None,
            active_criteria=active_criteria,
        )
    except Exception as e_save:
        job.status = "failed"
        job.last_error_code = "analysis_save_error"
        job.last_error_message = f"Error saving analysis record: {str(e_save)[:500]}"
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        await db.commit()
        logger.error("Error saving analysis for cloudtalk job %d: %s", job.id, e_save, exc_info=True)
        return

    # ── 7. Mark Job Completed ─────────────────────────────────────────────────
    job.status = "completed"
    job.analysis_id = analysis_record.analysis_id
    job.completed_at = datetime.now(timezone.utc)
    job.updated_at = datetime.now(timezone.utc)
    job.last_error_code = None
    job.last_error_message = None
    await db.commit()

    logger.info(
        "CloudTalk automatic evaluation completed: provider=cloudtalk, call_id=%s, job_id=%s, analysis_id=%d, status=completed",
        job.cloudtalk_call_id,
        job.id,
        job.analysis_id,
    )


async def run_due_cloudtalk_jobs(db: AsyncSession, limit: int = 10) -> int:
    """
    Polls and processes due jobs in 'pending' or 'waiting_recording' status.
    Called periodically by the background scheduler.
    """
    now_utc = datetime.now(timezone.utc)
    stmt = (
        select(CloudTalkEvaluationJob)
        .where(
            CloudTalkEvaluationJob.status.in_(["pending", "waiting_recording"]),
            and_(
                CloudTalkEvaluationJob.next_attempt_at != None,  # noqa: E711
                CloudTalkEvaluationJob.next_attempt_at <= now_utc,
            ),
        )
        .order_by(CloudTalkEvaluationJob.next_attempt_at.asc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    due_jobs = res.scalars().all()

    processed_count = 0
    for job in due_jobs:
        try:
            await process_cloudtalk_job(db, job.id)
            processed_count += 1
        except Exception as e:
            logger.error("Error processing due cloudtalk job_id=%s: %s", job.id, e, exc_info=True)

    return processed_count
