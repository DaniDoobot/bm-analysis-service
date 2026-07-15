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
    company_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="SET NULL"), nullable=True
    )
    hubspot_owner_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)

    company = relationship("Company")
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_initials: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    training_code: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True, index=True)
    training_numeric_code: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True, index=True)
    training_code_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    training_code_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )


class TrainingRun(Base):
    __tablename__ = "bm_training_runs"

    training_run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="SET NULL"), nullable=True
    )
    service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="SET NULL"), nullable=True
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    company = relationship("Company")
    service = relationship("Service")
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
    company_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="SET NULL"), nullable=True
    )
    service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="SET NULL"), nullable=True
    )
    hubspot_owner_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)

    company = relationship("Company")
    service = relationship("Service")
    agent_initials: Mapped[str] = mapped_column(Text, nullable=False)
    
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="'pending'", nullable=False)  # pending, running, pending_approval, in_progress, completed, skipped, failed, superseded, archived, finalization_failed
    cycle_mode: Mapped[str] = mapped_column(Text, default="automatic", server_default="'automatic'", nullable=False)
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
    final_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    superseded_by_report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    call_session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_training_call_sessions.session_id", ondelete="SET NULL"), nullable=True
    )
    evaluation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_training_call_evaluations.evaluation_id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    report = relationship("TrainingAgentReport", back_populates="completion_statuses")
    prompt = relationship("TrainingSimulationPrompt", back_populates="completion_status")
    call_session = relationship("TrainingCallSession", foreign_keys=[call_session_id])
    evaluation = relationship("TrainingCallEvaluation", foreign_keys=[evaluation_id])


class TrainingSchedulerSetting(Base):
    __tablename__ = "bm_training_scheduler_settings"

    setting_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    interval_days: Mapped[int] = mapped_column(Integer, default=14, server_default="14", nullable=False)
    lookback_days: Mapped[int] = mapped_column(Integer, default=14, server_default="14", nullable=False)
    
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )
    updated_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)


class TrainingCallSession(Base):
    __tablename__ = "bm_training_call_sessions"

    session_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_sid: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    recording_sid: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    cycle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_training_agent_reports.training_report_id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_training_simulation_prompts.simulation_prompt_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, default="in_progress", server_default="'in_progress'", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recording_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evaluation_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    cycle = relationship("TrainingAgentReport")
    prompt = relationship("TrainingSimulationPrompt")


class TrainingEvaluationPrompt(Base):
    __tablename__ = "bm_training_evaluation_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="SET NULL"), nullable=True
    )
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)

    company = relationship("Company")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    service = relationship("Service")


class TrainingCallEvaluation(Base):
    __tablename__ = "bm_training_call_evaluations"

    evaluation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_training_call_sessions.session_id", ondelete="CASCADE"), nullable=False
    )
    cycle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_training_agent_reports.training_report_id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_training_simulation_prompts.simulation_prompt_id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    prompt_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_training_evaluation_prompts.id"), nullable=False
    )
    transcription: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )

    session = relationship("TrainingCallSession")
    cycle = relationship("TrainingAgentReport")
    prompt = relationship("TrainingSimulationPrompt")
    prompt_version = relationship("TrainingEvaluationPrompt")
