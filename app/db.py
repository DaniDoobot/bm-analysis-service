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


def assert_not_production_db_for_tests() -> None:
    """
    Blocks execution if the database URL points to the production database.
    Can be bypassed if ALLOW_PRODUCTION_TESTS=true is set in the environment.
    """
    import os
    from app.config import get_settings
    settings = get_settings()
    raw_url = settings.database_url or ""
    
    is_prod = "91.98.230.119" in raw_url or "n8n" in raw_url
    allow_prod = os.environ.get("ALLOW_PRODUCTION_TESTS", "false").lower() == "true"
    if is_prod and not allow_prod:
        raise RuntimeError(
            f"CRITICAL SAFETY VIOLATION: Test or diagnostic script attempted to connect to the production database: '{raw_url}'. "
            "Execution is blocked for safety. Set ALLOW_PRODUCTION_TESTS=true in env to override."
        )


@lru_cache(maxsize=1)
def _get_engine():
    settings = get_settings()
    async_url = _make_async_url(settings.database_url)
    if not async_url:
        raise RuntimeError(
            "DATABASE_URL is not configured. "
            "Set DATABASE_URL in your .env file or environment."
        )
    
    # Run safety check only under test environment conditions
    import os
    is_test_env = (
        os.environ.get("APP_ENV") == "test"
        or "PYTEST_CURRENT_TEST" in os.environ
    )
    if is_test_env:
        assert_not_production_db_for_tests()

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
engine = get_engine()
SessionLocal = get_session_factory()
AsyncSessionLocal = get_session_factory()
