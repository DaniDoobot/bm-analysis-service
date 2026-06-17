"""SQLAlchemy ORM models for bm_prompts and bm_prompt_versions."""
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Prompt(Base):
    __tablename__ = "bm_prompts"

    prompt_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prompt_name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_type: Mapped[str] = mapped_column(Text, nullable=False)  # audio | text
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    base_structure_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_structure_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_structure_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_services.service_id"), nullable=True)
    service = relationship("Service", lazy="joined")
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_users.user_id", ondelete="RESTRICT"), nullable=True)

    # Archiving and Soft Delete support
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class PromptVersion(Base):
    __tablename__ = "bm_prompt_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prompt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompts.prompt_id", ondelete="CASCADE"), nullable=True
    )
    prompt: Mapped[str | None] = mapped_column("prompt_content", Text, nullable=True)
    version_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Archiving support — hides old versions from UI without physical delete
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)


class PromptBaseStructure(Base):
    __tablename__ = "bm_prompt_base_structures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    structure_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    structure_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_type: Mapped[str] = mapped_column(Text, default="audio", nullable=False)
    base_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    default_criteria: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_services.service_id"), nullable=True)
    service = relationship("Service", lazy="joined")
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_users.user_id", ondelete="RESTRICT"), nullable=True)


class StructurePermission(Base):
    __tablename__ = "bm_structure_permissions"

    permission_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    structure_type: Mapped[str] = mapped_column(Text, nullable=False) # 'base' or 'specific'
    structure_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("bm_users.user_id", ondelete="CASCADE"), nullable=False)
    permission_level: Mapped[str] = mapped_column(Text, nullable=False) # 'view', 'use', 'edit'
    granted_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_users.user_id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("structure_type", "structure_id", "user_id", name="uq_structure_user"),
    )


class StructurePermissionAudit(Base):
    __tablename__ = "bm_structure_permissions_audit"

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_users.user_id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False) # 'grant', 'modify', 'revoke', 'transfer', 'create', 'duplicate', 'delete'
    structure_type: Mapped[str] = mapped_column(Text, nullable=False)
    structure_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    affected_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_users.user_id", ondelete="SET NULL"), nullable=True)
    previous_permission: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_permission: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )




