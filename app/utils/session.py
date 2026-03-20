"""Session JWT and API key encryption utilities."""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet

from app.config import settings

logger = logging.getLogger(__name__)

SESSION_JWT_EXPIRY_HOURS = 24
SESSION_JWT_ALGORITHM = "HS256"


def _get_jwt_secret() -> str:
    """Return the JWT signing secret. Raises if not configured."""
    if not settings.SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET is not configured")
    return settings.SESSION_SECRET


def _get_fernet() -> Fernet:
    """Derive a Fernet key from SESSION_SECRET."""
    secret = _get_jwt_secret()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def create_session_jwt(user_id: str, email: str) -> str:
    """Create a 24-hour session JWT for dashboard use."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "type": "session",
        "iat": now,
        "exp": now + timedelta(hours=SESSION_JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=SESSION_JWT_ALGORITHM)


def decode_session_jwt(token: str) -> dict:
    """Decode and validate a session JWT.

    Returns the claims dict on success.
    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure.
    """
    claims = jwt.decode(
        token,
        _get_jwt_secret(),
        algorithms=[SESSION_JWT_ALGORITHM],
    )
    # Verify it's a session token (not some other JWT)
    if claims.get("type") != "session":
        raise jwt.InvalidTokenError("Not a session token")
    return claims


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an API key using Fernet (AES). Returns base64 ciphertext."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt an API key from Fernet ciphertext."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()
