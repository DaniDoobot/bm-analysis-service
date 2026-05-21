"""SQLAlchemy ORM model for bm_services."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Service(Base):
    __tablename__ = "bm_services"

    service_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    service_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
