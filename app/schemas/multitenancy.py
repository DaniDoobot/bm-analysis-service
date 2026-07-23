import re
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, field_validator
from app.core.roles import InternalRole

_SLUG_REGEX = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_company_key(v: str) -> str:
    """Validates that company_key is a valid slug: lowercase letters, digits, hyphens, underscores.
    Must start with a letter or digit. No spaces or special characters."""
    if not v or not v.strip():
        raise ValueError("La clave de empresa no puede estar vacía.")
    v = v.strip().lower()
    if not _SLUG_REGEX.match(v):
        raise ValueError(
            "La clave de empresa solo puede contener letras minúsculas, números, guiones (-) y guiones bajos (_), "
            "y debe empezar por letra o número."
        )
    return v


class CompanyBase(BaseModel):
    company_name: str
    company_key: str
    is_active: bool = True

class CompanyCreate(CompanyBase):
    @field_validator("company_key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        return _validate_company_key(v)

    @field_validator("company_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("El nombre de empresa no puede estar vacío.")
        return v.strip()

class CompanyUpdate(BaseModel):
    company_name: Optional[str] = None
    company_key: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("company_key")
    @classmethod
    def validate_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_company_key(v)

    @field_validator("company_name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("El nombre de empresa no puede estar vacío.")
        return v.strip()

class CompanyResponse(CompanyBase):
    company_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class AdminCompanyResponse(BaseModel):
    """Enriched company response for admin views with resource counts."""
    company_id: int
    company_name: str
    company_key: str
    is_active: bool
    services_count: int = 0
    users_count: int = 0
    teams_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)



class TeamBase(BaseModel):
    team_name: str
    company_id: int
    service_id: int

class TeamCreate(TeamBase):
    pass

class AdminTeamCreate(BaseModel):
    team_name: str
    company_id: int
    service_id: int

class TeamUpdate(BaseModel):
    """Update schema: all fields optional to allow partial updates."""
    team_name: Optional[str] = None
    is_active: Optional[bool] = None

class TeamResponse(TeamBase):
    team_id: int
    is_active: bool = True
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class AdminTeamResponse(BaseModel):
    """Enriched team response for admin views (includes denormalized names and counts)."""
    team_id: int
    team_name: str
    company_id: int
    company_name: Optional[str] = None
    service_id: int
    service_name: Optional[str] = None
    is_active: bool = True
    agent_count: int = 0
    coordinator_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class AdminTeamMemberResponse(BaseModel):
    """Summary of a user who is a team member (agent or coordinator)."""
    user_id: int
    username: str
    email: str
    name: Optional[str] = None
    role: str
    normalized_role: Optional[str] = None
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)



class UserSummaryResponse(BaseModel):
    user_id: int
    username: str
    email: str
    name: Optional[str] = None
    role: str
    hubspot_owner_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TenantContextResponse(BaseModel):
    user_id: int
    username: str
    email: str
    raw_role: str
    normalized_role: InternalRole
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    primary_service_id: Optional[int] = None
    primary_service_name: Optional[str] = None
    allowed_company_ids: List[int] = []
    allowed_service_ids: Optional[List[int]] = None
    allowed_services: Optional[List[dict]] = None
    allowed_team_ids: Optional[List[int]] = None
    is_super_admin: bool
    can_manage_companies: bool
    can_manage_company: bool
    can_manage_services: bool
    can_manage_teams: bool
    can_manage_users: bool
    can_manage_training: bool = False
    can_manage_trainer: bool = False
    can_manage_structures: bool = False

    model_config = ConfigDict(from_attributes=True)
