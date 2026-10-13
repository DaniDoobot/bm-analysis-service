"""
SQLAlchemy model for AI Usage Events.
Captures real token consumption, modality breakdowns, latency, and technical metrics from Gemini.
"""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Integer, Text, Numeric, DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AIUsageEvent(Base):
    __tablename__ = "bm_ai_usage_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), nullable=False
    )

    company_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="SET NULL"), nullable=True
    )
    service_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="SET NULL"), nullable=True
    )

    module: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    operation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(Text, default="gemini", server_default="'gemini'", nullable=False)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    credential_alias: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    request_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    call_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mass_analysis_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cycle_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trainer_session_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    prompt_token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    candidates_token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cached_content_token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    thoughts_token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    input_text_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    input_audio_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_text_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_audio_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    audio_duration_seconds: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    execution_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(Text, default="success", server_default="'success'", nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)

    company = relationship("Company")
    service = relationship("Service")

    __table_args__ = (
        Index("idx_bm_ai_usage_events_created_at", created_at.desc()),
        Index("idx_bm_ai_usage_events_company_created", company_id, created_at.desc()),
        Index("idx_bm_ai_usage_events_comp_serv_created", company_id, service_id, created_at.desc()),
        Index("idx_bm_ai_usage_events_module_created", module, created_at.desc()),
        Index("idx_bm_ai_usage_events_model_created", model, created_at.desc()),
        Index("idx_bm_ai_usage_events_call_id", call_id),
    )
