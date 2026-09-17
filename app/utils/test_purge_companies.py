"""
app/utils/test_purge_companies.py
==================================
Unit tests for scripts/purge_companies.py

Tests use in-memory SQLite (via SQLAlchemy async) and Mock objects
to avoid requiring a real PostgreSQL connection.

Run with:
    python -m pytest app/utils/test_purge_companies.py -v
"""
import asyncio
import os
import sys
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# Ensure scripts/ is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


# ---------------------------------------------------------------------------
# Helpers: fake company objects and audit results
# ---------------------------------------------------------------------------

def _make_audit(
    company_id: int,
    company_name: str,
    company_key: str,
    is_demo: bool = True,
    services: int = 2,
    users: int = 5,
) -> Dict[str, Any]:
    """Build a fake audit_company_resources() result dict."""
    return {
        "company": {
            "company_id": company_id,
            "company_name": company_name,
            "company_key": company_key,
            "is_demo": is_demo,
            "is_active": True,
            "created_at": "2024-01-01 00:00:00",
        },
        "counts": {
            "services": services,
            "users": users,
            "teams": 3,
            "typologies": 1,
            "analyses": 120,
            "mass_jobs": 0,
            "mass_results": 0,
            "training_runs": 4,
            "training_agent_settings": 2,
            "trainer_configs": 1,
            "trainer_simulations": 3,
            "trainer_sessions": 15,
            "prompts": 2,
        },
        "ids": {
            "service_ids": list(range(10, 10 + services)),
            "user_ids": list(range(100, 100 + users)),
            "team_ids": [200],
            "typology_ids": [300],
            "job_ids": [],
            "hubspot_owner_ids": [],
        },
    }


AUDIT_GESALUX = _make_audit(2, "Gesalux", "gesalux", is_demo=True)
AUDIT_DEMO1 = _make_audit(3, "Empresa Demo1", "empresa-demo1", is_demo=True)
AUDIT_BM = _make_audit(1, "Boston Medical", "boston_medical", is_demo=False)
AUDIT_DEMO = _make_audit(7, "Empresa Demo", "empresa_demo", is_demo=True)


# ---------------------------------------------------------------------------
# Import the module under test (after sys.path setup)
# ---------------------------------------------------------------------------

from scripts.purge_companies import (
    _check_absolute_protection,
    _normalize_db_url,
    _verify_company_identity,
    process_company,
    ABSOLUTELY_PROTECTED_IDS,
    EXPECTED_IDENTITY,
)
from scripts.purge_company import CompanyNotFoundError, CompanyProtectedError


# ===========================================================================
# TEST 1: _normalize_db_url converts postgresql:// correctly
# ===========================================================================
def test_normalize_db_url_postgresql():
    url = "postgresql://user:pass@host/db"
    result = _normalize_db_url(url)
    assert result == "postgresql+asyncpg://user:pass@host/db"


def test_normalize_db_url_postgres_alias():
    url = "postgres://user:pass@host/db"
    result = _normalize_db_url(url)
    assert result == "postgresql+asyncpg://user:pass@host/db"


def test_normalize_db_url_already_asyncpg():
    url = "postgresql+asyncpg://user:pass@host/db"
    result = _normalize_db_url(url)
    assert result == url  # no double conversion


# ===========================================================================
# TEST 2: Absolute protection — company_id=1 (Boston Medical)
# ===========================================================================
def test_absolute_guard_company_id_1():
    with pytest.raises(CompanyProtectedError) as exc_info:
        _check_absolute_protection(1)
    assert "ABSOLUTE GUARD" in str(exc_info.value)
    assert "1" in str(exc_info.value)


# ===========================================================================
# TEST 3: Absolute protection — company_id=7 (Empresa Demo)
# ===========================================================================
def test_absolute_guard_company_id_7():
    """company_id=7 must be blocked even without --force-non-demo."""
    with pytest.raises(CompanyProtectedError) as exc_info:
        _check_absolute_protection(7)
    assert "ABSOLUTE GUARD" in str(exc_info.value)
    assert "7" in str(exc_info.value)


# ===========================================================================
# TEST 4: Absolute protection does NOT block company_id=2 or 3
# ===========================================================================
def test_absolute_guard_allows_id_2():
    _check_absolute_protection(2)  # must not raise


def test_absolute_guard_allows_id_3():
    _check_absolute_protection(3)  # must not raise


# ===========================================================================
# TEST 5: Identity verification — name mismatch for company_id=2
# ===========================================================================
def test_identity_verification_name_mismatch_id_2():
    bad_meta = {
        "company_id": 2,
        "company_name": "Boston Medical",  # wrong name
        "company_key": "gesalux",
    }
    with pytest.raises(CompanyProtectedError) as exc_info:
        _verify_company_identity(bad_meta, 2)
    assert "IDENTITY MISMATCH" in str(exc_info.value)
    assert "name" in str(exc_info.value).lower()


# ===========================================================================
# TEST 6: Identity verification — key mismatch for company_id=3
# ===========================================================================
def test_identity_verification_key_mismatch_id_3():
    bad_meta = {
        "company_id": 3,
        "company_name": "Empresa Demo1",   # correct name
        "company_key": "boston_medical",   # wrong key
    }
    with pytest.raises(CompanyProtectedError) as exc_info:
        _verify_company_identity(bad_meta, 3)
    assert "IDENTITY MISMATCH" in str(exc_info.value)
    assert "key" in str(exc_info.value).lower()


