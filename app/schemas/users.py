"""Pydantic schemas for User profile and auth payloads."""
from pydantic import BaseModel


class UserBase(BaseModel):
    username: str
    email: str
    role: str = "agent"
    is_active: bool = True


class UserOut(UserBase):
    user_id: int
    password_masked: str = "********"

    class Config:
        from_attributes = True


class MeUpdatePayload(BaseModel):
    current_password: str
    username: str | None = None
    email: str | None = None


class MePasswordUpdatePayload(BaseModel):
    current_password: str
    new_password: str
    new_password_confirm: str


class RevealPasswordPayload(BaseModel):
    current_password: str


class LoginPayload(BaseModel):
    username: str
    password: str
