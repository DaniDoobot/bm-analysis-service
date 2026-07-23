"""Pydantic schemas for User profile and auth payloads."""
from typing import Optional, Any, List
from pydantic import BaseModel, field_validator, model_validator, Field, AliasChoices


def normalize_name_val(v):
    if v is None:
        return None
    if isinstance(v, str):
        v_clean = v.strip()
        if v_clean == "":
            return None
        return v_clean
    return v


def check_name_conflict(data: Any) -> Any:
    if isinstance(data, dict):
        has_name = "name" in data
        has_fullname = "full_name" in data
        if has_name and has_fullname:
            val_name = data["name"]
            val_fullname = data["full_name"]
            if normalize_name_val(val_name) != normalize_name_val(val_fullname):
                raise ValueError("Los campos name y full_name contienen valores diferentes.")
            else:
                data = dict(data)
                data.pop("full_name", None)
    return data


class UserBase(BaseModel):
    username: str
    email: str
    name: Optional[str] = None
    role: str = "agente"
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    primary_service_id: Optional[int] = None
    primary_service_name: Optional[str] = None
    allowed_service_ids: List[int] = Field(default_factory=list)
    allowed_services: List[dict] = Field(default_factory=list)
    primary_team_id: Optional[int] = None
    primary_team_name: Optional[str] = None
    allowed_team_ids: List[int] = Field(default_factory=list)
    allowed_teams: List[dict] = Field(default_factory=list)
    normalized_role: Optional[str] = None
    is_active: bool = True
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None


class UserOut(UserBase):
    user_id: int
    password_masked: str = "********"

    class Config:
        from_attributes = True


class UserOutFull(UserBase):
    """Extended UserOut for admin views — includes timestamps."""
    user_id: int
    password_masked: str = "********"
    must_reset_password: Optional[bool] = None
    password_set_at: Optional[str] = None
    reset_token_expires_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


# ── Auth ──────────────────────────────────────────────────────────────────────

class LoginPayload(BaseModel):
    """Accepts username OR email + password. Both field names are supported for compatibility."""
    username: Optional[str] = None
    email: Optional[str] = None
    password: str

    @property
    def login_identifier(self) -> str:
        """Return whichever of username/email was provided."""
        ident = (self.username or self.email or "").strip()
        return ident

    def model_post_init(self, __context):
        if not self.username and not self.email:
            raise ValueError("Se requiere 'username' o 'email'.")


class BootstrapPayload(BaseModel):
    """Payload for first-admin bootstrap. Only usable when bm_users is empty."""
    username: str
    email: str
    password: str
    name: Optional[str] = None

    @field_validator("username", "email")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v.strip()


# ── Admin CRUD ────────────────────────────────────────────────────────────────

from enum import Enum
from datetime import datetime

class UserPasswordSetupMode(str, Enum):
    invite_link = "invite_link"
    temporary_password = "temporary_password"
    manual = "manual"

class PasswordSetupLinkResponse(BaseModel):
    url: str
    expires_at: datetime

