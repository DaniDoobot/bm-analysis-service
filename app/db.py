"""
Database engine and session factory.
Uses SQLAlchemy 2.x async engine with asyncpg.

The engine is created lazily on first access so the app can import
even if DATABASE_URL is not yet set (useful for testing/import checks).
"""
from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


def _make_async_url(raw_url: str) -> str:
    """Convert a standard PostgreSQL URL to asyncpg dialect."""
    if not raw_url:
        return ""
    url = raw_url
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix):]
            break
    return url


@lru_cache(maxsize=1)
def _get_engine():
    settings = get_settings()
    async_url = _make_async_url(settings.database_url)
    if not async_url:
        raise RuntimeError(
            "DATABASE_URL is not configured. "
            "Set DATABASE_URL in your .env file or environment."
        )
    return create_async_engine(
        async_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


@lru_cache(maxsize=1)
def _get_session_factory():
    return async_sessionmaker(
        _get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


# Public accessors used by dependencies.py
def get_engine():
    return _get_engine()


def get_session_factory():
    return _get_session_factory()


# Keep backwards-compatible names
engine = None          # use get_engine() instead
AsyncSessionLocal = None  # use get_session_factory() instead
