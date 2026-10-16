"""Pydantic schemas for CloudTalk webhook payload and evaluation jobs."""
from datetime import datetime
from typing import Any, Dict, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class CloudTalkWebhookPayload(BaseModel):
    """
    Flexible schema for CloudTalk Workflow Automation 'Call -> Ended' webhook payload.
    Accepts various naming conventions used by CloudTalk APIs and webhooks.
    """
    model_config = ConfigDict(extra="allow")

    # Identifiers
    call_id: Optional[Union[str, int]] = None
    cdr_id: Optional[Union[str, int]] = None
    id: Optional[Union[str, int]] = None
    call_uuid: Optional[str] = None
    uuid: Optional[str] = None

    # Timing and Duration
    talking_time: Optional[Union[int, float, str]] = None
    billsec: Optional[Union[int, float, str]] = None
    duration: Optional[Union[int, float, str]] = None
    started_at: Optional[Union[str, datetime]] = None
    ended_at: Optional[Union[str, datetime]] = None

    # Call Metadata
    direction: Optional[str] = None
    type: Optional[str] = None

    # Agent Information
    agent_id: Optional[Union[str, int]] = None
    agent_name: Optional[str] = None
    agent_email: Optional[str] = None

    # Numbers
    external_number: Optional[str] = None
    contact_number: Optional[str] = None
    public_external: Optional[str] = None
    number: Optional[str] = None
    internal_number: Optional[str] = None
    public_internal: Optional[str] = None

    # Nested objects if CloudTalk sends full event payload
    Cdr: Optional[Dict[str, Any]] = None
    Call: Optional[Dict[str, Any]] = None
    Agent: Optional[Dict[str, Any]] = None
    Contact: Optional[Dict[str, Any]] = None


class CloudTalkWebhookResponse(BaseModel):
    ok: bool = True
    status: str  # accepted, skipped, completed, pending, waiting_recording, processing
    job_id: Optional[int] = None
    call_id: Optional[str] = None
    duplicate: bool = False
    retrying: bool = False
    message: Optional[str] = None
    analysis_id: Optional[int] = None


class CloudTalkJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    service_id: int
    cloudtalk_call_id: str
    call_uuid: Optional[str] = None
    talking_time: Optional[int] = None
    direction: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_email: Optional[str] = None
    external_number: Optional[str] = None
    internal_number: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: Optional[datetime] = None
    analysis_id: Optional[int] = None
    last_error_code: Optional[str] = None
    last_error_message: Optional[str] = None
    webhook_received_at: datetime
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
