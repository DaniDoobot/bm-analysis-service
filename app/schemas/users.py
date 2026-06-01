"""Pydantic schemas for User profile and auth payloads."""
from pydantic import BaseModel


class UserBase(BaseModel):
    username: str
    email: str
    role: str = "agent"
    is_active: bool = True
    hubspot_owner_id: str | None = None
    agent_initials: str | None = None


class UserOut(UserBase):
    user_id: int
    password_masked: str = "********"

    class Config:
        from_attributes = True


class MeUpdatePayload(BaseModel):
    current_password: str
    username: str | None = None
    email: str | None = None
    hubspot_owner_id: str | None = None
    agent_initials: str | None = None


class MePasswordUpdatePayload(BaseModel):
    current_password: str
    new_password: str
    new_password_confirm: str


class RevealPasswordPayload(BaseModel):
    current_password: str


class LoginPayload(BaseModel):
    """Accepts username OR email + password. Both field names are supported for compatibility."""
    username: str | None = None
    email: str | None = None
    password: str

    @property
    def login_identifier(self) -> str:
        """Return whichever of username/email was provided."""
        return (self.username or self.email or "").strip()

    def model_post_init(self, __context):
        if not self.username and not self.email:
            raise ValueError("Se requiere 'username' o 'email'.")