# ===========================================================================
# TEST 7: Dry-run for company_id=2 (Gesalux) — passes all guards, returns counts
# ===========================================================================
@pytest.mark.asyncio
async def test_dry_run_gesalux():
    mock_session = AsyncMock()

    with patch(
        "scripts.purge_companies._audit_only",
        new=AsyncMock(return_value=AUDIT_GESALUX),
    ):
        result = await process_company(
            session=mock_session,
            company_id=2,
            apply=False,
            force_non_demo=False,
        )

    assert result["status"] == "dry_run"
    assert result["applied"] is False
    assert result["company"]["company_id"] == 2
    assert result["company"]["company_name"] == "Gesalux"
    assert result["counts"]["services"] == 2
    assert result["counts"]["users"] == 5
    # Dry-run must NOT have deleted anything
    assert result["deleted_records"] == {}


# ===========================================================================
# TEST 8: Dry-run for company_id=3 (Empresa Demo1) — passes all guards
# ===========================================================================
@pytest.mark.asyncio
async def test_dry_run_empresa_demo1():
    mock_session = AsyncMock()

    with patch(
        "scripts.purge_companies._audit_only",
        new=AsyncMock(return_value=AUDIT_DEMO1),
    ):
        result = await process_company(
            session=mock_session,
            company_id=3,
            apply=False,
            force_non_demo=False,
        )

    assert result["status"] == "dry_run"
    assert result["applied"] is False
    assert result["company"]["company_id"] == 3
    assert result["company"]["company_name"] == "Empresa Demo1"
    assert result["deleted_records"] == {}


# ===========================================================================
# TEST 9: process_company raises CompanyProtectedError for company_id=1
# ===========================================================================
@pytest.mark.asyncio
async def test_process_company_blocks_id_1():
    mock_session = AsyncMock()
    with pytest.raises(CompanyProtectedError) as exc_info:
        await process_company(
            session=mock_session,
            company_id=1,
            apply=False,
            force_non_demo=False,
        )
    assert "ABSOLUTE GUARD" in str(exc_info.value)


# ===========================================================================
# TEST 10: process_company raises CompanyProtectedError for company_id=7
# ===========================================================================
@pytest.mark.asyncio
async def test_process_company_blocks_id_7():
    """Even with force_non_demo=True, company_id=7 must be blocked."""
    mock_session = AsyncMock()
    with pytest.raises(CompanyProtectedError) as exc_info:
        await process_company(
            session=mock_session,
            company_id=7,
            apply=False,
            force_non_demo=True,  # even with this flag
        )
    assert "ABSOLUTE GUARD" in str(exc_info.value)


# ===========================================================================
# TEST 11: CompanyNotFoundError propagates when company doesn't exist
# ===========================================================================
@pytest.mark.asyncio
async def test_company_not_found_propagates():
    mock_session = AsyncMock()

    with patch(
        "scripts.purge_companies._audit_only",
        new=AsyncMock(side_effect=CompanyNotFoundError("Company with id=99 was not found.")),
    ):
        with pytest.raises(CompanyNotFoundError) as exc_info:
            await process_company(
                session=mock_session,
                company_id=99,
                apply=False,
                force_non_demo=True,
            )
    assert "99" in str(exc_info.value)


# ===========================================================================
# TEST 12: Dry-run does NOT call purge_company (no writes)
# ===========================================================================
@pytest.mark.asyncio
async def test_dry_run_does_not_call_purge():
    mock_session = AsyncMock()

    with patch(
        "scripts.purge_companies._audit_only",
        new=AsyncMock(return_value=AUDIT_GESALUX),
    ) as mock_audit, patch(
        "scripts.purge_companies.purge_company",
        new=AsyncMock(),
    ) as mock_purge:
        result = await process_company(
            session=mock_session,
            company_id=2,
            apply=False,
            force_non_demo=False,
        )

    mock_audit.assert_called_once()
    mock_purge.assert_not_called()
    assert result["applied"] is False


# ===========================================================================
# TEST 13: Apply mode calls purge_company
# ===========================================================================
@pytest.mark.asyncio
async def test_apply_mode_calls_purge_company():
    mock_session = AsyncMock()

    fake_purge_result = {
        "status": "applied",
        "applied": True,
        "company": AUDIT_GESALUX["company"],
        "counts": AUDIT_GESALUX["counts"],
        "deleted_records": {"bm_companies": 1, "bm_services": 2},
    }

    with patch(
        "scripts.purge_companies._audit_only",
        new=AsyncMock(return_value=AUDIT_GESALUX),
    ), patch(
        "scripts.purge_companies.purge_company",
        new=AsyncMock(return_value=fake_purge_result),
    ) as mock_purge:
        result = await process_company(
            session=mock_session,
            company_id=2,
            apply=True,
            force_non_demo=False,
        )

    mock_purge.assert_called_once()
    call_kwargs = mock_purge.call_args.kwargs
    assert call_kwargs["company_id"] == 2
    assert call_kwargs["apply"] is True
    assert result["applied"] is True
