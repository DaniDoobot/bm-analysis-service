"""SQLAlchemy ORM models for bm_prompts and bm_prompt_versions."""
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

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


