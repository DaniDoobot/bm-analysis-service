"""SQLAlchemy ORM models for bm_analyses, bm_call_analysis_current, bm_analysis_results."""
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Analysis(Base):
    __tablename__ = "bm_analyses"

    analysis_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    analysis_type: Mapped[str | None] = mapped_column(Text, nullable=True)  # audio | text
    call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    hubspot_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fecha_eval: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agente_telefonico: Mapped[str | None] = mapped_column(Text, nullable=True)
    hubspot_owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompts.prompt_id", ondelete="SET NULL"), nullable=True
    )
    prompt_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompt_versions.id", ondelete="SET NULL"), nullable=True
    )
    transcription: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcription_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcription_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    tipo_llamada: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluacion_global: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CallAnalysisCurrent(Base):
    """
    Tabla de análisis vigente por call_id + analysis_type.
    No tiene una PK serial; la clave lógica es (call_id, analysis_type).
    Se usa upsert para mantener siempre el último análisis.
    """
    __tablename__ = "bm_call_analysis_current"

    call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    analysis_type: Mapped[str] = mapped_column(Text, primary_key=True)
    latest_analysis_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_analyses.analysis_id", ondelete="SET NULL"), nullable=True
    )
    hubspot_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    fecha_eval: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    agente_telefonico: Mapped[str | None] = mapped_column(Text, nullable=True)
    hubspot_owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompts.prompt_id", ondelete="SET NULL"), nullable=True
    )
    prompt_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompt_versions.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    tipo_llamada: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluacion_global: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)

    @property
    def analysis_id(self) -> int | None:
        return self.latest_analysis_id


class AnalysisResult(Base):
    __tablename__ = "bm_analysis_results"

    result_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_analyses.analysis_id", ondelete="CASCADE"), nullable=True
    )
    criterion_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompt_criteria.criterion_id", ondelete="SET NULL"), nullable=True
    )
    criterion_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    criterion_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    criterion_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_number: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    value_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    feed: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AnalysisCriterionResult(Base):
    __tablename__ = "bm_analysis_criterion_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bm_analyses.analysis_id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    hs_object_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompts.prompt_id", ondelete="SET NULL"), nullable=True
    )
    prompt_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompt_versions.id", ondelete="SET NULL"), nullable=True
    )
    criterion_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bm_prompt_criteria.criterion_id", ondelete="SET NULL"), nullable=True
    )
    criterion_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    criterion_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    criterion_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_raw: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    text_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    boolean_value: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    category_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    percentage_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    feed_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_applicable: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    not_applicable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    service_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    service_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    typology_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    typology_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    typology_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )


