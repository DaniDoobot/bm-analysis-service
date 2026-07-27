"""Service for resolving and managing company branding configurations."""
import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.companies import Company
from app.core.tenant_context import TenantContext
from app.schemas.multitenancy import CompanyBrandingResponse, CompanyBrandingUpdate

logger = logging.getLogger(__name__)


class CompanyBrandingService:
    @staticmethod
    def get_global_branding() -> CompanyBrandingResponse:
        """Return neutral global branding for super_admin or global context."""
        return CompanyBrandingResponse(
            company_id=None,
            company_name=None,
            brand_name="Doobot.ai_",
            brand_short_name="Doobot",
            logo_url=None,
            logo_dark_url=None,
            favicon_url=None,
            primary_color=None,
            secondary_color=None,
            accent_color=None,
            login_background_url=None,
            app_variant="global",
            dashboard_variant="global",
            sector=None,
            custom_welcome_title="Bienvenido a Doobot.ai_",
            custom_welcome_subtitle="Plataforma global de análisis conversacional",
            is_global_context=True,
        )

    @classmethod
    def build_branding_response(cls, company: Company) -> CompanyBrandingResponse:
        """Build a CompanyBrandingResponse from a Company model instance with safe fallbacks."""
        is_boston = False
        if company.company_name and "boston" in company.company_name.lower():
            is_boston = True
        elif company.company_key and "boston" in company.company_key.lower():
            is_boston = True

        brand_name = company.brand_name or company.company_name
        brand_short_name = company.brand_short_name or brand_name
        app_variant = company.app_variant or ("boston_medical" if is_boston else "default")
        dashboard_variant = company.dashboard_variant or ("boston_medical" if is_boston else "default")
        sector = company.sector or ("healthcare" if is_boston else None)

        return CompanyBrandingResponse(
            company_id=company.company_id,
            company_name=company.company_name,
            brand_name=brand_name,
            brand_short_name=brand_short_name,
            logo_url=company.logo_url,
            logo_dark_url=company.logo_dark_url,
            favicon_url=company.favicon_url,
            primary_color=company.primary_color,
            secondary_color=company.secondary_color,
            accent_color=company.accent_color,
            login_background_url=company.login_background_url,
            app_variant=app_variant,
            dashboard_variant=dashboard_variant,
            sector=sector,
            custom_welcome_title=company.custom_welcome_title,
            custom_welcome_subtitle=company.custom_welcome_subtitle,
            is_global_context=False,
        )

    @classmethod
    async def get_branding_for_user(
        cls, db: AsyncSession, context: TenantContext
    ) -> CompanyBrandingResponse:
        """Resolve branding response for the current user's tenant context."""
        if context.company_id is not None:
            stmt = select(Company).where(Company.company_id == context.company_id)
            res = await db.execute(stmt)
            company = res.scalar()
            if company:
                return cls.build_branding_response(company)

        # Fallback for super_admin or user without associated company
        return cls.get_global_branding()

    @classmethod
    async def update_company_branding(
        cls, db: AsyncSession, company_id: int, payload: CompanyBrandingUpdate
    ) -> Optional[CompanyBrandingResponse]:
        """Update branding fields for a specific company."""
        stmt = select(Company).where(Company.company_id == company_id)
        res = await db.execute(stmt)
        company = res.scalar()
        if not company:
            return None

        update_data = payload.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if hasattr(company, field):
                setattr(company, field, value)

        await db.commit()
        await db.refresh(company)
        return cls.build_branding_response(company)
