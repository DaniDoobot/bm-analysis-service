"""
app/utils/sanitization.py
=========================
Utilities to sanitize sensitive fields from API responses.
"""
from typing import Any, Dict, Optional

SENSITIVE_RECORDING_KEYS = {
    "recording_url",
    "recordingurl",
    "hs_call_recording_url",
    "audio_url",
    "audiourl",
    "media_url",
    "mediaurl",
    "recording_uri",
    "recordinguri",
    "audio_uri",
    "audiouri",
}


def sanitize_hubspot_metadata(meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Returns a sanitized copy of hubspot_metadata with all sensitive recording/audio
    URLs removed, preventing exposure of external Twilio/CDN storage locations.
    
    - Does NOT mutate the input dict or database ORM instances.
    - Preserves all other non-recording fields intact.
    - Recursively sanitizes nested dictionaries.
    """
    if meta is None or not isinstance(meta, dict):
        return meta

    sanitized: Dict[str, Any] = {}
    for key, val in meta.items():
        key_norm = str(key).lower().replace("-", "_")

        # 1. Exact match on known recording / audio keys
        if key_norm in SENSITIVE_RECORDING_KEYS:
            continue

        # 2. Key name indicates recording/audio URL
        if ("recording" in key_norm or "audio" in key_norm or "media" in key_norm) and (
            "url" in key_norm or "uri" in key_norm or "link" in key_norm
        ):
            continue

        # 3. If key is 'recording' and value is a URL string
        if key_norm == "recording" and isinstance(val, str) and (
            val.startswith("http://") or val.startswith("https://")
        ):
            continue

        # 4. If value is a string containing twilio or recording storage leak
        if isinstance(val, str) and (
            "api.twilio.com" in val or "demo.doobot.ai/recordings" in val
        ):
            continue

        # 5. Recursive sanitization for nested dictionaries
        if isinstance(val, dict):
            sanitized[key] = sanitize_hubspot_metadata(val)
        else:
            sanitized[key] = val

    return sanitized
