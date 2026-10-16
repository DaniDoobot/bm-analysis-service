"""SQLAlchemy model for CloudTalk evaluation jobs."""
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.types import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CloudTalkEvaluationJob(Base):
    __tablename__ = "bm_cloudtalk_evaluation_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="CASCADE"), nullable=False, default=1
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="CASCADE"), nullable=False
    )
    cloudtalk_call_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    call_uuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    talking_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Webhook payload stored as JSON/JSONB (sanitized of sensitive tokens/headers)
    webhook_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )

    # Status: pending, waiting_recording, processing, completed, skipped, failed, configuration_missing
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="'pending'")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    analysis_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_analyses.analysis_id", ondelete="SET NULL"), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    webhook_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    company = relationship("Company")
    service = relationship("Service")
    analysis = relationship("Analysis")

    __table_args__ = (
        UniqueConstraint("company_id", "service_id", "cloudtalk_call_id", name="uq_cloudtalk_eval_job_call"),
        Index("ix_bm_cloudtalk_eval_jobs_status_next", "status", "next_attempt_at"),
    )
