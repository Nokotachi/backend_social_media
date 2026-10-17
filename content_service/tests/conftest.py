import io
import os
import tempfile
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC = Path(tempfile.mkdtemp()) / "jwt_public.pem"
_PUBLIC.write_bytes(
    _KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
)

os.environ.update(
    CONTENT_DB_PASSWORD="test",
    DB_PORT="5433",
    JWT_PUBLIC_KEY_FILE=str(_PUBLIC),
    RABBITMQ_USER="test",
    RABBITMQ_PASSWORD="test",
    EVENTS_ENABLED="false",
    CORS_ORIGINS="http://localhost:5173",
    MINIO_ROOT_USER="test",
    MINIO_ROOT_PASSWORD="test-password",
    MINIO_PORT="9000",
    STORAGE_BACKEND="local",
)

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from content_service.core.bus import OutboxEvent, dispatch
from content_service.database import get_db
from content_service.main import app
from content_service.settings import get_settings
from content_service.users import HANDLERS

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def pytest_collection_modifyitems(items) -> None:
    if not TEST_DATABASE_URL:
        skip = pytest.mark.skip(reason="TEST_DATABASE_URL не задан — нужен Postgres со схемой")
        for item in items:
            item.add_marker(skip)


_TABLES = (
    "posts, post_images, post_likes, reposts, post_favorites, "
    "hidden_posts, comments, comment_likes, post_reports, "
    "user_replicas, follow_replicas, outbox"
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


class FakeUsers:
    def __init__(self, session_factory) -> None:
        self._sf = session_factory
        self._next_id = 1
        self._event_id = 0
        self.by_name: dict[str, dict] = {}

    async def event(self, event_type: str, data: dict) -> None:
        self._event_id += 1
        async with self._sf() as db:
            await dispatch(db, HANDLERS, event_type, self._event_id, data)
            await db.commit()

    async def create(self, username: str, *, role: str = "user") -> dict[str, str]:
        user = {
            "id": self._next_id,
            "username": username,
            "display_name": username,
            "avatar_url": "",
            "role": role,
            "status": "active",
            "token_version": 1,
        }
        self._next_id += 1
        self.by_name[username] = user
        await self.event("user.updated", user)
        token = jwt.encode(
            {
                "sub": str(user["id"]),
                "role": role,
                "token_version": 1,
                "exp": datetime.now(UTC) + timedelta(minutes=15),
            },
            _KEY,
            algorithm="RS256",
        )
        return {"Authorization": f"Bearer {token}"}

    def id(self, username: str) -> int:
        return self.by_name[username]["id"]

    async def follow(self, follower: str, followee: str) -> None:
        await self.event(
            "follow.changed",
            {"follower_id": self.id(follower), "followee_id": self.id(followee), "following": True},
        )

    async def set_status(self, username: str, status: str) -> None:
        self.by_name[username]["status"] = status
        await self.event("user.updated", self.by_name[username])

    async def delete(self, username: str) -> None:
        await self.event("user.deleted", {"id": self.id(username)})

    async def sql(self, query: str, **params) -> None:
        async with self._sf() as s:
            await s.execute(text(query), params)
            await s.commit()

    async def outbox(self) -> list[tuple[str, dict]]:
        async with self._sf() as s:
            rows = await s.scalars(select(OutboxEvent).order_by(OutboxEvent.id))
            return [(e.routing_key, e.payload) for e in rows]


Users = FakeUsers


@pytest.fixture
def users(session_factory) -> FakeUsers:
    return FakeUsers(session_factory)


@pytest_asyncio.fixture
async def client(session_factory, users: FakeUsers, tmp_path) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db():
        async with session_factory() as session:
            yield session

    settings = get_settings()
    settings.media_root = str(tmp_path)
    settings.recommendation_service_url = ""
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def png_bytes(color: str = "red", size: tuple[int, int] = (600, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


async def make_post(
    client: AsyncClient, headers: dict[str, str], caption: str = "", n_images: int = 1
) -> dict:
    files = [("images", (f"{i}.png", png_bytes(), "image/png")) for i in range(n_images)]
    resp = await client.post(
        "/posts", headers=headers, files=files, data={"aspect_ratio": "1:1", "caption": caption}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
