"""
Credential resolver for multi-tenant Gemini API keys.
Resolves the appropriate API key and credential alias based on company and service.
"""
import logging
import os
from typing import Tuple

from app.config import get_settings

logger = logging.getLogger(__name__)

# Constants for credential aliases
ALIAS_BOSTON_FRONT = "boston_front"
ALIAS_BOSTON_EXPAC = "boston_expac"
ALIAS_BOSTON_COMERCIALES = "boston_comerciales"
ALIAS_EMPRESA_DEMO = "empresa_demo"
ALIAS_GLOBAL_FALLBACK = "global_fallback"


def resolve_gemini_credentials(
    company_id: int | None = None,
    service_id: int | None = None,
    module: str | None = None,
) -> Tuple[str, str]:
    """
    Resolve Gemini API key and alias based on hierarchy:
    1. company_id=1 + service_id=1 -> GEMINI_KEY_BOSTON_FRONT (alias 'boston_front')
    2. company_id=1 + service_id=2 -> GEMINI_KEY_BOSTON_EXPAC (alias 'boston_expac')
    3. company_id=1 + service_id=3 -> GEMINI_KEY_BOSTON_COMERCIALES (alias 'boston_comerciales')
    4. company_id=7 (any service)   -> GEMINI_KEY_EMPRESA_DEMO (alias 'empresa_demo')
    5. fallback                     -> GEMINI_API_KEY (alias 'global_fallback')

    If a specific variable exists but is empty or whitespace:
    fallback to GEMINI_API_KEY with alias 'global_fallback'.

    Returns:
        (api_key, credential_alias)
    """
    settings = get_settings()

    # 1. Boston Medical (company_id=1)
    if company_id == 1:
        if service_id == 1:
            raw_key = os.environ.get("GEMINI_KEY_BOSTON_FRONT")
            key = raw_key if raw_key is not None else getattr(settings, "gemini_key_boston_front", "")
            if key and key.strip():
                _log_resolved(company_id, service_id, module, ALIAS_BOSTON_FRONT)
                return key.strip(), ALIAS_BOSTON_FRONT
        elif service_id == 2:
            raw_key = os.environ.get("GEMINI_KEY_BOSTON_EXPAC")
            key = raw_key if raw_key is not None else getattr(settings, "gemini_key_boston_expac", "")
            if key and key.strip():
                _log_resolved(company_id, service_id, module, ALIAS_BOSTON_EXPAC)
                return key.strip(), ALIAS_BOSTON_EXPAC
        elif service_id == 3:
            raw_key = os.environ.get("GEMINI_KEY_BOSTON_COMERCIALES")
            key = raw_key if raw_key is not None else getattr(settings, "gemini_key_boston_comerciales", "")
            if key and key.strip():
                _log_resolved(company_id, service_id, module, ALIAS_BOSTON_COMERCIALES)
                return key.strip(), ALIAS_BOSTON_COMERCIALES

    # 2. Empresa Demo (company_id=7)
    elif company_id == 7:
        raw_key = os.environ.get("GEMINI_KEY_EMPRESA_DEMO")
        key = raw_key if raw_key is not None else getattr(settings, "gemini_key_empresa_demo", "")
        if key and key.strip():
            _log_resolved(company_id, service_id, module, ALIAS_EMPRESA_DEMO)
            return key.strip(), ALIAS_EMPRESA_DEMO

    # 3. Global Fallback
    raw_fallback = os.environ.get("GEMINI_API_KEY")
    fallback_key = raw_fallback if raw_fallback is not None else getattr(settings, "gemini_api_key", "")
    _log_resolved(company_id, service_id, module, ALIAS_GLOBAL_FALLBACK)
    return (fallback_key.strip() if fallback_key else ""), ALIAS_GLOBAL_FALLBACK


def _log_resolved(company_id: int | None, service_id: int | None, module: str | None, alias: str) -> None:
    # Safe logging — NEVER print keys or key fragments
    logger.info(
        "Gemini credential resolved: company_id=%s service_id=%s module=%s credential_alias=%s",
        company_id,
        service_id,
        module,
        alias,
    )
