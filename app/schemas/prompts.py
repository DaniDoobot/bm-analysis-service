"""Pydantic schemas for prompts and prompt versions."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


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
    base_structure_id: int | None = None
    base_structure_key: str | None = None
    base_structure_name: str | None = None


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
    version_name: str | None = None
    updated_by: str | None = None
    updated_by_email: str | None = None
    change_note: str | None = None
    source: str | None = None
    is_current: bool = False
    created_at: datetime | None = None
    base_structure_id: int | None = None
    base_structure_key: str | None = None
    base_structure_name: str | None = None

    @model_validator(mode="after")
    def apply_fallback(self) -> 'PromptVersionOut':
        if not self.version_name:
            self.version_name = self.change_note or self.version_label
        return self


# ── Active Prompt (for analysis use) ─────────────────────────────────────────

class ActivePromptOut(BaseModel):
    prompt_id: int
    prompt_name: str
    prompt_type: str
    description: str | None = None
    current_version_id: int | None = None
    version_label: str | None = None
    prompt: str | None = None
    base_structure_id: int | None = None
    base_structure_key: str | None = None
    base_structure_name: str | None = None



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
    version_name: str | None = None
    generated_name: str | None = None


# ── Activate Version Request ──────────────────────────────────────────────────

class ActivateVersionRequest(BaseModel):
    id: int


# ── Prompt Base Structure ─────────────────────────────────────────────────────

class PromptBaseStructureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    structure_key: str
    structure_name: str
    description: str | None = None
    prompt_type: str
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: str | None = None
    created_by_email: str | None = None


class PromptBaseStructureDetailOut(PromptBaseStructureOut):
    base_prompt: str
    default_criteria: list[dict[str, Any]] = []

    @model_validator(mode="after")
    def strip_criteria(self) -> "PromptBaseStructureDetailOut":
        # Always return empty list regardless of what the ORM/DB stored.
        # Base structures must never expose criteria items.
        self.default_criteria = []
        return self


class PromptBaseStructureCreate(BaseModel):
    structure_key: str
    structure_name: str
    description: str | None = None
    prompt_type: str = "audio"
    base_prompt: str
    default_criteria: list[dict[str, Any]] | None = None
    created_by: str | None = None
    created_by_email: str | None = None


class PromptBaseStructureUpdate(BaseModel):
    structure_name: str | None = None
    description: str | None = None
    prompt_type: str | None = None
    base_prompt: str | None = None
    default_criteria: list[dict[str, Any]] | None = None
    is_active: bool | None = None


class CreateFromBaseRequest(BaseModel):
    base_structure_id: int
    prompt_name: str
    prompt_type: str = "audio"
    created_by: str | None = None
    created_by_email: str | None = None
    copy_default_criteria: bool = True
    activate: bool = False


class CreateFromBaseResponse(BaseModel):
    ok: bool
    prompt_id: int
    prompt_version_id: int | None = None
    prompt_name: str
    prompt_type: str
    prompt: str | None = None
    criteria_count: int

