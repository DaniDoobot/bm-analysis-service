"""
Unit tests for multi-tenant Gemini API credentials resolver.
Verifies hierarchical resolution by company_id, service_id, and fallbacks.
"""
import os
import logging
from unittest.mock import patch

import pytest

from app.services.ai_credentials import (
    resolve_gemini_credentials,
    ALIAS_BOSTON_FRONT,
    ALIAS_BOSTON_EXPAC,
    ALIAS_BOSTON_COMERCIALES,
    ALIAS_EMPRESA_DEMO,
    ALIAS_GLOBAL_FALLBACK,
)
from app.services.ai_provider import get_ai_provider, GeminiProvider


@pytest.fixture(autouse=True)
def clean_gemini_env():
    """Ensure a clean environment for each test."""
    env_keys = [
        "GEMINI_KEY_BOSTON_FRONT",
        "GEMINI_KEY_BOSTON_EXPAC",
        "GEMINI_KEY_BOSTON_COMERCIALES",
        "GEMINI_KEY_EMPRESA_DEMO",
        "GEMINI_API_KEY",
    ]
    old_env = {k: os.environ.get(k) for k in env_keys}
    
    # Set default fallback
    os.environ["GEMINI_API_KEY"] = "default-global-key-12345"
    for k in env_keys[:-1]:
        os.environ.pop(k, None)

    yield

    for k, v in old_env.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def test_boston_front_resolution():
    os.environ["GEMINI_KEY_BOSTON_FRONT"] = "key-boston-front-abc"
    key, alias = resolve_gemini_credentials(company_id=1, service_id=1, module="mass_evaluations")
    assert key == "key-boston-front-abc"
    assert alias == ALIAS_BOSTON_FRONT


def test_boston_expac_resolution():
    os.environ["GEMINI_KEY_BOSTON_EXPAC"] = "key-boston-expac-xyz"
    key, alias = resolve_gemini_credentials(company_id=1, service_id=2, module="mass_evaluations")
    assert key == "key-boston-expac-xyz"
    assert alias == ALIAS_BOSTON_EXPAC


def test_boston_comerciales_resolution():
    os.environ["GEMINI_KEY_BOSTON_COMERCIALES"] = "key-boston-comerciales-789"
    key, alias = resolve_gemini_credentials(company_id=1, service_id=3, module="mass_evaluations")
    assert key == "key-boston-comerciales-789"
    assert alias == ALIAS_BOSTON_COMERCIALES


def test_empresa_demo_resolution():
    os.environ["GEMINI_KEY_EMPRESA_DEMO"] = "key-empresa-demo-999"
    
    # Any service in company 7 should resolve to Empresa Demo key
    for sid in [1, 2, 10, 11, None]:
        key, alias = resolve_gemini_credentials(company_id=7, service_id=sid, module="cycles")
        assert key == "key-empresa-demo-999"
        assert alias == ALIAS_EMPRESA_DEMO


def test_unknown_company_fallback():
    key, alias = resolve_gemini_credentials(company_id=999, service_id=1, module="test_analysis")
    assert key == "default-global-key-12345"
    assert alias == ALIAS_GLOBAL_FALLBACK


def test_boston_unconfigured_service_fallback():
    # Boston company_id=1, but unknown service_id=99
    os.environ["GEMINI_KEY_BOSTON_FRONT"] = "key-boston-front-abc"
    key, alias = resolve_gemini_credentials(company_id=1, service_id=99, module="mass_evaluations")
    assert key == "default-global-key-12345"
    assert alias == ALIAS_GLOBAL_FALLBACK


def test_boston_missing_env_fallback():
    # Boston Front service, but env var not set
    os.environ.pop("GEMINI_KEY_BOSTON_FRONT", None)
    key, alias = resolve_gemini_credentials(company_id=1, service_id=1, module="mass_evaluations")
    assert key == "default-global-key-12345"
    assert alias == ALIAS_GLOBAL_FALLBACK


def test_boston_empty_or_whitespace_env_fallback():
    # Boston Front service, but env var is empty or spaces
    os.environ["GEMINI_KEY_BOSTON_FRONT"] = "   "
    key, alias = resolve_gemini_credentials(company_id=1, service_id=1, module="mass_evaluations")
    assert key == "default-global-key-12345"
    assert alias == ALIAS_GLOBAL_FALLBACK


def test_empresa_demo_empty_env_fallback():
    # Empresa Demo, but env var is empty
    os.environ["GEMINI_KEY_EMPRESA_DEMO"] = ""
    key, alias = resolve_gemini_credentials(company_id=7, service_id=1, module="cycles")
    assert key == "default-global-key-12345"
    assert alias == ALIAS_GLOBAL_FALLBACK


def test_ai_provider_factory_integration():
    os.environ["GEMINI_KEY_BOSTON_FRONT"] = "key-boston-front-abc"
    provider = get_ai_provider(company_id=1, service_id=1, module="mass_evaluations")
    
    assert isinstance(provider, GeminiProvider)
    assert provider.api_key == "key-boston-front-abc"
    assert provider.credential_alias == ALIAS_BOSTON_FRONT


def test_logging_safety_no_key_leaks(caplog):
    secret_key = "super-secret-unique-gemini-key-998877"
    os.environ["GEMINI_KEY_BOSTON_FRONT"] = secret_key

    with caplog.at_level(logging.INFO):
        key, alias = resolve_gemini_credentials(company_id=1, service_id=1, module="audit_test")
        assert key == secret_key
        assert alias == ALIAS_BOSTON_FRONT

    # Ensure the secret key or fragments do not appear anywhere in the logs
    for record in caplog.records:
        assert secret_key not in record.message
        assert "super-secret" not in record.message
        assert "998877" not in record.message
