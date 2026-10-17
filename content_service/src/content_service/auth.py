from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated

import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from content_service.settings import get_settings
from content_service.users import UserDirectory, get_user_directory

ALGORITHM = "RS256"
_bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def public_key():
    path = get_settings().jwt_public_key_file
    if not path.exists():
        raise RuntimeError(
            f"Нет публичного ключа JWT {path}. Создайте: python -m user_service.keygen ../keys"
        )
    return serialization.load_pem_public_key(path.read_bytes())


@dataclass(frozen=True)
class CurrentUser:
    id: int
    role: str
    hidden_author_ids: frozenset[int] = frozenset()
    users: UserDirectory | None = field(default=None, compare=False, repr=False)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    users: UserDirectory = Depends(get_user_directory),
) -> CurrentUser:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="InvalidAccessToken",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        payload = jwt.decode(credentials.credentials, public_key(), algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
        role = str(payload["role"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise unauthorized from exc

    info = await users.auth_info(user_id)
    if info is not None:
        if payload.get("token_version") != info.token_version:
            raise unauthorized
        if info.status != "active":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AccountNotActive")
        role = info.role
    return CurrentUser(
        id=user_id, role=role, hidden_author_ids=await users.hidden_ids(), users=users
    )


async def get_current_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AdminOnly")
    return user


Auth = Annotated[CurrentUser, Depends(get_current_user)]
AdminAuth = Annotated[CurrentUser, Depends(get_current_admin)]
