"""SQLAlchemy ORM model for bm_prompt_drafts."""
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PromptDraft(Base):
    __tablename__ = "bm_prompt_drafts"

    draft_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prompt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompts.prompt_id", ondelete="CASCADE"), nullable=True
    )
    draft_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_data: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(
        Text, default="draft", nullable=True
    )  # draft | discarded | published
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

