"""SQLAlchemy models for automated mass evaluations."""
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class MassEvaluationJob(Base):
    __tablename__ = "bm_mass_evaluation_jobs"

    job_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # execution source: on_demand vs automation
    execution_source: Mapped[str] = mapped_column(Text, default="on_demand", server_default="'on_demand'")

    # Prompt specific fields
    prompt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version_label: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Selection filters
    agent_owner_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    agent_names: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    date_mode: Mapped[str] = mapped_column(Text, default="relative", server_default="'relative'")
    date_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    date_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    relative_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_window_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    time_window_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, default="Europe/Madrid", server_default="'Europe/Madrid'")
    duration_min_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_max_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str] = mapped_column(Text, default="all", server_default="'all'")
    only_with_recording: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    max_calls: Mapped[int] = mapped_column(Integer, default=100, server_default="100")

    # Scheduling
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    schedule_type: Mapped[str | None] = mapped_column(Text, nullable=True)  # manual, daily, weekly, monthly, cron
    schedule_cron: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_rrule: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0 Mon, 6 Sun
    schedule_day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Audit
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    runs = relationship("MassEvaluationRun", back_populates="job", cascade="all, delete-orphan")
    results = relationship("MassEvaluationResult", back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_mass_eval_jobs_active_next", "is_active", "next_run_at"),
        Index("idx_mass_eval_jobs_execution_source", "execution_source"),
    )


class MassEvaluationRun(Base):
    __tablename__ = "bm_mass_evaluation_runs"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("bm_mass_evaluation_jobs.job_id", ondelete="CASCADE"), nullable=False)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)  # manual, scheduled

    # execution source: on_demand vs automation
    execution_source: Mapped[str] = mapped_column(Text, default="on_demand", server_default="'on_demand'")

    status: Mapped[str] = mapped_column(Text, default="pending", server_default="'pending'")  # pending, running, completed, completed_with_errors, failed
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Summary
    calls_found: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    calls_selected: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    calls_analyzed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    calls_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    calls_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    effective_filters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    job = relationship("MassEvaluationJob", back_populates="runs")
    results = relationship("MassEvaluationResult", back_populates="run", cascade="all, delete-orphan")


class MassEvaluationResult(Base):
    __tablename__ = "bm_mass_evaluation_results"

    mass_analysis_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("bm_mass_evaluation_runs.run_id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("bm_mass_evaluation_jobs.job_id", ondelete="CASCADE"), nullable=False)

    # execution source: on_demand vs automation
    execution_source: Mapped[str] = mapped_column(Text, default="on_demand", server_default="'on_demand'")

    # Call Identity
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    hs_object_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    hubspot_owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analysis_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    call_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Prompt details
    prompt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)

    # Service and typology snapshot
    service_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    service_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    typology_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    typology_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    typology_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Result payload
    status: Mapped[str] = mapped_column(Text, default="completed", server_default="'completed'")  # completed, failed, skipped
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    items_json: Mapped[list[Any] | dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evaluacion_global: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    hubspot_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())

    # Audit / Overwrite details
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    @property
    def global_score(self) -> float | None:
        return float(self.evaluacion_global) if self.evaluacion_global is not None else None

    # Relationships
    job = relationship("MassEvaluationJob", back_populates="results")
    run = relationship("MassEvaluationRun", back_populates="results")

    __table_args__ = (
        UniqueConstraint("run_id", "call_id", name="uq_mass_eval_run_call"),
        UniqueConstraint("call_id", "prompt_id", name="uq_mass_eval_call_prompt"),
        Index("idx_mass_eval_results_run_id", "run_id"),
        Index("idx_mass_eval_results_job_id", "job_id"),
        Index("idx_mass_eval_results_call_id", "call_id"),
        Index("idx_mass_eval_results_hubspot_owner_id", "hubspot_owner_id"),
        Index("idx_mass_eval_results_call_timestamp", "call_timestamp"),
        Index("idx_mass_eval_results_execution_source", "execution_source"),
    )



class MassEvaluationCriterionResult(Base):
    __tablename__ = "bm_mass_evaluation_criterion_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mass_analysis_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_mass_evaluation_results.mass_analysis_id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("bm_mass_evaluation_runs.run_id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("bm_mass_evaluation_jobs.job_id", ondelete="CASCADE"), nullable=False)

    # execution source: on_demand vs automation
    execution_source: Mapped[str] = mapped_column(Text, default="on_demand", server_default="'on_demand'")

    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    hs_object_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_prompts.prompt_id", ondelete="SET NULL"), nullable=True)
    prompt_version_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_prompt_versions.id", ondelete="SET NULL"), nullable=True)
    criterion_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("bm_prompt_criteria.criterion_id", ondelete="SET NULL"), nullable=True)
    criterion_key: Mapped[str] = mapped_column(Text, nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("job_id", "call_id", "criterion_key", name="uq_mass_eval_crit_res"),
        Index("idx_mass_eval_crit_mass_id", "mass_analysis_id"),
        Index("idx_mass_eval_crit_job_call", "job_id", "call_id"),
        Index("idx_mass_eval_crit_execution_source", "execution_source"),
    )


class MassAnalysisAutomation(Base):
    __tablename__ = "bm_mass_analysis_automations"

    automation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default="30")

    # Linked permanent job
    job_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bm_mass_evaluation_jobs.job_id", ondelete="SET NULL"), nullable=True)
    lookback_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    delay_minutes: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    
    # Target configurations
    service_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    
    # Call filters
    min_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction_filter: Mapped[str] = mapped_column(Text, default="all", server_default="'all'") # INBOUND, OUTBOUND, all
    agent_owner_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True) # JSON list of strings
    
    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    runs = relationship("MassAnalysisAutomationRun", back_populates="automation", cascade="all, delete-orphan")


class MassAnalysisAutomationRun(Base):
    __tablename__ = "bm_mass_analysis_automation_runs"

    automation_run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_mass_analysis_automations.automation_id", ondelete="CASCADE"), nullable=False
    )
    
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="'pending'") # pending, running, completed, failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Execution details
    window_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    calls_found: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    calls_selected: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    calls_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    
    # Linked background objects
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    automation = relationship("MassAnalysisAutomation", back_populates="runs")
