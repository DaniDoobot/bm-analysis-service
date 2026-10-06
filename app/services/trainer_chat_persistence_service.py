"""Service for persistent AI Tutor chat storage and lifecycle management."""
import logging
from typing import Any, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.roles import InternalRole, normalize_role
from app.core.tenant_context import TenantContext
from app.models.trainer_chat import TrainerChat, TrainerChatMessage
from app.models.users import User
from app.schemas.trainer_chat import TrainerChatCreate, TrainerChatUpdate

logger = logging.getLogger(__name__)


class TrainerChatPersistenceService:
    """
    Manages persistent AI Tutor conversations and messages with strict tenant
    and user isolation.
    """

    @classmethod
    async def create_chat(
        cls,
        db: AsyncSession,
        current_user: User,
        context: TenantContext,
        data: TrainerChatCreate,
    ) -> TrainerChat:
        """
        Creates a new conversation thread.
        Strictly owned by current_user.user_id within company_id.
        Validates target_agent_id according to role rules.
        """
        from app.services.trainer_chatbot_service import TrainerChatbotService

        norm_role = normalize_role(current_user.role)

        if norm_role == InternalRole.AGENT:
            # Agents can only create chats about themselves
            if (
                data.target_agent_id
                and data.target_agent_id.strip()
                and data.target_agent_id.strip() != current_user.hubspot_owner_id
            ):
                logger.warning(
                    "Security notice: Agent %s tried to create chat for target agent %s",
                    current_user.user_id,
                    data.target_agent_id.strip(),
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Un agente no tiene permisos para crear conversaciones sobre otro agente.",
                )
            if not current_user.hubspot_owner_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID.",
                )
            target_agent = current_user.hubspot_owner_id
        elif data.target_agent_id and data.target_agent_id.strip():
            # Supervisors and admins validate target_agent_id via established resolver
            target_agent = TrainerChatbotService.resolve_target_agent(
                current_user=current_user,
                requested_agent_id=data.target_agent_id.strip(),
                context=context,
            )
        else:
            target_agent = current_user.hubspot_owner_id

        company_id = context.company_id or current_user.company_id
        if company_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se pudo determinar la empresa asociada al usuario.",
            )

        title = (
            data.title.strip()
            if (data.title and data.title.strip())
            else "Nueva conversación"
        )

        chat = TrainerChat(
            company_id=company_id,
            owner_user_id=current_user.user_id,
            target_agent_id=target_agent,
            title=title,
            is_active=True,
            is_archived=False,
        )
        db.add(chat)
        await db.commit()
        await db.refresh(chat)

        stmt = (
            select(TrainerChat)
            .where(TrainerChat.chat_id == chat.chat_id)
            .options(selectinload(TrainerChat.messages))
        )
        res = await db.execute(stmt)
        chat = res.scalar_one()
        chat.message_count = 0
        return chat

    @classmethod
    async def list_chats(
        cls,
        db: AsyncSession,
        current_user: User,
        context: TenantContext,
        target_agent_id: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> List[TrainerChat]:
        """
        Lists active chat threads owned by the current user within their company.
        Excludes other users' chats and other companies' chats.
        """
        company_id = context.company_id or current_user.company_id
        if company_id is None:
            return []

        msg_count_subq = (
            select(func.count(TrainerChatMessage.message_id))
            .where(TrainerChatMessage.chat_id == TrainerChat.chat_id)
            .scalar_subquery()
        )

        stmt = (
            select(TrainerChat, msg_count_subq.label("message_count"))
            .where(
                TrainerChat.owner_user_id == current_user.user_id,
                TrainerChat.company_id == company_id,
                TrainerChat.is_active == True,  # noqa: E712
            )
        )

        if not include_archived:
            stmt = stmt.where(TrainerChat.is_archived == False)  # noqa: E712

        if target_agent_id and target_agent_id.strip():
            stmt = stmt.where(TrainerChat.target_agent_id == target_agent_id.strip())

        stmt = stmt.order_by(TrainerChat.updated_at.desc(), TrainerChat.chat_id.desc()).limit(limit).offset(offset)
        res = await db.execute(stmt)
        rows = res.all()

        chats: List[TrainerChat] = []
        for chat_obj, count in rows:
            chat_obj.message_count = count or 0
            chats.append(chat_obj)

        return chats

    @classmethod
    async def get_chat(
        cls,
        db: AsyncSession,
        chat_id: int,
        current_user: User,
        context: TenantContext,
        with_messages: bool = True,
    ) -> Optional[TrainerChat]:
        """
        Retrieves a single chat thread by chat_id, strictly enforcing that
        owner_user_id matches current_user and company_id matches context.
        Returns None if not found or belongs to another user.
        """
        company_id = context.company_id or current_user.company_id
        if company_id is None:
            return None

        stmt = select(TrainerChat).where(
            TrainerChat.chat_id == chat_id,
            TrainerChat.owner_user_id == current_user.user_id,
            TrainerChat.company_id == company_id,
            TrainerChat.is_active == True,  # noqa: E712
        )

        if with_messages:
            stmt = stmt.options(selectinload(TrainerChat.messages))

        res = await db.execute(stmt)
        chat = res.scalar_one_or_none()
        if chat and with_messages:
            chat.message_count = len(chat.messages)
        return chat

    @classmethod
    async def update_chat(
        cls,
        db: AsyncSession,
        chat_id: int,
        current_user: User,
        context: TenantContext,
        data: TrainerChatUpdate,
    ) -> Optional[TrainerChat]:
        """
        Updates title or is_archived on an existing chat owned by current_user.
        """
        chat = await cls.get_chat(db, chat_id, current_user, context, with_messages=False)
        if not chat:
            return None

        if data.title is not None and data.title.strip():
            chat.title = data.title.strip()
        if data.is_archived is not None:
            chat.is_archived = data.is_archived

        chat.updated_at = func.now()
        await db.commit()
        await db.refresh(chat)

        count_stmt = select(func.count(TrainerChatMessage.message_id)).where(TrainerChatMessage.chat_id == chat_id)
        count_res = await db.execute(count_stmt)
        chat.message_count = count_res.scalar() or 0
        return chat

    @classmethod
    async def soft_delete_chat(
        cls,
        db: AsyncSession,
        chat_id: int,
        current_user: User,
        context: TenantContext,
    ) -> bool:
        """
        Soft deletes a conversation thread (is_active = False).
        Returns True if deleted, False if not found / foreign chat.
        """
        chat = await cls.get_chat(db, chat_id, current_user, context, with_messages=False)
        if not chat:
            return False

        chat.is_active = False
        chat.updated_at = func.now()
        await db.commit()
        return True

    @classmethod
    async def append_message(
        cls,
        db: AsyncSession,
        chat_id: int,
        role: str,
        content: str,
        input_type: str = "text",
        metadata_json: Optional[dict[str, Any]] = None,
    ) -> TrainerChatMessage:
        """
        Appends a message to a conversation thread.
        """
        msg = TrainerChatMessage(
            chat_id=chat_id,
            role=role,
            content=content,
            input_type=input_type,
            metadata_json=metadata_json,
        )
        db.add(msg)
        await db.flush()
        return msg

    @classmethod
    async def get_recent_messages(
        cls,
        db: AsyncSession,
        chat_id: int,
        limit: int = 8,
    ) -> List[dict[str, str]]:
        """
        Retrieves the last `limit` messages from DB for a chat in chronological order,
        formatted as [{"role": ..., "content": ...}] matching sanitize_history.
        """
        stmt = (
            select(TrainerChatMessage)
            .where(TrainerChatMessage.chat_id == chat_id)
            .order_by(TrainerChatMessage.created_at.desc(), TrainerChatMessage.message_id.desc())
            .limit(limit)
        )
        res = await db.execute(stmt)
        msgs = list(res.scalars().all())
        # Reverse to chronological order (oldest to newest)
        msgs.reverse()

        return [
            {
                "role": m.role,
                "content": m.content.strip()[:2000],
            }
            for m in msgs
            if m.role in ("user", "assistant") and m.content and m.content.strip()
        ]

    @classmethod
    async def touch_chat(cls, db: AsyncSession, chat_id: int) -> None:
        """Updates updated_at timestamp on the conversation thread."""
        await db.execute(
            update(TrainerChat)
            .where(TrainerChat.chat_id == chat_id)
            .values(updated_at=func.now())
        )
