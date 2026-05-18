"""Pydantic schemas for prompts and prompt versions."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


# ── Prompt ────────────────────────────────────────────────────────────────────

class PromptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    prompt_id: int
    prompt_name: str
    prompt_type: str
    description: str | None = None
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PromptWithCurrentVersion(PromptOut):
    current_version_id: int | None = None
    version_label: str | None = None
    prompt: str | None = None


# ── Prompt Version ────────────────────────────────────────────────────────────

class PromptVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    prompt_id: int | None = None
    prompt: str | None = None
    version_label: str | None = None
    updated_by: str | None = None
    updated_by_email: str | None = None
    change_note: str | None = None
    source: str | None = None
    is_current: bool = False
    created_at: datetime | None = None


# ── Active Prompt (for analysis use) ─────────────────────────────────────────

class ActivePromptOut(BaseModel):
    prompt_id: int
    prompt_name: str
    prompt_type: str
    description: str | None = None
    current_version_id: int | None = None
    version_label: str | None = None
    prompt: str | None = None


# ── Save Prompt Request ───────────────────────────────────────────────────────

class SavePromptRequest(BaseModel):
    prompt_id: int
    prompt_type: str | None = None
    prompt: str
    updated_by: str | None = None
    updated_by_email: str | None = None
    change_note: str | None = None
    source: str | None = "manual"
    version_label: str | None = None


# ── Activate Version Request ──────────────────────────────────────────────────

class ActivateVersionRequest(BaseModel):
    id: int
