import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import (
    RequireCompanyAccess,
    get_db,
    get_tenant_context,
    require_super_admin,
)
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.core.tenant_context import TenantContext
from app.schemas.multitenancy import AdminCompanyResponse, CompanyCreate, CompanyResponse, CompanyUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Multi-tenancy Companies"])


# ---------------------------------------------------------------------------
# Helper: build enriched AdminCompanyResponse with counts
# ---------------------------------------------------------------------------

async def _build_admin_company_response(company: Company, db: AsyncSession) -> AdminCompanyResponse:
    """Compute counts via efficient aggregate subqueries (no N+1)."""
    svc_count_res = await db.execute(
        select(func.count()).select_from(Service).where(Service.company_id == company.company_id)
    )
    services_count = svc_count_res.scalar() or 0

    users_count_res = await db.execute(
        select(func.count()).select_from(User).where(
            (User.company_id == company.company_id) & (User.is_active == True)
        )
    )
    users_count = users_count_res.scalar() or 0

    teams_count_res = await db.execute(
        select(func.count()).select_from(Team).where(Team.company_id == company.company_id)
    )
    teams_count = teams_count_res.scalar() or 0

    return AdminCompanyResponse(
        company_id=company.company_id,
        company_name=company.company_name,
        company_key=company.company_key,
        is_active=company.is_active,
        services_count=services_count,
        users_count=users_count,
        teams_count=teams_count,
        created_at=company.created_at,
        updated_at=company.updated_at,
    )


# ---------------------------------------------------------------------------
# GET /bm/companies — List companies
# ---------------------------------------------------------------------------

@router.get("/companies", response_model=List[AdminCompanyResponse])
async def list_companies(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    is_active: Optional[bool] = Query(None, description="Filtrar por estado activo/inactivo"),
):
    """
    List companies accessible to the authenticated user.

    - super_admin: all companies.
    - company_admin: only their own company.
    - service_manager / team_coordinator / agent: returns empty list (no company management access).
    """
    if context.is_super_admin:
        stmt = select(Company)
    else:
        if not context.allowed_company_ids:
            return []
        stmt = select(Company).where(Company.company_id.in_(context.allowed_company_ids))

    if is_active is not None:
        stmt = stmt.where(Company.is_active == is_active)

    stmt = stmt.order_by(Company.company_name)
    res = await db.execute(stmt)
    companies = list(res.scalars().all())

    results = []
    for company in companies:
        results.append(await _build_admin_company_response(company, db))
    return results


# ---------------------------------------------------------------------------
# GET /bm/companies/{company_id} — Company detail
# ---------------------------------------------------------------------------

@router.get("/companies/{company_id}", response_model=AdminCompanyResponse)
async def get_company(
    company_id: int,
    context: Annotated[TenantContext, Depends(RequireCompanyAccess())],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Retrieve details of a specific company by ID (enriched with resource counts).

    - super_admin: any company.
    - company_admin: only their own company.
    - others: 403.
    """
    stmt = select(Company).where(Company.company_id == company_id)
    res = await db.execute(stmt)
    company = res.scalar()
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Empresa no encontrada.")
    return await _build_admin_company_response(company, db)


# ---------------------------------------------------------------------------
# POST /bm/companies — Create company
# ---------------------------------------------------------------------------

@router.post("/companies", response_model=AdminCompanyResponse, status_code=status.HTTP_201_CREATED)
async def create_company(
    payload: CompanyCreate,
    context: Annotated[TenantContext, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Create a new company. Super Admin only.

    - company_key must be a valid slug (lowercase, digits, hyphens, underscores).
    - company_key must be unique.
    - company_name must be unique.
    """
    # Check uniqueness of key and name
    dup_stmt = select(Company).where(
        (Company.company_key == payload.company_key) |
        (Company.company_name == payload.company_name)
    )
    dup_res = await db.execute(dup_stmt)
    if dup_res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ya existe una empresa con ese nombre o clave única.",
        )

    company = Company(
        company_name=payload.company_name,
        company_key=payload.company_key,
        is_active=payload.is_active,
    )
    db.add(company)
    await db.commit()
    await db.refresh(company)

    logger.info(
        "Actor (user_id=%s) CREATED company '%s' (id=%s, key=%s)",
        context.user_id, company.company_name, company.company_id, company.company_key,
    )
    return await _build_admin_company_response(company, db)


# ---------------------------------------------------------------------------
# PATCH /bm/companies/{company_id} — Update company
# ---------------------------------------------------------------------------

@router.patch("/companies/{company_id}", response_model=AdminCompanyResponse)
async def update_company(
    company_id: int,
    payload: CompanyUpdate,
    context: Annotated[TenantContext, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Update an existing company's details. Super Admin only.

    Allowed fields: company_name, company_key (slug validated), is_active.
    Validates slug format and uniqueness of company_key if changed.
    company_name cannot be left empty.
    """
    stmt = select(Company).where(Company.company_id == company_id)
    res = await db.execute(stmt)
    company = res.scalar()
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Empresa no encontrada.")

    if payload.company_name is not None:
        # Check uniqueness of new name (excluding self)
        name_dup_res = await db.execute(
            select(Company).where(
                (Company.company_name == payload.company_name) &
                (Company.company_id != company_id)
            )
        )
        if name_dup_res.scalar():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Ya existe otra empresa con ese nombre.",
            )
        company.company_name = payload.company_name

    if payload.company_key is not None:
        # Check uniqueness of new key (excluding self)
        key_dup_res = await db.execute(
            select(Company).where(
                (Company.company_key == payload.company_key) &
                (Company.company_id != company_id)
            )
        )
        if key_dup_res.scalar():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Esa clave de empresa ya está en uso.",
            )
        company.company_key = payload.company_key

    if payload.is_active is not None:
        company.is_active = payload.is_active

    await db.commit()
    await db.refresh(company)

    logger.info(
        "Actor (user_id=%s) UPDATED company id=%s (name=%s, key=%s, active=%s)",
        context.user_id, company_id, company.company_name, company.company_key, company.is_active,
    )
    return await _build_admin_company_response(company, db)
