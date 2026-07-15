from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict
from app.core.roles import InternalRole

class CompanyBase(BaseModel):
    company_name: str
    company_key: str
    is_active: bool = True

class CompanyCreate(CompanyBase):
    pass

class CompanyUpdate(BaseModel):
    company_name: Optional[str] = None
    company_key: Optional[str] = None
    is_active: Optional[bool] = None

class CompanyResponse(CompanyBase):
    company_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamBase(BaseModel):
    team_name: str
    company_id: int
    service_id: int

class TeamCreate(TeamBase):
    pass

class TeamUpdate(BaseModel):
    team_name: str

class TeamResponse(TeamBase):
    team_id: int
    created_at: datetime
    updated_at: datetime

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
    allowed_company_ids: List[int] = []
    allowed_service_ids: Optional[List[int]] = None
    allowed_team_ids: Optional[List[int]] = None
    is_super_admin: bool
    can_manage_companies: bool
    can_manage_company: bool
    can_manage_services: bool
    can_manage_teams: bool

    model_config = ConfigDict(from_attributes=True)
