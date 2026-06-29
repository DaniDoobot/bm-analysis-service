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


def enforce_destructive_safety(is_test: bool = True) -> None:
    """
    Unconditionally blocks execution if the database URL points to the production database
    (host '91.98.230.119', database name 'n8n', or containing production/prod tags).
    If is_test is True, also enforces that the database name must contain '_test'.
    """
    from app.config import get_settings
    settings = get_settings()
    raw_url = settings.database_url or ""
    
    url_lower = raw_url.lower()
    db_name = raw_url.split("/")[-1].split("?")[0] if "/" in raw_url else ""
    db_name_lower = db_name.lower()
    
    is_prod = (
        "91.98.230.119" in raw_url
        or db_name_lower == "n8n"
        or "speechbm.doobot.ai" in url_lower
        or ("prod" in url_lower and "_test" not in db_name_lower and "_dev" not in db_name_lower)
    )
    
    if is_prod:
        raise RuntimeError(
            "\n"
            "===============================================================================\n"
            "   CRITICAL SAFETY VIOLATION: OPERATION BLOCKED AGAINST PRODUCTION DATABASE\n"
            "===============================================================================\n"
            f"The database URL points to production: '{raw_url}'\n"
            "Execution of tests, cleanups, or destructive scripts against production is FORBIDDEN.\n"
            "==============================================================================="
        )
        
    if is_test:
        has_test_in_name = "_test" in db_name_lower
        if not has_test_in_name:
            raise RuntimeError(
                "\n"
                "===============================================================================\n"
                "   CRITICAL SAFETY VIOLATION: DATABASE NAME DOES NOT CONTAIN '_test'\n"
                "===============================================================================\n"
                f"The database name '{db_name}' is not allowed for test execution / destructive work.\n"
                "Tests and mock seeding require a database name containing '_test' (e.g. 'n8n_test').\n"
                "==============================================================================="
            )


def assert_not_production_db_for_tests() -> None:
    """
    Blocks execution unconditionally if the database URL points to the production database
    or if the database name does not contain '_test'.
    """
    enforce_destructive_safety(is_test=True)



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
    import sys
    
    main_file = sys.argv[0].lower() if (sys.argv and sys.argv[0]) else ""
    is_test_script = "test_" in main_file or "test.py" in main_file or "_test" in main_file
    
    is_test_env = (
        os.environ.get("APP_ENV") == "test"
        or "PYTEST_CURRENT_TEST" in os.environ
        or is_test_script
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
