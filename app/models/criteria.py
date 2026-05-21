"""SQLAlchemy ORM model for bm_prompt_criteria."""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PromptCriterion(Base):
    __tablename__ = "bm_prompt_criteria"

    criterion_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prompt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompts.prompt_id", ondelete="CASCADE"), nullable=True
    )
    criterion_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    criterion_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    criterion_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    criterion_type: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # score_1_10 | percentage | boolean | text | category | number
    output_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    feed_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    allowed_values: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    applies_to_types: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    order_index: Mapped[int | None] = mapped_column(Integer, nullable=True, default=100)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PromptCriterionTypology(Base):
    __tablename__ = "bm_prompt_criterion_typologies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    criterion_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bm_prompt_criteria.criterion_id", ondelete="CASCADE"), nullable=False
    )
    typology_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_typologies.typology_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("criterion_id", "typology_id", name="uq_criterion_typology"),
    )


