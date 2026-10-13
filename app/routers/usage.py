"""
FastAPI Router for FinOps & AI Usage Reporting (Phase 4).
Exposes endpoints for aggregated token consumption, cost attributions,
dimensional breakdowns (company, service, module, model), and timeseries trends.
"""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_context import TenantContext
from app.dependencies import get_db, require_finops_access
from app.schemas.usage import (
    CompanyUsageResponse,
    ModelUsageResponse,
    ModuleUsageResponse,
    ServiceUsageResponse,
    UsageSummaryResponse,
    UsageTimeseriesResponse,
)
from app.services.usage_service import (
    get_usage_by_company,
    get_usage_by_model,
    get_usage_by_module,
    get_usage_by_service,
    get_usage_summary,
    get_usage_timeseries,
)

router = APIRouter(prefix="/bm/usage", tags=["FinOps & AI Usage"])


@router.get("/summary", response_model=UsageSummaryResponse)
async def get_summary_endpoint(
    period: Optional[str] = Query(None, description="Period shortcut: day, week, month, quarter, custom"),
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    company_id: Optional[int] = Query(None, description="Filter by company ID"),
    service_id: Optional[int] = Query(None, description="Filter by service ID"),
    module: Optional[str] = Query(None, description="Filter by technical module"),
    model: Optional[str] = Query(None, description="Filter by model name"),
    pricing_status: Optional[str] = Query(None, description="Filter by pricing status (priced, partial, unpriced)"),
    db: AsyncSession = Depends(get_db),
    context: TenantContext = Depends(require_finops_access),
):
    """Retrieve overall FinOps summary metrics, tokens, audio duration, and costs for the requested period."""
    return await get_usage_summary(
        session=db,
        context=context,
        period=period,
        date_from=date_from,
        date_to=date_to,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )


@router.get("/timeseries", response_model=UsageTimeseriesResponse)
async def get_timeseries_endpoint(
    period: Optional[str] = Query(None, description="Period shortcut: day, week, month, quarter, custom"),
    granularity: Optional[str] = Query("auto", description="auto, hour, day, week, month, quarter"),
    group_by: Optional[str] = Query("none", description="none, company, service, module, functional_area, model"),
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    company_id: Optional[int] = Query(None, description="Filter by company ID"),
    service_id: Optional[int] = Query(None, description="Filter by service ID"),
    module: Optional[str] = Query(None, description="Filter by technical module"),
    model: Optional[str] = Query(None, description="Filter by model name"),
    pricing_status: Optional[str] = Query(None, description="Filter by pricing status"),
    db: AsyncSession = Depends(get_db),
    context: TenantContext = Depends(require_finops_access),
):
    """Retrieve time-bucketed consumption and cost trends, optionally grouped by dimension."""
    return await get_usage_timeseries(
        session=db,
        context=context,
        period=period,
        granularity=granularity,
        group_by=group_by,
        date_from=date_from,
        date_to=date_to,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )


@router.get("/by-company", response_model=CompanyUsageResponse)
async def get_by_company_endpoint(
    period: Optional[str] = Query(None, description="Period shortcut: day, week, month, quarter, custom"),
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    company_id: Optional[int] = Query(None, description="Filter by company ID"),
    service_id: Optional[int] = Query(None, description="Filter by service ID"),
    module: Optional[str] = Query(None, description="Filter by technical module"),
    model: Optional[str] = Query(None, description="Filter by model name"),
    pricing_status: Optional[str] = Query(None, description="Filter by pricing status"),
    db: AsyncSession = Depends(get_db),
    context: TenantContext = Depends(require_finops_access),
):
    """Retrieve AI usage and cost breakdown by company with percentage share."""
    return await get_usage_by_company(
        session=db,
        context=context,
        period=period,
        date_from=date_from,
        date_to=date_to,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )


@router.get("/by-service", response_model=ServiceUsageResponse)
async def get_by_service_endpoint(
    period: Optional[str] = Query(None, description="Period shortcut: day, week, month, quarter, custom"),
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    company_id: Optional[int] = Query(None, description="Filter by company ID"),
    service_id: Optional[int] = Query(None, description="Filter by service ID"),
    module: Optional[str] = Query(None, description="Filter by technical module"),
    model: Optional[str] = Query(None, description="Filter by model name"),
    pricing_status: Optional[str] = Query(None, description="Filter by pricing status"),
    db: AsyncSession = Depends(get_db),
    context: TenantContext = Depends(require_finops_access),
):
    """Retrieve AI usage and cost breakdown by service with percentage share."""
    return await get_usage_by_service(
        session=db,
        context=context,
        period=period,
        date_from=date_from,
        date_to=date_to,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )


@router.get("/by-module", response_model=ModuleUsageResponse)
async def get_by_module_endpoint(
    period: Optional[str] = Query(None, description="Period shortcut: day, week, month, quarter, custom"),
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    company_id: Optional[int] = Query(None, description="Filter by company ID"),
    service_id: Optional[int] = Query(None, description="Filter by service ID"),
    module: Optional[str] = Query(None, description="Filter by technical module"),
    model: Optional[str] = Query(None, description="Filter by model name"),
    pricing_status: Optional[str] = Query(None, description="Filter by pricing status"),
    db: AsyncSession = Depends(get_db),
    context: TenantContext = Depends(require_finops_access),
):
    """Retrieve AI usage and cost breakdown by technical module and functional area."""
    return await get_usage_by_module(
        session=db,
        context=context,
        period=period,
        date_from=date_from,
        date_to=date_to,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )


@router.get("/by-model", response_model=ModelUsageResponse)
async def get_by_model_endpoint(
    period: Optional[str] = Query(None, description="Period shortcut: day, week, month, quarter, custom"),
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD or ISO) in Europe/Madrid"),
    company_id: Optional[int] = Query(None, description="Filter by company ID"),
    service_id: Optional[int] = Query(None, description="Filter by service ID"),
    module: Optional[str] = Query(None, description="Filter by technical module"),
    model: Optional[str] = Query(None, description="Filter by model name"),
    pricing_status: Optional[str] = Query(None, description="Filter by pricing status"),
    db: AsyncSession = Depends(get_db),
    context: TenantContext = Depends(require_finops_access),
):
    """Retrieve AI usage and cost breakdown by model with percentage share."""
    return await get_usage_by_model(
        session=db,
        context=context,
        period=period,
        date_from=date_from,
        date_to=date_to,
        company_id=company_id,
        service_id=service_id,
        module=module,
        model=model,
        pricing_status=pricing_status,
    )
