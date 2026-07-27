"""SQLAlchemy ORM model for bm_companies."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base

class Company(Base):
    __tablename__ = "bm_companies"

    company_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    company_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    
    # Branding & Customization fields
    brand_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand_short_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_dark_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    favicon_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_color: Mapped[str | None] = mapped_column(Text, nullable=True)
    secondary_color: Mapped[str | None] = mapped_column(Text, nullable=True)
    accent_color: Mapped[str | None] = mapped_column(Text, nullable=True)
    login_background_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_variant: Mapped[str | None] = mapped_column(Text, nullable=True)
    dashboard_variant: Mapped[str | None] = mapped_column(Text, nullable=True)
    sector: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_welcome_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_welcome_subtitle: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
