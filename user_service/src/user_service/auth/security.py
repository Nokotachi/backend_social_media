import hashlib
import secrets
import string
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives import serialization

from user_service.settings import get_settings

settings = get_settings()
_password_hasher = PasswordHasher()


def hash_password(raw_password: str) -> str:
    return _password_hasher.hash(raw_password)


def verify_password(raw_password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, raw_password)
    except VerifyMismatchError:
        return False


def generate_opaque_token(num_bytes: int) -> str:
    return secrets.token_urlsafe(num_bytes)


def hash_opaque_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_email_code(length: int) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))


def hash_email_code(code: str) -> str:
    return hash_opaque_token(code)


ALGORITHM = "RS256"


@lru_cache
def _private_key():
    path = settings.jwt_private_key_file
    if not path.exists():
        raise RuntimeError(
            f"Нет приватного ключа JWT {path}. Создайте: python -m user_service.keygen ../keys"
        )
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def create_access_token(*, user_id: int, role: str, token_version: int) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "token_version": token_version,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(payload, _private_key(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, _private_key().public_key(), algorithms=[ALGORITHM])
