from dataclasses import dataclass
from typing import Any

from fastapi import Depends
from sqlalchemy import BigInteger, Boolean, Integer, String, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from content_service.core import bus
from content_service.core.storage import get_storage
from content_service.database import Base, get_db
from content_service.models import (
    Comment,
    CommentLike,
    HiddenPost,
    Post,
    PostFavorite,
    PostImage,
    PostLike,
    PostReport,
    PostStatus,
    Repost,
)

QUEUE = "content_service"
ACTIVE = "active"


class UserReplica(Base):
    __tablename__ = "user_replicas"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(30), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class FollowReplica(Base):
    __tablename__ = "follow_replicas"

    follower_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    followee_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    following: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


@dataclass(frozen=True)
class AuthInfo:
    id: int
    role: str
    status: str
    token_version: int


@dataclass(frozen=True)
class Author:
    id: int
    username: str
    display_name: str
    avatar_url: str


class UserDirectory:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def auth_info(self, user_id: int) -> AuthInfo | None:
        user = await self._db.get(UserReplica, user_id)
        if user is None:
            return None
        return AuthInfo(user.user_id, user.role, user.status, user.token_version)

    async def hidden_ids(self) -> frozenset[int]:
        rows = await self._db.scalars(
            select(UserReplica.user_id).where(UserReplica.status != ACTIVE)
        )
        return frozenset(rows)

    async def authors(self, ids: set[int]) -> dict[int, Author]:
        if not ids:
            return {}
        rows = await self._db.scalars(
            select(UserReplica).where(UserReplica.user_id.in_(ids), UserReplica.status == ACTIVE)
        )
        return {
            u.user_id: Author(u.user_id, u.username, u.display_name, u.avatar_url) for u in rows
        }

    async def id_by_username(self, username: str) -> int | None:
        return await self._db.scalar(
            select(UserReplica.user_id).where(
                func.lower(UserReplica.username) == username.lower(),
                UserReplica.status == ACTIVE,
            )
        )

    async def following_ids(self, user_id: int) -> list[int]:
        rows = await self._db.scalars(
            select(FollowReplica.followee_id)
            .join(UserReplica, UserReplica.user_id == FollowReplica.followee_id)
            .where(
                FollowReplica.follower_id == user_id,
                FollowReplica.following.is_(True),
                UserReplica.status == ACTIVE,
            )
        )
        return list(rows)


def get_user_directory(db: AsyncSession = Depends(get_db)) -> UserDirectory:
    return UserDirectory(db)


async def on_user_updated(db: AsyncSession, event_id: int, data: dict[str, Any]) -> None:
    user = await db.get(UserReplica, data["id"], with_for_update=True)
    if user is None:
        user = UserReplica(user_id=data["id"], last_event_id=0)
        db.add(user)
    elif event_id <= user.last_event_id or user.status == "deleted":
        return
    for field in ("username", "display_name", "avatar_url", "role", "status", "token_version"):
        setattr(user, field, data[field])
    user.last_event_id = event_id


async def on_user_deleted(db: AsyncSession, event_id: int, data: dict[str, Any]) -> None:
    user_id = int(data["id"])
    user = await db.get(UserReplica, user_id, with_for_update=True)
    if user is None:
        db.add(
            UserReplica(
                user_id=user_id,
                username="",
                display_name="",
                role="user",
                status="deleted",
                token_version=0,
                last_event_id=event_id,
            )
        )
    else:
        user.status = "deleted"
        user.last_event_id = max(user.last_event_id, event_id)
    urls = list(
        await db.scalars(
            select(PostImage.image_url)
            .join(Post, Post.id == PostImage.post_id)
            .where(Post.author_id == user_id)
        )
    )
    await db.execute(delete(Post).where(Post.author_id == user_id))
    await db.execute(delete(Comment).where(Comment.author_id == user_id))
    await db.execute(delete(CommentLike).where(CommentLike.user_id == user_id))
    for model in (PostLike, Repost, PostFavorite, HiddenPost):
        await db.execute(delete(model).where(model.user_id == user_id))
    await db.execute(delete(PostReport).where(PostReport.reporter_id == user_id))
    await db.execute(
        delete(FollowReplica).where(
            (FollowReplica.follower_id == user_id) | (FollowReplica.followee_id == user_id)
        )
    )
    await db.commit()
    files = get_storage()
    for url in urls:
        await files.delete_by_url(url)


async def on_follow_changed(db: AsyncSession, event_id: int, data: dict[str, Any]) -> None:
    key = (data["follower_id"], data["followee_id"])
    row = await db.get(FollowReplica, key, with_for_update=True)
    if row is None:
        db.add(
            FollowReplica(
                follower_id=key[0],
                followee_id=key[1],
                following=data["following"],
                last_event_id=event_id,
            )
        )
    elif event_id > row.last_event_id:
        row.following = data["following"]
        row.last_event_id = event_id


HANDLERS: dict[str, bus.Handler] = {
    "user.updated": on_user_updated,
    "user.deleted": on_user_deleted,
    "follow.changed": on_follow_changed,
}


async def emit_posts_count(db: AsyncSession, author_id: int) -> None:
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": author_id})
    count = await db.scalar(
        select(func.count())
        .select_from(Post)
        .where(Post.author_id == author_id, Post.status == PostStatus.PUBLISHED)
    )
    bus.add_event(db, "posts.counted", {"user_id": author_id, "posts_count": count})


async def enqueue_snapshot(db: AsyncSession) -> None:
    rows = await db.execute(
        select(Post.author_id, func.count())
        .where(Post.status == PostStatus.PUBLISHED)
        .group_by(Post.author_id)
    )
    for author_id, count in rows:
        bus.add_event(db, "posts.counted", {"user_id": author_id, "posts_count": count})
    await db.commit()
