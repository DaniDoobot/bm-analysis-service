"""SQLAlchemy ORM models for Trainer Chatbot persistence (chats and messages)."""
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TrainerChat(Base):
    """
    Persistent AI Tutor conversation thread.
    Belongs strictly to owner_user_id within company_id.
    Optionally scoped to target_agent_id (for supervisors examining a specific agent).
    """
    __tablename__ = "bm_trainer_chats"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_companies.company_id", ondelete="RESTRICT"), nullable=False
    )
    owner_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_users.user_id", ondelete="CASCADE"), nullable=False
    )
    target_agent_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="Nueva conversación", server_default="Nueva conversación")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    company = relationship("Company", foreign_keys=[company_id])
    owner_user = relationship("User", foreign_keys=[owner_user_id])
    messages: Mapped[List["TrainerChatMessage"]] = relationship(
        "TrainerChatMessage",
        back_populates="chat",
        cascade="all, delete-orphan",
        order_by="TrainerChatMessage.created_at.asc()",
    )

    __table_args__ = (
        Index("idx_bm_trainer_chats_owner_updated", "owner_user_id", "updated_at"),
        Index("idx_bm_trainer_chats_company", "company_id"),
        Index("idx_bm_trainer_chats_target_agent", "owner_user_id", "target_agent_id"),
    )


class TrainerChatMessage(Base):
    """
    Individual chat message within a persistent conversation thread.
    Preserves user prompts (text or audio transcripts) and assistant responses.
    """
    __tablename__ = "bm_trainer_chat_messages"

    message_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bm_trainer_chats.chat_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    input_type: Mapped[str] = mapped_column(Text, nullable=False, default="text", server_default="text")
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationship
    chat: Mapped["TrainerChat"] = relationship("TrainerChat", back_populates="messages")

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="chk_trainer_chat_message_role"),
        CheckConstraint("input_type IN ('text', 'audio')", name="chk_trainer_chat_message_input_type"),
        Index("idx_bm_trainer_chat_messages_chat_created", "chat_id", "created_at"),
    )
