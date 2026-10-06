"""Pydantic schemas for AI Tutor chat persistence."""
from datetime import datetime
from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict


class TrainerChatCreate(BaseModel):
    """Payload to create a new persistent Tutor conversation thread."""
    title: Optional[str] = None
    target_agent_id: Optional[str] = None


class TrainerChatUpdate(BaseModel):
    """Payload to update title or archive status of a conversation."""
    title: Optional[str] = None
    is_archived: Optional[bool] = None


class TrainerChatMessageResponse(BaseModel):
    """Individual chat message representation."""
    message_id: int
    chat_id: int
    role: str
    content: str
    input_type: str = "text"
    created_at: datetime
    metadata_json: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class TrainerChatSummaryResponse(BaseModel):
    """Summary of a chat thread for list views and sidebars."""
    chat_id: int
    title: str
    target_agent_id: Optional[str] = None
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    message_count: Optional[int] = 0

    model_config = ConfigDict(from_attributes=True)


class TrainerChatDetailResponse(BaseModel):
    """Full detail of a chat thread including chronological messages."""
    chat_id: int
    title: str
    target_agent_id: Optional[str] = None
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    messages: List[TrainerChatMessageResponse] = []

    model_config = ConfigDict(from_attributes=True)
