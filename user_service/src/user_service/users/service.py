from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from user_service.auth import security
from user_service.auth.models import RefreshToken, User, UserRole, UserStatus
from user_service.core.pagination import decode_cursor, encode_cursor
from user_service.users import events
from user_service.users.exceptions import (
    CannotBlockAdmin,
    CannotFollowYourself,
    ProfileNotFound,
    WrongPassword,
)
from user_service.users.models import Follow, Profile
from user_service.users.schemas import (
    FollowResponse,
    MeResponse,
    ProfilePage,
    ProfileResponse,
    ProfileShort,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _active_profiles():
    return (
        select(Profile)
        .join(User, User.id == Profile.user_id)
        .where(User.status == UserStatus.ACTIVE.value)
    )


async def _get_active_profile(db: AsyncSession, username: str) -> Profile:
    profile = await db.scalar(
        _active_profiles().where(func.lower(Profile.username) == username.lower())
    )
    if profile is None:
        raise ProfileNotFound()
    return profile


async def _following_set(db: AsyncSession, viewer_id: int, user_ids: list[int]) -> set[int]:
    if not user_ids:
        return set()
    rows = await db.scalars(
        select(Follow.followee_id).where(
            Follow.follower_id == viewer_id, Follow.followee_id.in_(user_ids)
        )
    )
    return set(rows)


def _short(profile: Profile, following: set[int]) -> ProfileShort:
    return ProfileShort(
        id=profile.user_id,
        username=profile.username,
        display_name=profile.display_name,
        avatar_url=profile.avatar_url,
        is_following=profile.user_id in following,
    )


async def get_me(db: AsyncSession, *, user: User) -> MeResponse:
    profile = await db.get(Profile, user.id)
    return MeResponse(
        id=user.id,
        email=user.email,
        username=user.username,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        display_name=profile.display_name,
        bio=profile.bio,
        avatar_url=profile.avatar_url,
        posts_count=await events.posts_count(db, user.id),
        follower_count=profile.follower_count,
        followee_count=profile.followee_count,
    )


async def update_me(db: AsyncSession, *, user: User, changes: dict) -> MeResponse:
    profile = await db.get(Profile, user.id)
    for field in ("display_name", "bio", "avatar_url"):
        if field in changes and changes[field] is not None:
            setattr(profile, field, changes[field].strip() if field != "bio" else changes[field])
    await events.emit_user(db, user.id)
    await db.commit()
    return await get_me(db, user=user)


async def delete_me(db: AsyncSession, *, user: User, password: str) -> str:
    if not security.verify_password(password, user.password_hash):
        raise WrongPassword()
    profile = await db.get(Profile, user.id)
    avatar_url = profile.avatar_url if profile else ""

    followee_ids = select(Follow.followee_id).where(Follow.follower_id == user.id)
    follower_ids = select(Follow.follower_id).where(Follow.followee_id == user.id)
    await db.execute(
        update(Profile)
        .where(Profile.user_id.in_(followee_ids))
        .values(follower_count=Profile.follower_count - 1)
    )
    await db.execute(
        update(Profile)
        .where(Profile.user_id.in_(follower_ids))
        .values(followee_count=Profile.followee_count - 1)
    )
    await db.execute(delete(User).where(User.id == user.id))
    await events.emit_user_deleted(db, user.id)
    await db.commit()
    return avatar_url


async def get_profile(db: AsyncSession, *, username: str, viewer: User) -> ProfileResponse:
    profile = await _get_active_profile(db, username)
    following = await _following_set(db, viewer.id, [profile.user_id])
    return ProfileResponse(
        id=profile.user_id,
        username=profile.username,
        display_name=profile.display_name,
        bio=profile.bio,
        avatar_url=profile.avatar_url,
        posts_count=await events.posts_count(db, profile.user_id),
        follower_count=profile.follower_count,
        followee_count=profile.followee_count,
        is_following=profile.user_id in following,
        is_me=profile.user_id == viewer.id,
    )


async def follow(db: AsyncSession, *, viewer: User, username: str) -> FollowResponse:
    target = await _get_active_profile(db, username)
    if target.user_id == viewer.id:
        raise CannotFollowYourself()

    exists = await db.get(Follow, (viewer.id, target.user_id))
    if exists is None:
        db.add(Follow(follower_id=viewer.id, followee_id=target.user_id, created_at=_now()))
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
        else:
            await db.execute(
                update(Profile)
                .where(Profile.user_id == target.user_id)
                .values(follower_count=Profile.follower_count + 1)
            )
            await db.execute(
                update(Profile)
                .where(Profile.user_id == viewer.id)
                .values(followee_count=Profile.followee_count + 1)
            )
            await events.emit_follow(db, viewer.id, target.user_id)
            await db.commit()

    await db.refresh(target)
    return FollowResponse(is_following=True, follower_count=target.follower_count)


async def unfollow(db: AsyncSession, *, viewer: User, username: str) -> FollowResponse:
    target = await _get_active_profile(db, username)
    result = await db.execute(
        delete(Follow).where(Follow.follower_id == viewer.id, Follow.followee_id == target.user_id)
    )
    if result.rowcount:
        await db.execute(
            update(Profile)
            .where(Profile.user_id == target.user_id)
            .values(follower_count=Profile.follower_count - 1)
        )
        await db.execute(
            update(Profile)
            .where(Profile.user_id == viewer.id)
            .values(followee_count=Profile.followee_count - 1)
        )
        await events.emit_follow(db, viewer.id, target.user_id)
    await db.commit()
    await db.refresh(target)
    return FollowResponse(is_following=False, follower_count=target.follower_count)


async def _follow_list(
    db: AsyncSession,
    *,
    username: str,
    viewer: User,
    cursor: str | None,
    limit: int,
    direction: str,
) -> ProfilePage:
    target = await _get_active_profile(db, username)
    if direction == "followers":
        anchor_col, other_col = Follow.followee_id, Follow.follower_id
    else:
        anchor_col, other_col = Follow.follower_id, Follow.followee_id

    query = (
        select(Profile, Follow.created_at)
        .join(Follow, other_col == Profile.user_id)
        .join(User, User.id == Profile.user_id)
        .where(anchor_col == target.user_id, User.status == UserStatus.ACTIVE.value)
    )
    cur = decode_cursor(cursor)
    if cur is not None:
        t = datetime.fromisoformat(cur["t"])
        query = query.where(
            or_(Follow.created_at < t, and_(Follow.created_at == t, other_col < cur["id"]))
        )
    query = query.order_by(Follow.created_at.desc(), other_col.desc()).limit(limit + 1)
    rows = (await db.execute(query)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    following = await _following_set(db, viewer.id, [p.user_id for p, _ in rows])
    next_cursor = None
    if has_more:
        last_profile, last_t = rows[-1]
        next_cursor = encode_cursor({"t": last_t.isoformat(), "id": last_profile.user_id})
    return ProfilePage(items=[_short(p, following) for p, _ in rows], next_cursor=next_cursor)


async def list_followers(db, *, username, viewer, cursor, limit) -> ProfilePage:
    return await _follow_list(
        db, username=username, viewer=viewer, cursor=cursor, limit=limit, direction="followers"
    )


async def list_following(db, *, username, viewer, cursor, limit) -> ProfilePage:
    return await _follow_list(
        db, username=username, viewer=viewer, cursor=cursor, limit=limit, direction="following"
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_users(
    db: AsyncSession, *, q: str, viewer: User, cursor: str | None, limit: int
) -> ProfilePage:
    raw = q.strip().lower()
    needle = _escape_like(raw)
    prefix_match = func.lower(Profile.username).like(f"{needle}%", escape="\\")
    query = _active_profiles().where(
        or_(prefix_match, func.lower(Profile.display_name).like(f"%{needle}%", escape="\\"))
    )
    cur = decode_cursor(cursor)
    offset = int(cur["o"]) if cur else 0
    exact_match = func.lower(Profile.username) == raw
    query = (
        query.order_by(exact_match.desc(), prefix_match.desc(), func.lower(Profile.username))
        .offset(offset)
        .limit(limit + 1)
    )
    profiles = list(await db.scalars(query))

    has_more = len(profiles) > limit
    profiles = profiles[:limit]
    following = await _following_set(db, viewer.id, [p.user_id for p in profiles])
    return ProfilePage(
        items=[_short(p, following) for p in profiles],
        next_cursor=encode_cursor({"o": offset + limit}) if has_more else None,
    )


async def set_blocked(db: AsyncSession, *, username: str, blocked: bool) -> None:
    user = await db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if user is None or user.status in (UserStatus.PENDING.value, UserStatus.DELETED.value):
        raise ProfileNotFound()
    if user.role == UserRole.ADMIN.value:
        raise CannotBlockAdmin()

    if blocked:
        user.status = UserStatus.BLOCKED.value
        user.token_version += 1
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
    else:
        user.status = UserStatus.ACTIVE.value
    await events.emit_user(db, user.id)
    await db.commit()
