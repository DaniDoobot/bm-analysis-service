"""
Service resolver utilities for normalizing service parameters (ID, key, or slug name).
"""
import logging
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.models.services import Service

logger = logging.getLogger(__name__)


async def resolve_service_id(
    db: AsyncSession,
    service_id: int | None = None,
    service_key: str | None = None,
    service_param: str | int | None = None,
    company_ids: list[int] | None = None
) -> tuple[int | None, str | None]:
    """
    Resolves (service_id, service_key) from any combination of input parameters.
    If service_param is passed as string 'front', resolves it against bm_services.
    Returns (resolved_service_id, resolved_service_key).
    If a string service is provided but cannot be resolved, raises 422 Unprocessable Entity.
    """
    # 1. Direct integer service_id
    if service_id is not None:
        stmt = select(Service).where(Service.service_id == service_id)
        if company_ids:
            stmt = stmt.where(Service.company_id.in_(company_ids))
        res = await db.execute(stmt)
        svc = res.scalar_one_or_none()
        if svc:
            return svc.service_id, svc.service_key
        return service_id, service_key

    # 2. Extract raw string param from service_param or service_key
    raw_str = None
    if isinstance(service_param, int):
        return await resolve_service_id(db, service_id=service_param, company_ids=company_ids)
    elif isinstance(service_param, str) and service_param.strip():
        raw_str = service_param.strip()
    elif service_key and service_key.strip():
        raw_str = service_key.strip()

    if not raw_str:
        return None, None

    # Try numeric string check e.g. "1"
    if raw_str.isdigit():
        return await resolve_service_id(db, service_id=int(raw_str), company_ids=company_ids)

    # 3. Match against service_key or normalized service_name
    raw_lower = raw_str.lower()
    raw_clean = raw_lower.replace("-", "").replace("_", "").replace(" ", "")

    stmt = select(Service).where(
        or_(
            func.lower(Service.service_key) == raw_lower,
            func.lower(Service.service_name) == raw_lower,
            func.lower(func.replace(Service.service_key, "_", "-")) == raw_lower,
            func.lower(func.replace(Service.service_key, "-", "_")) == raw_lower,
            func.lower(func.replace(Service.service_name, " ", "-")) == raw_lower,
            func.lower(func.replace(Service.service_name, " ", "_")) == raw_lower,
            func.lower(func.replace(func.replace(func.replace(Service.service_key, "-", ""), "_", ""), " ", "")) == raw_clean,
            func.lower(func.replace(func.replace(func.replace(Service.service_name, "-", ""), "_", ""), " ", "")) == raw_clean,
        )
    )
    if company_ids:
        stmt = stmt.where(Service.company_id.in_(company_ids))

    res = await db.execute(stmt)
    svc = res.scalar_one_or_none()
    if svc:
        return svc.service_id, svc.service_key

    # Fallback partial match if unique
    stmt_like = select(Service).where(
        or_(
            func.lower(Service.service_name).like(f"%{raw_lower}%"),
            func.lower(Service.service_key).like(f"%{raw_lower}%")
        )
    )
    if company_ids:
        stmt_like = stmt_like.where(Service.company_id.in_(company_ids))
    svcs = (await db.execute(stmt_like)).scalars().all()
    if len(svcs) == 1:
        return svcs[0].service_id, svcs[0].service_key

    logger.warning("[resolve_service_id] Could not resolve service string '%s'", raw_str)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Service '{raw_str}' not found or invalid."
    )
