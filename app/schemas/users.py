"""Pydantic schemas for User profile and auth payloads."""
from typing import Optional
from pydantic import BaseModel, field_validator, model_validator


class UserBase(BaseModel):
    username: str
    email: str
    role: str = "agente"
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
        return (self.username or self.email or "").strip()

    def model_post_init(self, __context):
        if not self.username and not self.email:
            raise ValueError("Se requiere 'username' o 'email'.")


class BootstrapPayload(BaseModel):
    """Payload for first-admin bootstrap. Only usable when bm_users is empty."""
    email: str
    username: Optional[str] = None
    password: str
    agent_initials: Optional[str] = None


# ── Admin CRUD ────────────────────────────────────────────────────────────────

class UserCreatePayload(BaseModel):
    """Payload for admin creating a new user."""
    email: str
    username: Optional[str] = None       # default: email.split("@")[0]
    password: Optional[str] = None
    role: str = "agente"
    is_active: bool = True
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None
    must_reset_password: bool = False

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        allowed = {"administrador", "admin", "agente", "agent", "usuario"}
        if v not in allowed:
            raise ValueError(f"Rol invalido '{v}'. Permitidos: {allowed}")
        return v

    @model_validator(mode="after")
    def validate_password_requirement(self) -> "UserCreatePayload":
        if not self.must_reset_password and not self.password:
            raise ValueError("Se requiere 'password' cuando 'must_reset_password' es False.")
        return self


class UserUpdatePayload(BaseModel):
    """Payload for admin updating an existing user (all fields optional)."""
    email: Optional[str] = None
    username: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None
    must_reset_password: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v is None:
            return v
        allowed = {"administrador", "admin", "agente", "agent", "usuario"}
        if v not in allowed:
            raise ValueError(f"Rol invalido '{v}'. Permitidos: {allowed}")
        return v


class UserAdminResetPasswordPayload(BaseModel):
    """Payload for admin resetting another user's password (no current_password required)."""
    new_password: str


# ── Self-service (me) ─────────────────────────────────────────────────────────

class MeUpdatePayload(BaseModel):
    current_password: str
    username: Optional[str] = None
    email: Optional[str] = None
    hubspot_owner_id: Optional[str] = None
    agent_initials: Optional[str] = None


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



