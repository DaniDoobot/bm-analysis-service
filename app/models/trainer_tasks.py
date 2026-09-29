"""SQLAlchemy ORM models for Trainer Tasks (tasks, task items, assignees, and attempt allocations)."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TrainerTask(Base):
    __tablename__ = "bm_trainer_tasks"

    task_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bm_users.user_id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text, default="active", server_default="'active'", nullable=False
    )  # active, completed, cancelled
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    company = relationship("Company")
    service = relationship("Service")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    items = relationship(
        "TrainerTaskItem",
        back_populates="task",
        order_by="TrainerTaskItem.order_index",
        lazy="selectin",
    )
    assignees = relationship("TrainerTaskAssignee", back_populates="task", lazy="selectin")

    def compute_effective_status(self, now: Optional[datetime] = None) -> str:
        """Derive effective status considering due date without modifying database."""
        current_time = now or datetime.now(timezone.utc)
        if self.status == "active" and self.due_at is not None:
            due = self.due_at if self.due_at.tzinfo else self.due_at.replace(tzinfo=timezone.utc)
            curr = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
            if curr > due:
                return "overdue"
        return self.status


class TrainerTaskItem(Base):
    __tablename__ = "bm_trainer_task_items"

    task_item_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    simulation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_simulations.simulation_id", ondelete="RESTRICT"), nullable=False
    )
    required_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )

    task = relationship("TrainerTask", back_populates="items")
    simulation = relationship("TrainerSimulation", lazy="joined")
    allocations = relationship("TrainerTaskAttemptAllocation", back_populates="task_item")

    __table_args__ = (
        CheckConstraint("required_attempts >= 1", name="chk_task_item_required_attempts"),
        UniqueConstraint("task_id", "simulation_id", name="uq_trainer_task_item"),
    )


class TrainerTaskAssignee(Base):
    __tablename__ = "bm_trainer_task_assignees"

    task_assignee_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="RESTRICT"), nullable=False
    )
    hubspot_owner_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, default="pending", server_default="'pending'", nullable=False
    )  # pending, in_progress, completed, completed_late, cancelled
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now()
    )

    task = relationship("TrainerTask", back_populates="assignees")
    company = relationship("Company")
    allocations = relationship("TrainerTaskAttemptAllocation", back_populates="assignee")

    __table_args__ = (
        UniqueConstraint("task_id", "hubspot_owner_id", name="uq_trainer_task_assignee"),
    )

    def compute_effective_status(self, now: Optional[datetime] = None) -> str:
        """
        Derive effective status:
        - If status is pending or in_progress, and task.due_at has passed: 'overdue'.
        - If status is completed and completed_at > task.due_at: 'completed_late'.
        - Otherwise returns current persisted status.
        """
        current_time = now or datetime.now(timezone.utc)
        curr = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        due_at = self.task.due_at if self.task and self.task.due_at else None
        if due_at:
            due = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
            if self.status in ("pending", "in_progress") and curr > due:
                return "overdue"
            if self.status == "completed" and self.completed_at:
                comp = self.completed_at if self.completed_at.tzinfo else self.completed_at.replace(tzinfo=timezone.utc)
                if comp > due:
                    return "completed_late"
        return self.status


class TrainerTaskAttemptAllocation(Base):
    __tablename__ = "bm_trainer_task_attempt_allocations"

    allocation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_assignee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_task_assignees.task_assignee_id", ondelete="RESTRICT"), nullable=False
    )
    task_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_task_items.task_item_id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_sessions.session_id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), nullable=False
    )

    assignee = relationship("TrainerTaskAssignee", back_populates="allocations")
    task_item = relationship("TrainerTaskItem", back_populates="allocations")
    session = relationship("TrainerSession")
