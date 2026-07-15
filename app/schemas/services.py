"""Pydantic schemas for Services."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class ServiceBase(BaseModel):
    service_key: str
    service_name: str
    description: str | None = None
    is_active: bool = True
    company_id: int | None = None


class ServiceCreate(ServiceBase):
    pass


class ServiceUpdate(BaseModel):
    service_name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    service_key: str | None = None
    company_id: int | None = None


class ServiceOut(ServiceBase):
    model_config = ConfigDict(from_attributes=True)

    service_id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
