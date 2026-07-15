"""SQLAlchemy ORM models for bm_teams and associations."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, Text, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class Team(Base):
    __tablename__ = "bm_teams"

    team_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_name: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company = relationship("Company")
    service = relationship("Service")

    __table_args__ = (
        UniqueConstraint("service_id", "team_name", name="uq_service_team_name"),
    )


class UserServiceAssociation(Base):
    __tablename__ = "bm_user_services"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_users.user_id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_services.service_id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserTeamAssociation(Base):
    __tablename__ = "bm_user_teams"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_users.user_id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_teams.team_id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentTeamAssociation(Base):
    __tablename__ = "bm_agent_teams"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_users.user_id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_teams.team_id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
