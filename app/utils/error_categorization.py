"""
Helper for categorizing analysis errors and identifying transient retryable conditions.
Used across Gemini / Twilio / HubSpot providers and background evaluation runners.
"""
from typing import Any


def is_transient_llm_error(exc_or_msg: Any) -> bool:
    """
    Checks if an exception or error message corresponds to a transient/retryable LLM/network failure.
    """
    err_str = str(exc_or_msg).lower()
    transient_indicators = [
        "503",
        "unavailable",
        "overloaded",
        "500",
        "internal error",
        "502",
        "bad gateway",
        "429",
        "resource_exhausted",
        "quota",
        "rate limit",
        "server disconnected",
        "connection reset",
        "connection closed",
        "timeout",
        "timed out",
        "remote end closed",
        "econnreset",
        "readtimeouterror",
    ]
    return any(ind in err_str for ind in transient_indicators)


def categorize_error(exc_or_msg: Any) -> dict[str, Any]:
    """
    Categorizes an error string or Exception into a normalized category with metadata.
    Returns:
        {
            "category": str,
            "is_transient": bool,
            "provider": str,
            "friendly_message": str,
            "raw_message": str
        }
    """
    err_str = str(exc_or_msg).strip()
    err_lower = err_str.lower()

    if not err_str:
        return {
            "category": "unknown",
            "is_transient": False,
            "provider": "system",
            "friendly_message": "Error desconocido sin mensaje.",
            "raw_message": "",
        }

    # 1. Gemini / LLM Errors
    if "503" in err_lower or "unavailable" in err_lower or "overloaded" in err_lower:
        return {
            "category": "gemini_overloaded_503",
            "is_transient": True,
            "provider": "gemini",
            "friendly_message": "El modelo de IA (Gemini) está temporalmente saturado (503 UNAVAILABLE).",
            "raw_message": err_str,
        }
    if "500" in err_lower or "internal error" in err_lower:
        return {
            "category": "gemini_transient_500",
            "is_transient": True,
            "provider": "gemini",
            "friendly_message": "Error interno transitorio en la API de IA (Gemini 500 INTERNAL).",
            "raw_message": err_str,
        }
    if "429" in err_lower or "resource_exhausted" in err_lower or "rate limit" in err_lower or "quota" in err_lower:
        return {
            "category": "gemini_rate_limit_429",
            "is_transient": True,
            "provider": "gemini",
            "friendly_message": "Límite de tasa / cuota alcanzado en Gemini (429 RESOURCE_EXHAUSTED).",
            "raw_message": err_str,
        }
    if "502" in err_lower or "bad gateway" in err_lower:
        return {
            "category": "gemini_bad_gateway_502",
            "is_transient": True,
            "provider": "gemini",
            "friendly_message": "Pasarela no disponible al conectar con Gemini (502 Bad Gateway).",
            "raw_message": err_str,
        }
    if "json" in err_lower and ("válido" in err_lower or "valid" in err_lower or "decode" in err_lower or "parse" in err_lower):
        return {
            "category": "gemini_invalid_json",
            "is_transient": False,
            "provider": "gemini",
            "friendly_message": "La respuesta de la IA no cumplió con la estructura JSON requerida.",
            "raw_message": err_str,
        }

    # 2. Twilio Errors
    if "404" in err_lower and ("twilio" in err_lower or "recording" in err_lower or "api.twilio.com" in err_lower):
        return {
            "category": "twilio_recording_not_found",
            "is_transient": False,
            "provider": "twilio",
            "friendly_message": "La grabación no existe o fue eliminada en Twilio (404 Not Found).",
            "raw_message": err_str,
        }
    if "no recording url" in err_lower or "recording url present" in err_lower:
        return {
            "category": "twilio_missing_recording_url",
            "is_transient": False,
            "provider": "twilio",
            "friendly_message": "La llamada no dispone de URL de grabación.",
            "raw_message": err_str,
        }

    # 3. Transport / Network Errors
    if any(k in err_lower for k in ["server disconnected", "connection reset", "connection closed", "timed out", "timeout", "remote end closed"]):
        return {
            "category": "transport_error",
            "is_transient": True,
            "provider": "network",
            "friendly_message": "Corte de conexión o timeout durante la transferencia de red.",
            "raw_message": err_str,
        }

    # 4. Credentials / Auth Errors
    if any(k in err_lower for k in ["token", "credential", "bearer", "unauthorized", "401", "403", "forbidden"]):
        return {
            "category": "missing_credentials",
            "is_transient": False,
            "provider": "auth",
            "friendly_message": "Fallo de autenticación o credenciales no configuradas.",
            "raw_message": err_str,
        }

    # 5. Code / System Errors
    if any(k in err_lower for k in ["greenlet", "attributeerror", "keyerror", "typeerror", "valueerror", "unhashable"]):
        return {
            "category": "code_error",
            "is_transient": False,
            "provider": "backend",
            "friendly_message": f"Error de ejecución interno: {err_str}",
            "raw_message": err_str,
        }

    # 6. Abandoned execution
    if "abandoned" in err_lower or "heartbeat" in err_lower or "stale" in err_lower:
        return {
            "category": "system_abandoned",
            "is_transient": False,
            "provider": "system",
            "friendly_message": "Ejecución detenida o interrumpida por inactividad de proceso.",
            "raw_message": err_str,
        }

    return {
        "category": "unknown",
        "is_transient": False,
        "provider": "unknown",
        "friendly_message": err_str,
        "raw_message": err_str,
    }
