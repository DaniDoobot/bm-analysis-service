"""Common Pydantic schemas shared across the application."""
from typing import Any

from pydantic import BaseModel


class OkResponse(BaseModel):
    ok: bool = True
    status: str = "ok"


class ErrorResponse(BaseModel):
    ok: bool = False
    status: str = "error"
    error_message: str
    details: Any | None = None
