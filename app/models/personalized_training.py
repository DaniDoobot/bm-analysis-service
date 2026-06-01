"""SQLAlchemy models for personalized agent training, reports, prompts, and completion status."""
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TrainingAgentSetting(Base):
    __tablename__ = "bm_training_agent_settings"

    setting_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hubspot_owner_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_initials: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )


class TrainingRun(Base):
    __tablename__ = "bm_training_runs"

    training_run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="'pending'", nullable=False)  # pending, running, completed, failed, partially_completed
    triggered_by: Mapped[str] = mapped_column(Text, default="manual", server_default="'manual'", nullable=False)  # scheduler, manual
    created_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    
    agents_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    agents_completed: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    agents_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    agents_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    reports = relationship("TrainingAgentReport", back_populates="run", cascade="all, delete-orphan")


class TrainingAgentReport(Base):
    __tablename__ = "bm_training_agent_reports"

    training_report_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    training_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_training_runs.training_run_id", ondelete="SET NULL"), nullable=True
    )
    hubspot_owner_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_initials: Mapped[str] = mapped_column(Text, nullable=False)
    
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="'pending'", nullable=False)  # pending, running, completed, skipped, failed
    skipped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    evaluations_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    calls_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    avg_evaluacion_global: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    
    summary_general: Mapped[str | None] = mapped_column(Text, nullable=True)
    strengths_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    weaknesses_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    notable_data_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    general_objectives_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    specific_objectives_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    superseded_by_report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run = relationship("TrainingRun", back_populates="reports")
    prompts = relationship("TrainingSimulationPrompt", back_populates="report", cascade="all, delete-orphan")
    completion_statuses = relationship("TrainingCompletionStatus", back_populates="report", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_training_report_agent_period", "hubspot_owner_id", "period_start", "period_end"),
    )


class TrainingSimulationPrompt(Base):
    __tablename__ = "bm_training_simulation_prompts"

    simulation_prompt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    training_report_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_training_agent_reports.training_report_id", ondelete="CASCADE"), nullable=False
    )
    hubspot_owner_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    prompt_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2, 3, 4
    title: Mapped[str] = mapped_column(Text, nullable=False)
    scenario_type: Mapped[str] = mapped_column(Text, nullable=False)
    objective_focus_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    report = relationship("TrainingAgentReport", back_populates="prompts")
    completion_status = relationship("TrainingCompletionStatus", back_populates="prompt", cascade="all, delete-orphan", uselist=False)


class TrainingCompletionStatus(Base):
    __tablename__ = "bm_training_completion_status"

    completion_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    training_report_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_training_agent_reports.training_report_id", ondelete="CASCADE"), nullable=False
    )
    simulation_prompt_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_training_simulation_prompts.simulation_prompt_id", ondelete="CASCADE"), nullable=False
    )
    hubspot_owner_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="'pending'", nullable=False)  # pending, completed, failed
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    training_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    training_phone_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    report = relationship("TrainingAgentReport", back_populates="completion_statuses")
    prompt = relationship("TrainingSimulationPrompt", back_populates="completion_status")
