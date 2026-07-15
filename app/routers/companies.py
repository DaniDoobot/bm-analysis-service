import logging
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import (
    get_db,
    get_tenant_context,
    require_super_admin,
    RequireCompanyAccess,
)
from app.models.companies import Company
from app.core.tenant_context import TenantContext
from app.schemas.multitenancy import CompanyResponse, CompanyCreate, CompanyUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Multi-tenancy Companies"])


@router.get("/companies", response_model=List[CompanyResponse])
async def list_companies(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """List all companies accessible by the authenticated user's context."""
    if context.is_super_admin:
        stmt = select(Company)
    else:
        if not context.allowed_company_ids:
            return []
        stmt = select(Company).where(Company.company_id.in_(context.allowed_company_ids))
    
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.get("/companies/{company_id}", response_model=CompanyResponse)
async def get_company(
    company_id: int,
    context: Annotated[TenantContext, Depends(RequireCompanyAccess())],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Retrieve details of a specific company by ID."""
    stmt = select(Company).where(Company.company_id == company_id)
    res = await db.execute(stmt)
    company = res.scalar()
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Empresa no encontrada.")
    return company


@router.post("/companies", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
async def create_company(
    payload: CompanyCreate,
    context: Annotated[TenantContext, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Create a new company (Super Admin only)."""
    dup_stmt = select(Company).where(
        (Company.company_key == payload.company_key) | 
        (Company.company_name == payload.company_name)
    )
    dup_res = await db.execute(dup_stmt)
    if dup_res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Ya existe una empresa con ese nombre o clave única."
        )
    
    company = Company(
        company_name=payload.company_name,
        company_key=payload.company_key,
        is_active=payload.is_active
    )
    db.add(company)
    await db.commit()
    await db.refresh(company)
    return company


@router.patch("/companies/{company_id}", response_model=CompanyResponse)
async def update_company(
    company_id: int,
    payload: CompanyUpdate,
    context: Annotated[TenantContext, Depends(require_super_admin)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Update details of an existing company (Super Admin only)."""
    stmt = select(Company).where(Company.company_id == company_id)
    res = await db.execute(stmt)
    company = res.scalar()
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Empresa no encontrada.")
    
    if payload.company_name is not None:
        company.company_name = payload.company_name
    if payload.company_key is not None:
        dup_stmt = select(Company).where(Company.company_key == payload.company_key)
        dup_res = await db.execute(dup_stmt)
        existing = dup_res.scalar()
        if existing and existing.company_id != company_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, 
                detail="Esa clave de empresa ya está en uso."
            )
        company.company_key = payload.company_key
    if payload.is_active is not None:
        company.is_active = payload.is_active
        
    await db.commit()
    await db.refresh(company)
    return company
