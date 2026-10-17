from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Integer, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from user_service.auth.models import User
from user_service.core import bus
from user_service.database import Base
from user_service.users.models import Follow, Profile

QUEUE = "user_service"


class UserPostCount(Base):
    __tablename__ = "user_post_counts"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    posts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


def _snapshot(user: User, profile: Profile) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": profile.display_name,
        "avatar_url": profile.avatar_url,
        "role": user.role,
        "status": user.status,
        "token_version": user.token_version,
    }


async def _lock_user(db: AsyncSession, user_id: int) -> None:
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())


async def emit_user(db: AsyncSession, user_id: int) -> None:
    await db.flush()
    await _lock_user(db, user_id)
    user = await db.get(User, user_id, populate_existing=True)
    profile = await db.get(Profile, user_id, populate_existing=True)
    bus.add_event(db, "user.updated", _snapshot(user, profile))


async def emit_user_deleted(db: AsyncSession, user_id: int) -> None:
    bus.add_event(db, "user.deleted", {"id": user_id})


async def emit_follow(db: AsyncSession, follower_id: int, followee_id: int) -> None:
    await db.flush()
    await _lock_user(db, follower_id)
    following = await db.get(Follow, (follower_id, followee_id), populate_existing=True)
    bus.add_event(
        db,
        "follow.changed",
        {
            "follower_id": follower_id,
            "followee_id": followee_id,
            "following": following is not None,
        },
    )


async def posts_count(db: AsyncSession, user_id: int) -> int:
    return (
        await db.scalar(select(UserPostCount.posts_count).where(UserPostCount.user_id == user_id))
        or 0
    )


async def on_posts_counted(db: AsyncSession, event_id: int, data: dict[str, Any]) -> None:
    user_id = int(data["user_id"])
    if await db.get(User, user_id) is None:
        return
    row = await db.get(UserPostCount, user_id, with_for_update=True)
    if row is None:
        db.add(
            UserPostCount(user_id=user_id, posts_count=data["posts_count"], last_event_id=event_id)
        )
    elif event_id > row.last_event_id:
        row.posts_count = data["posts_count"]
        row.last_event_id = event_id


HANDLERS: dict[str, bus.Handler] = {"posts.counted": on_posts_counted}


async def enqueue_snapshot(db: AsyncSession) -> None:
    rows = await db.execute(select(User, Profile).join(Profile, Profile.user_id == User.id))
    for user, profile in rows:
        bus.add_event(db, "user.updated", _snapshot(user, profile))
    for follower_id, followee_id in await db.execute(
        select(Follow.follower_id, Follow.followee_id)
    ):
        bus.add_event(
            db,
            "follow.changed",
            {"follower_id": follower_id, "followee_id": followee_id, "following": True},
        )
    await db.commit()
