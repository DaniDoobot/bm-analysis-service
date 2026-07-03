"""SQLAlchemy ORM models for the Trainer module (simulations, configurations, sessions, and evaluations)."""
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    BigInteger,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TrainerEvaluationConfig(Base):
    __tablename__ = "bm_trainer_evaluation_configs"

    config_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="RESTRICT"), nullable=False
    )
    speech_structure_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bm_prompts.prompt_id", ondelete="RESTRICT"), nullable=False
    )
    extra_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    service = relationship("Service", lazy="joined")
    speech_structure = relationship("Prompt", lazy="joined")


class TrainerSimulation(Base):
    __tablename__ = "bm_trainer_simulations"

    simulation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="RESTRICT"), nullable=False
    )
    evaluation_config_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_trainer_evaluation_configs.config_id", ondelete="SET NULL"), nullable=True
    )
    roleplay_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="draft", server_default="'draft'", nullable=False)  # draft, published, archived
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    service = relationship("Service", lazy="joined")
    evaluation_config = relationship("TrainerEvaluationConfig", lazy="joined")
    versions = relationship("TrainerSimulationVersion", back_populates="simulation", cascade="all, delete-orphan")


class TrainerSimulationVersion(Base):
    __tablename__ = "bm_trainer_simulation_versions"

    version_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    simulation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_simulations.simulation_id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    roleplay_prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    service_id: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluation_config_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )

    simulation = relationship("TrainerSimulation", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("simulation_id", "version_number", name="uq_bm_trainer_sim_version"),
    )


class TrainerSession(Base):
    __tablename__ = "bm_trainer_sessions"

    session_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    simulation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_simulations.simulation_id", ondelete="RESTRICT"), nullable=False
    )
    simulation_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_trainer_simulation_versions.version_id", ondelete="SET NULL"), nullable=True
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    agent_code: Mapped[str] = mapped_column(Text, nullable=False)
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="RESTRICT"), nullable=False
    )
    call_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    external_call_sid: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="started", server_default="'started'", nullable=False)  # started, completed, failed
    evaluation_status: Mapped[str] = mapped_column(Text, default="started", server_default="'started'", nullable=False)  # started, completed, evaluation_pending, evaluated, evaluation_error, failed
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    simulation = relationship("TrainerSimulation")
    simulation_version = relationship("TrainerSimulationVersion")
    service = relationship("Service")


class TrainerEvaluation(Base):
    __tablename__ = "bm_trainer_evaluations"

    evaluation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_sessions.session_id", ondelete="RESTRICT"), nullable=False
    )
    evaluation_config_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_trainer_evaluation_configs.config_id", ondelete="SET NULL"), nullable=True
    )
    prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    strengths: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    improvement_points: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    session = relationship("TrainerSession")
    evaluation_config = relationship("TrainerEvaluationConfig")
