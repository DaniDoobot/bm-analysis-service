"""Pydantic schemas for prompt criteria."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class CriterionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    criterion_id: int
    prompt_id: int | None = None
    criterion_key: str | None = None
    criterion_name: str | None = None
    criterion_description: str | None = None
    criterion_type: str | None = None
    output_key: str | None = None
    feed_key: str | None = None
    allowed_values: Any | None = None
    applies_to_types: Any | None = None
    order_index: int | None = None
    is_required: bool = False
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CriteriaGroupedOut(BaseModel):
    prompt: str | None = None
    criteria: list[CriterionOut] = []
    grouped: dict[str, list[CriterionOut]] = {}


class SaveCriterionRequest(BaseModel):
    criterion_id: int | None = None
    prompt_id: int
    criterion_key: str
    criterion_name: str
    criterion_description: str | None = None
    criterion_type: str = "text"
    output_key: str | None = None
    feed_key: str | None = None
    allowed_values: Any | None = None
    applies_to_types: Any | None = None
    order_index: int | None = 100
    is_required: bool = False
    is_active: bool = True


class ToggleCriterionRequest(BaseModel):
    criterion_id: int
    is_active: bool
