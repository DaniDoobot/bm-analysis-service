"""
Central, fail-closed guard for external side effects and integrations.
Protects against accidental external interactions (HubSpot writes/reads,
automation schedulers, mass job runs, training cycles) by demo tenants
(Company.is_demo == True) while preserving explicit interactive exceptions
(e.g., interactive manual Trainer simulations).
"""
import logging
import time
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class IntegrationType(str, Enum):
    HUBSPOT_WRITE = "hubspot_write"
    HUBSPOT_READ = "hubspot_read"
    AUTOMATION = "automation"
    MASS_SCHEDULER = "mass_scheduler"
    TRAINING_SCHEDULER = "training_scheduler"
    TRAINER_INTERACTIVE = "trainer_interactive"
    TWILIO_INTERACTIVE = "twilio_interactive"
    TWILIO_BACKGROUND = "twilio_background"
    GENERIC = "generic"


# Actions explicitly allowed for demo tenants (e.g., interactive manual demos)
DEMO_ALLOWED_INTEGRATIONS: frozenset[str] = frozenset({
    IntegrationType.TRAINER_INTERACTIVE.value,
    IntegrationType.TWILIO_INTERACTIVE.value,
})

# Throttling dictionary: (company_id, service_id, integration, reason) -> last_logged_timestamp
_LOG_THROTTLE: dict[tuple, float] = {}
_LOG_THROTTLE_INTERVAL_SECONDS: float = 60.0


def log_side_effect_blocked(
    company_id: int | None,
    service_id: int | None,
    integration: str,
    reason: str = "demo_tenant_blocked",
    extra_info: str | None = None,
) -> None:
    """
    Structured, sanitized logging for blocked external side effects.
    Guarantees no sensitive data (tokens, PINs, passwords) is ever logged.
    Includes throttling to avoid spamming logs across recurring scheduler ticks.
    """
    key = (company_id, service_id, integration, reason)
    now = time.monotonic()
    last_logged = _LOG_THROTTLE.get(key, 0.0)

    is_throttled = (now - last_logged) < _LOG_THROTTLE_INTERVAL_SECONDS

    msg = (
        f"[side_effects_guard] BLOCKED: reason={reason} "
        f"integration={integration} company_id={company_id} service_id={service_id}"
    )
    if extra_info:
        msg += f" info={extra_info}"

    if not is_throttled:
        _LOG_THROTTLE[key] = now
        logger.warning(msg)
    else:
        logger.debug("%s (throttled)", msg)


async def resolve_company_demo_status(
    db: AsyncSession,
    *,
    company_id: int | None = None,
    service_id: int | None = None,
) -> tuple[int | None, bool | None]:
    """
    Safely resolves (company_id, is_demo) from company_id or service_id.
    Returns (resolved_company_id, is_demo).
    If company cannot be resolved, returns (None, None).
    """
    from app.models.companies import Company
    from app.models.services import Service

    # 1. Resolve via direct company_id
    if company_id is not None:
        try:
            stmt = select(Company.company_id, Company.is_demo).where(Company.company_id == company_id)
            res = await db.execute(stmt)
            row = res.first()
            if row is not None:
                return row[0], bool(row[1])
            return company_id, None
        except Exception as e:
            logger.error("[side_effects_guard] Error resolving company_id %s: %s", company_id, e)
            return company_id, None

    # 2. Resolve via service_id -> Service.company_id -> Company.is_demo
    if service_id is not None:
        try:
            stmt = (
                select(Company.company_id, Company.is_demo)
                .join(Service, Service.company_id == Company.company_id)
                .where(Service.service_id == service_id)
            )
            res = await db.execute(stmt)
            row = res.first()
            if row is not None:
                return row[0], bool(row[1])
            return None, None
        except Exception as e:
            logger.error("[side_effects_guard] Error resolving service_id %s: %s", service_id, e)
            return None, None

    return None, None


async def is_external_side_effect_allowed(
    db: AsyncSession | None,
    *,
    company_id: int | None = None,
    service_id: int | None = None,
    integration: str = "generic",
    is_demo: bool | None = None,
) -> bool:
    """
    Centralized, fail-closed gate for external side effects and integrations.

    Rules:
    A) Resolves Company from company_id or service_id (or accepts pre-resolved is_demo).
    B) If Company cannot be reliably resolved -> False (FAIL CLOSED).
    C) If Company.is_demo is True:
       - Returns True ONLY IF integration is in DEMO_ALLOWED_INTEGRATIONS (e.g. trainer_interactive).
       - Returns False for all background, automated, or external actions.
    D) If Company.is_demo is False:
       - Preserves existing integration rules (e.g. HubSpot service containment for Front vs EXPAC).
       - Returns True for allowed operations.
    E) Prevents N+1 queries by accepting pre-resolved `is_demo`.
    """
    integration_norm = str(integration).strip().lower()
    resolved_company_id = company_id

    # 1. Resolve is_demo if not provided
    if is_demo is None:
        if db is None:
            log_side_effect_blocked(
                company_id, service_id, integration_norm,
                reason="fail_closed_no_db_session"
            )
            return False

        resolved_cid, resolved_is_demo = await resolve_company_demo_status(
            db, company_id=company_id, service_id=service_id
        )
        if resolved_cid is not None:
            resolved_company_id = resolved_cid
        is_demo = resolved_is_demo

    # 2. Fail closed if is_demo could not be resolved
    if is_demo is None:
        log_side_effect_blocked(
            resolved_company_id, service_id, integration_norm,
            reason="company_unresolved_fail_closed"
        )
        return False

    # 3. Handle Demo Tenants (is_demo == True)
    if is_demo is True:
        # Check for explicit interactive demo permission
        if integration_norm in DEMO_ALLOWED_INTEGRATIONS:
            return True

        # All other side effects blocked for demo tenants
        log_side_effect_blocked(
            resolved_company_id, service_id, integration_norm,
            reason="demo_tenant_blocked"
        )
        return False

    # 4. Handle Real Tenants (is_demo == False)
    # Check integration-specific containment rules:
    if integration_norm in (
        IntegrationType.HUBSPOT_WRITE.value,
        "hubspot_alarm_ticket",
        "hubspot_rem_ticket",
    ):
        # Compose with existing HubSpot service containment
        from app.services.hubspot_service import is_hubspot_side_effect_allowed
        if not is_hubspot_side_effect_allowed(service_id):
            log_side_effect_blocked(
                resolved_company_id, service_id, integration_norm,
                reason="hubspot_service_containment_blocked"
            )
            return False

    return True
