"""Pydantic schemas for prompts and prompt versions."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


from enum import Enum

class StructureType(str, Enum):
    prompt = "prompt"
    prompt_base_structure = "prompt-base-structure"
    base = "base"
    specific = "specific"


class OwnerUserOut(BaseModel):
    user_id: int
    display_name: str | None = None
    email: str | None = None


class StructureAccessOut(BaseModel):
    effective_permission: str
    is_owner: bool
    is_admin: bool
    can_view: bool
    can_use: bool
    can_edit: bool
    can_share: bool
    can_delete: bool
    can_transfer: bool
    can_duplicate: bool
    access_source: str
    manual_permission: str
    inherited_permission: str
    can_archive: bool
    can_restore: bool


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
    service_id: int | None = None
    service_key: str | None = None
    service_name: str | None = None

    # Archiving support
    is_archived: bool = False
    archived_at: datetime | None = None
    archived_by_email: str | None = None
    deleted_at: datetime | None = None


class PromptWithCurrentVersion(PromptOut):
    current_version_id: int | None = None
    version_label: str | None = None
    version_name: str | None = None
    prompt: str | None = None

    # Permissions metadata
    owner: OwnerUserOut | None = None
    access: StructureAccessOut | None = None

    # Extra fields for maximum frontend compatibility:
    name: str | None = None
    version: str | None = None
    label: str | None = None
    base: str | None = None

    # Draft state distinction
    has_active_draft: bool = False
    draft_status: str | None = None
    active_draft_id: int | None = None
    current_version_label: str | None = None


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
    service_id: int | None = None
    service_key: str | None = None
    service_name: str | None = None

    # Extra fields for maximum frontend compatibility:
    name: str | None = None
    version: str | None = None
    label: str | None = None
    base: str | None = None

    # Archiving support
    is_archived: bool = False
    archived_at: datetime | None = None
    archived_by_email: str | None = None



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
    prompt_type: str = "text"
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: str | None = None
    created_by_email: str | None = None
    service_id: int | None = None
    service_key: str | None = None
    service_name: str | None = None

    # Permissions metadata
    owner: OwnerUserOut | None = None
    access: StructureAccessOut | None = None


class PromptBaseStructureDetailOut(PromptBaseStructureOut):
    base_prompt: str
    default_criteria: list[dict[str, Any]] = []


class PromptBaseStructureCreate(BaseModel):
    structure_key: str
    structure_name: str
    description: str | None = None
    prompt_type: str = "text"
    base_prompt: str
    default_criteria: list[dict[str, Any]] | None = None
    created_by: str | None = None
    created_by_email: str | None = None
    service_id: int | None = None
    owner_user_id: int | None = None


class PromptBaseStructureUpdate(BaseModel):
    structure_name: str | None = None
    name: str | None = None
    description: str | None = None
    prompt_type: str | None = None
    base_prompt: str | None = None
    default_criteria: list[dict[str, Any]] | None = None
    is_active: bool | None = None
    service_id: int | None = None


class CreateFromBaseRequest(BaseModel):
    base_structure_id: int
    prompt_name: str
    prompt_type: str = "audio"
    created_by: str | None = None
    created_by_email: str | None = None
    copy_default_criteria: bool = True
    activate: bool = False
    owner_user_id: int | None = None


class CreateFromBaseResponse(BaseModel):
    ok: bool
    prompt_id: int
    prompt_version_id: int | None = None
    prompt_name: str
    prompt_type: str
    prompt: str | None = None
    criteria_count: int
    service_id: int | None = None


# ── Structure Sharing / Permissions ─────────────────────────────────────────

class StructurePermissionOut(BaseModel):
    permission_id: int
    user_id: int
    username: str
    email: str
    permission_level: str
    granted_by_user_id: int | None = None
    created_at: datetime
    updated_at: datetime


class PermissionActionResponse(BaseModel):
    ok: bool
    message: str


class GrantPermissionRequest(BaseModel):
    user_id: int
    permission_level: str


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: int


class TypologyItem(BaseModel):
    id: int
    key: str
    name: str
    service: str
    typology_key: str
    service_id: int
    service_key: str
    is_active: bool
    description: str | None = None


class BaseStructureWithTypologies(BaseModel):
    id: int
    name: str
    structure_key: str
    service: str | None = None
    description: str | None = None
    is_active: bool
    associated_typologies: list[TypologyItem] = []
    available_typologies: list[TypologyItem] = []


class UpdateTypologiesRequest(BaseModel):
    typology_ids: list[int]


class PromptBaseStructureNestedDetailOut(BaseModel):
    structure: PromptBaseStructureDetailOut
    associated_typologies: list[TypologyItem] = []
    available_typologies: list[TypologyItem] = []



