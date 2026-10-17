import os
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

from user_service.keygen import PRIVATE_NAME, ensure_keys

_KEYS = Path(tempfile.mkdtemp())
ensure_keys(_KEYS, _KEYS)

os.environ.update(
    USER_DB_PASSWORD="test",
    DB_PORT="5433",
    JWT_PRIVATE_KEY_FILE=str(_KEYS / PRIVATE_NAME),
    RABBITMQ_USER="test",
    RABBITMQ_PASSWORD="test",
    EVENTS_ENABLED="false",
    CORS_ORIGINS="http://localhost:5173",
    MINIO_ROOT_USER="test",
    MINIO_ROOT_PASSWORD="test-password",
    MINIO_PORT="9000",
    STORAGE_BACKEND="local",
    EMAIL_BACKEND="console",
)

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from user_service.auth import models as _auth_models  # noqa: F401
from user_service.core import bus as _bus  # noqa: F401
from user_service.core.email import get_email_sender
from user_service.database import Base, get_db
from user_service.main import app
from user_service.users import events as _events  # noqa: F401
from user_service.users import models as _users_models  # noqa: F401


class SpyEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_verification_code(self, *, to_email: str, code: str) -> None:
        self.sent.append(("verify", to_email, code))

    async def send_password_reset_code(self, *, to_email: str, code: str) -> None:
        self.sent.append(("reset", to_email, code))

    async def send_password_change_code(self, *, to_email: str, code: str) -> None:
        self.sent.append(("change", to_email, code))

    def last_code(self) -> str:
        return self.sent[-1][2]


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
_PG_TABLES = (
    "users, profiles, follows, refresh_tokens, email_tokens, password_reset_tokens, "
    "user_post_counts, outbox"
)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    if TEST_DATABASE_URL:
        engine = create_async_engine(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {_PG_TABLES} RESTART IDENTITY CASCADE"))
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def email_sender() -> SpyEmailSender:
    return SpyEmailSender()


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, email_sender: SpyEmailSender
) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_email_sender] = lambda: email_sender
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_login_guard():
    from user_service.auth import login_guard

    login_guard.clear_all()
    yield
    login_guard.clear_all()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
