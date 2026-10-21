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

import string

HISTORICAL_SECRET_KEY = "bm-dev-secret-key-for-token-signing-12345"


def get_secret_key() -> str:
    """
    Resolve JWT secret key from environment or app settings.
    Guarantees 100% backward compatibility by defaulting to HISTORICAL_SECRET_KEY.
    """
    import os
    env_val = os.environ.get("JWT_SECRET_KEY")
    if env_val and env_val.strip():
        return env_val.strip()
    try:
        from app.config import get_settings
        s = get_settings()
        configured = getattr(s, "jwt_secret_key", None)
        if configured and configured.strip():
            return configured.strip()
    except Exception:
        pass
    return HISTORICAL_SECRET_KEY


SECRET_KEY = get_secret_key()

SPECIAL_CHARACTERS = "!@#$%&*_-."


def validate_password_policy(password: str) -> str:
    """
    Validates canonical password policy:
    - Minimum 10 characters
    - At least 1 special character (non-alphanumeric and non-whitespace)
    """
    if not password:
        raise ValueError("La contraseña no puede estar vacía.")
    if len(password) < 10:
        raise ValueError("La contraseña debe tener al menos 10 caracteres.")
    if not any(not c.isalnum() and not c.isspace() for c in password):
        raise ValueError("La contraseña debe incluir al menos un carácter especial.")
    return password


def generate_temporary_password(length: int = 16) -> str:
    """
    Generates a cryptographically secure temporary password using `secrets`
    guaranteed to satisfy `validate_password_policy`.
    """
    if length < 10:
        length = 16
    specials = [secrets.choice(SPECIAL_CHARACTERS) for _ in range(2)]
    digits = [secrets.choice(string.digits) for _ in range(2)]
    letters = [secrets.choice(string.ascii_letters) for _ in range(length - 4)]
    all_chars = specials + digits + letters
    sys_rand = secrets.SystemRandom()
    sys_rand.shuffle(all_chars)
    pwd = "".join(all_chars)
    return validate_password_policy(pwd)


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


def create_access_token(data: dict, expires_in: int = 86400, secret_key: str | None = None) -> str:
    """Generate a signed access token (stateless JSON payload)."""
    payload = {
        "sub": json.dumps(data),
        "exp": time.time() + expires_in
    }
    payload_json = json.dumps(payload).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("utf-8").rstrip("=")
    
    # Sign signature
    sig = hmac.new(
        (secret_key or get_secret_key() or SECRET_KEY).encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    
    return f"{payload_b64}.{sig}"


def decode_access_token(token: str, secret_key: str | None = None) -> dict | None:
    """Decode a signed access token and verify its signature and expiration."""
    if not token or "." not in token:
        return None
    try:
        payload_b64, signature = token.split(".", 1)
        expected_sig = hmac.new(
            (secret_key or get_secret_key() or SECRET_KEY).encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
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