class UserCreatePayload(BaseModel):
    """Payload for admin creating a new user."""
    model_config = {"extra": "forbid"}

    email: str
    username: Optional[str] = None       # default: email.split("@")[0]
    name: Optional[str] = Field(default=None, validation_alias=AliasChoices("name", "full_name"))
    password: Optional[str] = None
    role: str = "agente"
    company_id: Optional[int] = None
    primary_service_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("primary_service_id", "service_id"))
    allowed_service_ids: Optional[List[int]] = Field(default=None, validation_alias=AliasChoices("allowed_service_ids", "service_ids"))
    primary_team_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("primary_team_id", "team_id"))
    allowed_team_ids: Optional[List[int]] = Field(default=None, validation_alias=AliasChoices("allowed_team_ids", "team_ids"))
    is_active: bool = True
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None
    must_reset_password: bool = False
    password_setup: Optional[UserPasswordSetupMode] = None

    @model_validator(mode="before")
    @classmethod
    def check_conflict(cls, data: Any) -> Any:
        return check_name_conflict(data)

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, v):
        return normalize_name_val(v)

    @field_validator("hubspot_owner_id", mode="before")
    @classmethod
    def clean_hubspot_owner_id(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v_clean = v.strip()
            if v_clean == "":
                return None
            return v_clean
        return str(v).strip()

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if not v:
            return v
        from app.core.roles import ROLE_MAPPINGS
        v_clean = v.strip().lower().replace(" ", "_").replace("-", "_")
        if v.strip().lower() not in ROLE_MAPPINGS and v_clean not in ROLE_MAPPINGS:
            raise ValueError(f"Rol inválido '{v}'.")
        return v

    @model_validator(mode="after")
    def validate_password_requirement(self) -> Any:
        is_invite = (self.password_setup == UserPasswordSetupMode.invite_link)
        is_temp = (self.password_setup == UserPasswordSetupMode.temporary_password)
        
        if is_invite or is_temp:
            # Under explicit password_setup modes, password is not strictly required during creation
            pass
        else:
            # Backward compatibility
            if not self.must_reset_password and not self.password:
                raise ValueError("Se requiere 'password' cuando 'must_reset_password' es False.")
        return self


class UserUpdatePayload(BaseModel):
    """Payload for admin updating an existing user (all fields optional)."""
    model_config = {"extra": "forbid"}

    email: Optional[str] = None
    username: Optional[str] = None
    name: Optional[str] = Field(default=None, validation_alias=AliasChoices("name", "full_name"))
    role: Optional[str] = None
    company_id: Optional[int] = None
    primary_service_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("primary_service_id", "service_id"))
    allowed_service_ids: Optional[List[int]] = Field(default=None, validation_alias=AliasChoices("allowed_service_ids", "service_ids"))
    primary_team_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("primary_team_id", "team_id"))
    allowed_team_ids: Optional[List[int]] = Field(default=None, validation_alias=AliasChoices("allowed_team_ids", "team_ids"))
    is_active: Optional[bool] = None
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None
    must_reset_password: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def check_conflict(cls, data: Any) -> Any:
        return check_name_conflict(data)

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, v):
        return normalize_name_val(v)

    @field_validator("hubspot_owner_id", mode="before")
    @classmethod
    def clean_hubspot_owner_id(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v_clean = v.strip()
            if v_clean == "":
                return None
            return v_clean
        return str(v).strip()

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v is None:
            return v
        from app.core.roles import ROLE_MAPPINGS
        v_clean = v.strip().lower().replace(" ", "_").replace("-", "_")
        if v.strip().lower() not in ROLE_MAPPINGS and v_clean not in ROLE_MAPPINGS:
            raise ValueError(f"Rol inválido '{v}'.")
        return v



class UserAdminResetPasswordPayload(BaseModel):
    """Payload for admin resetting another user's password (no current_password required)."""
    new_password: str


class AdminPasswordResetPayload(BaseModel):
    temp_password: Optional[str] = None


# ── Self-service (me) ─────────────────────────────────────────────────────────

class MeUpdatePayload(BaseModel):
    current_password: str
    username: Optional[str] = None
    email: Optional[str] = None
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None

    @field_validator("hubspot_owner_id", mode="before")
    @classmethod
    def clean_hubspot_owner_id(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v_clean = v.strip()
            if v_clean == "":
                return None
            return v_clean
        return str(v).strip()


class MePasswordUpdatePayload(BaseModel):
    current_password: str
    new_password: str
    new_password_confirm: str


class RevealPasswordPayload(BaseModel):
    current_password: str


class RequestPasswordResetPayload(BaseModel):
    email: str


class ResetPasswordPayload(BaseModel):
    token: str
    new_password: str


# ── Sharing / Permissions Eligible Users ───────────────────────────────────

class EligibleUserOut(BaseModel):
    user_id: int
    username: str
    email: str
    role: str


class PasswordResetConfirmPayload(BaseModel):
    token: str
    new_password: str
    confirm_password: str




