"""
Lightweight and secure cryptography & token signing utilities.
Implements PBKDF2-SHA256 password hashing and HMAC-SHA256 stateful tokens.
Zero external library dependencies.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

SECRET_KEY = "bm-dev-secret-key-for-token-signing-12345"


def hash_password(password: str) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with django-style output."""
    salt = secrets.token_hex(16)
    iterations = 100000
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return f"pbkdf2_sha256${iterations}${salt}${key.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """Verify raw password against a PBKDF2-SHA256 hash securely."""
    if not hashed or "$" not in hashed:
        return False
    try:
        parts = hashed.split("$")
        if len(parts) != 4:
            return False
        algorithm, iterations, salt, key_hex = parts
        iterations = int(iterations)
        key = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
        )
        return secrets.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def create_access_token(data: dict, expires_in: int = 86400) -> str:
    """Generate a signed access token (stateless JSON payload)."""
    payload = {
        "sub": json.dumps(data),
        "exp": time.time() + expires_in
    }
    payload_json = json.dumps(payload).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("utf-8").rstrip("=")
    
    # Sign signature
    sig = hmac.new(
        SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    
    return f"{payload_b64}.{sig}"


def decode_access_token(token: str) -> dict | None:
    """Decode a signed access token and verify its signature and expiration."""
    if not token or "." not in token:
        return None
    try:
        payload_b64, signature = token.split(".", 1)
        expected_sig = hmac.new(
            SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        
        if not secrets.compare_digest(signature, expected_sig):
            return None
        
        # Add b64 padding back
        padding = len(payload_b64) % 4
        if padding:
            payload_b64 += "=" * (4 - padding)
            
        payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        payload = json.loads(payload_json)
        
        # Check expiration
        if time.time() > payload.get("exp", 0):
            return None
            
        return json.loads(payload.get("sub", "{}"))
    except Exception:
        return None
