"""Pydantic schemas for Typologies."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class TypologyBase(BaseModel):
    service_id: int
    typology_key: str
    typology_name: str
    description: str | None = None
    sort_order: int = 100
    is_active: bool = True


class TypologyCreate(TypologyBase):
    pass


class TypologyUpdate(BaseModel):
    typology_name: str | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class TypologyOut(TypologyBase):
    model_config = ConfigDict(from_attributes=True)

    typology_id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CriterionTypologyAssociation(BaseModel):
    typology_id: int
    typology_key: str
    typology_name: str
    is_associated: bool
