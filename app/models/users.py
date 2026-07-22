"""SQLAlchemy ORM model for bm_users."""
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "bm_users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_companies.company_id", ondelete="SET NULL"), nullable=True)
    primary_service_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_services.service_id", ondelete="SET NULL"), nullable=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, default="agent", nullable=False)
    
    company = relationship("Company")
    primary_service = relationship("Service", foreign_keys=[primary_service_id])
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    hubspot_owner_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    agent_initials: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_plain_dev: Mapped[str | None] = mapped_column(Text, nullable=True)
    must_reset_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reset_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    reset_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserAudit(Base):
    __tablename__ = "bm_user_audits"

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("bm_users.user_id", ondelete="CASCADE"), nullable=False)
    target_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("bm_users.user_id", ondelete="CASCADE"), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # 'update', 'deactivate', 'activate', 'password_reset'
    changes_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PasswordResetToken(Base):
    __tablename__ = "bm_password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_users.user_id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        Text, unique=True, index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_admin_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_users.user_id", ondelete="SET NULL"), nullable=True
    )


