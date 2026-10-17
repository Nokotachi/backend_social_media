import logging
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, delete, exists, func, or_, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from content_service.auth import CurrentUser
from content_service.core import errors, storage
from content_service.core.pagination import decode_cursor, encode_cursor
from content_service.core.storage import Storage
from content_service.database import AsyncSessionLocal
from content_service.models import (
    Comment,
    HiddenPost,
    Post,
    PostFavorite,
    PostImage,
    PostLike,
    PostReport,
    PostStatus,
    Repost,
)
from content_service.posts.schemas import (
    AuthorShort,
    PostImageOut,
    PostOut,
    PostPage,
    ReportReason,
)
from content_service.settings import Settings
from content_service.users import emit_posts_count

logger = logging.getLogger("content_service.posts")


def _now() -> datetime:
    return datetime.now(UTC)


def _image_error(status_code: int, code: str, index: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "image_index": index})


def _author_visible(viewer: CurrentUser, author_col=Post.author_id):
    if not viewer.hidden_author_ids:
        return true()
    return author_col.not_in(viewer.hidden_author_ids)


def _visible_to(viewer: CurrentUser):
    return and_(
        _author_visible(viewer),
        or_(Post.status == PostStatus.PUBLISHED, Post.author_id == viewer.id),
    )


def _not_hidden_by(viewer: CurrentUser):
    return ~exists().where(HiddenPost.user_id == viewer.id, HiddenPost.post_id == Post.id)


def _base_ids_query(viewer: CurrentUser):
    return select(Post.id, Post.created_at).where(_visible_to(viewer))


async def _counts(db: AsyncSession, model, key_col, ids: list[int]) -> dict[int, int]:
    rows = await db.execute(select(key_col, func.count()).where(key_col.in_(ids)).group_by(key_col))
    return {k: c for k, c in rows.all()}


async def _viewer_set(db: AsyncSession, model, viewer_id: int, ids: list[int]) -> set[int]:
    rows = await db.scalars(
        select(model.post_id).where(model.user_id == viewer_id, model.post_id.in_(ids))
    )
    return set(rows)


async def hydrate(db: AsyncSession, ids: list[int], viewer: CurrentUser) -> list[PostOut]:
    if not ids:
        return []
    posts = list(await db.scalars(select(Post).where(Post.id.in_(ids), _visible_to(viewer))))
    authors = await viewer.users.authors({p.author_id for p in posts}) if posts else {}
    posts = [p for p in posts if p.author_id in authors]
    found_ids = [p.id for p in posts]
    if not found_ids:
        return []

    images: dict[int, list[PostImageOut]] = {}
    for img in await db.scalars(
        select(PostImage)
        .where(PostImage.post_id.in_(found_ids))
        .order_by(PostImage.post_id, PostImage.position)
    ):
        images.setdefault(img.post_id, []).append(
            PostImageOut(id=img.id, url=img.image_url, position=img.position)
        )

    likes = await _counts(db, PostLike, PostLike.post_id, found_ids)
    comments = await _counts(db, Comment, Comment.post_id, found_ids)
    reposts = await _counts(db, Repost, Repost.post_id, found_ids)
    liked = await _viewer_set(db, PostLike, viewer.id, found_ids)
    favorited = await _viewer_set(db, PostFavorite, viewer.id, found_ids)
    reposted = await _viewer_set(db, Repost, viewer.id, found_ids)

    by_id: dict[int, PostOut] = {}
    for post in posts:
        author = authors[post.author_id]
        by_id[post.id] = PostOut(
            id=post.id,
            author=AuthorShort(
                id=author.id,
                username=author.username,
                display_name=author.display_name,
                avatar_url=author.avatar_url,
            ),
            caption=post.caption,
            aspect_ratio=post.aspect_ratio,
            status=post.status,
            created_at=post.created_at,
            images=images.get(post.id, []),
            likes_count=likes.get(post.id, 0),
            comments_count=comments.get(post.id, 0),
            reposts_count=reposts.get(post.id, 0),
            is_liked=post.id in liked,
            is_favorited=post.id in favorited,
            is_reposted=post.id in reposted,
            is_mine=post.author_id == viewer.id,
        )
    return [by_id[i] for i in ids if i in by_id]


async def get_visible_post(db: AsyncSession, post_id: int, viewer: CurrentUser) -> Post:
    post = await db.scalar(select(Post).where(Post.id == post_id, _visible_to(viewer)))
    if post is None:
        raise errors.not_found()
    return post


async def _keyset_page(
    db: AsyncSession, query, ts_col, id_col, cursor: str | None, limit: int, viewer: CurrentUser
) -> PostPage:
    cur = decode_cursor(cursor)
    if cur is not None:
        t = datetime.fromisoformat(cur["t"])
        query = query.where(or_(ts_col < t, and_(ts_col == t, id_col < cur["id"])))
    rows = (await db.execute(query.order_by(ts_col.desc(), id_col.desc()).limit(limit + 1))).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = await hydrate(db, [r[0] for r in rows], viewer)
    next_cursor = None
    if has_more:
        last_id, last_t = rows[-1][0], rows[-1][1]
        next_cursor = encode_cursor({"t": last_t.isoformat(), "id": last_id})
    return PostPage(items=items, next_cursor=next_cursor)


async def _offset_page(
    db: AsyncSession, query, cursor: str | None, limit: int, viewer: CurrentUser
) -> PostPage:
    cur = decode_cursor(cursor)
    offset = int(cur["o"]) if cur else 0
    ids = list(await db.scalars(query.offset(offset).limit(limit + 1)))
    has_more = len(ids) > limit
    items = await hydrate(db, ids[:limit], viewer)
    return PostPage(
        items=items, next_cursor=encode_cursor({"o": offset + limit}) if has_more else None
    )


async def create_post(
    db: AsyncSession,
    *,
    viewer: CurrentUser,
    images: list[UploadFile],
    aspect_ratio: str,
    caption: str,
    settings: Settings,
    files: Storage,
) -> Post:
    if not 1 <= len(images) <= settings.max_images_per_post:
        raise errors.bad_request("InvalidImagesCount")
    if aspect_ratio not in settings.aspect_ratio_list:
        raise errors.bad_request("InvalidAspectRatio")
    if len(caption) > 2200:
        raise errors.bad_request("CaptionTooLong")

    w_ratio, h_ratio = (int(x) for x in aspect_ratio.split(":"))
    target = w_ratio / h_ratio
    checked = []
    for index, upload in enumerate(images):
        try:
            data, ext, (width, height) = await storage.read_image(
                upload, max_bytes=settings.max_image_bytes
            )
        except HTTPException as exc:
            raise _image_error(exc.status_code, exc.detail, index) from exc
        if min(width, height) < settings.min_image_short_side:
            raise _image_error(400, "ImageTooSmall", index)
        if abs(width / height - target) / target > settings.aspect_ratio_tolerance:
            raise _image_error(400, "ImageAspectRatioMismatch", index)
        checked.append((data, ext))

    now = _now()
    post = Post(
        author_id=viewer.id,
        caption=caption,
        aspect_ratio=aspect_ratio,
        status=PostStatus.PUBLISHED
        if not settings.recommendation_service_url
        else PostStatus.PROCESSING,
        created_at=now,
    )
    db.add(post)
    await db.flush()
    for position, (data, ext) in enumerate(checked):
        url = await files.save(key=storage.new_key(str(post.id), ext), data=data, ext=ext)
        db.add(PostImage(post_id=post.id, image_url=url, position=position, created_at=now))
    if post.status == PostStatus.PUBLISHED:
        await emit_posts_count(db, viewer.id)
    await db.commit()
    return post


async def request_embedding_and_publish(post_id: int, settings: Settings) -> None:
    url = f"{settings.recommendation_service_url.rstrip('/')}/embeddings/posts/{post_id}/compute"
    try:
        async with httpx.AsyncClient(timeout=settings.recommendation_timeout_seconds) as client:
            resp = await client.post(url)
            resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Эмбеддинг для поста %s не посчитан, публикуем без него", post_id)
    async with AsyncSessionLocal() as db:
        post = await db.get(Post, post_id)
        if post is None:
            return
        post.status = PostStatus.PUBLISHED
        await emit_posts_count(db, post.author_id)
        await db.commit()


async def delete_post(
    db: AsyncSession, *, post_id: int, viewer: CurrentUser, files: Storage
) -> None:
    post = await db.get(Post, post_id)
    if post is None:
        raise errors.not_found()
    if post.author_id != viewer.id and not viewer.is_admin:
        raise errors.forbidden("NotPostAuthor")
    urls = list(await db.scalars(select(PostImage.image_url).where(PostImage.post_id == post_id)))
    await db.execute(delete(Post).where(Post.id == post_id))
    await emit_posts_count(db, post.author_id)
    await db.commit()
    for url in urls:
        await files.delete_by_url(url)


async def following_feed(db, *, viewer: CurrentUser, cursor, limit) -> PostPage:
    followees = await viewer.users.following_ids(viewer.id)
    query = _base_ids_query(viewer).where(
        Post.status == PostStatus.PUBLISHED,
        Post.author_id.in_(followees),
        _not_hidden_by(viewer),
    )
    return await _keyset_page(db, query, Post.created_at, Post.id, cursor, limit, viewer)


async def author_posts(db, *, username: str, viewer: CurrentUser, cursor, limit) -> PostPage:
    author_id = await viewer.users.id_by_username(username)
    if author_id is None:
        raise errors.not_found("UserNotFound")
    query = _base_ids_query(viewer).where(Post.author_id == author_id)
    return await _keyset_page(db, query, Post.created_at, Post.id, cursor, limit, viewer)


def _popular_ids_query(viewer: CurrentUser, days: int):
    likes = (
        select(PostLike.post_id, func.count().label("likes")).group_by(PostLike.post_id).subquery()
    )
    return (
        select(Post.id)
        .outerjoin(likes, likes.c.post_id == Post.id)
        .where(
            _author_visible(viewer),
            Post.status == PostStatus.PUBLISHED,
            Post.created_at >= _now() - timedelta(days=days),
            Post.author_id != viewer.id,
            _not_hidden_by(viewer),
        )
        .order_by(func.coalesce(likes.c.likes, 0).desc(), Post.created_at.desc(), Post.id.desc())
    )


async def popular(db, *, viewer: CurrentUser, days: int, cursor, limit) -> PostPage:
    return await _offset_page(db, _popular_ids_query(viewer, days), cursor, limit, viewer)


async def search_posts(db, *, viewer: CurrentUser, q: str | None, cursor, limit) -> PostPage:
    if not q or not q.strip():
        return await popular(db, viewer=viewer, days=30, cursor=cursor, limit=limit)
    needle = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    query = (
        select(Post.id)
        .where(
            _author_visible(viewer),
            Post.status == PostStatus.PUBLISHED,
            func.lower(Post.caption).like(f"%{needle}%", escape="\\"),
            _not_hidden_by(viewer),
        )
        .order_by(Post.created_at.desc(), Post.id.desc())
    )
    return await _offset_page(db, query, cursor, limit, viewer)


async def favorites(db, *, viewer: CurrentUser, cursor, limit) -> PostPage:
    query = (
        select(PostFavorite.post_id, PostFavorite.created_at)
        .join(Post, Post.id == PostFavorite.post_id)
        .where(PostFavorite.user_id == viewer.id, _visible_to(viewer))
    )
    return await _keyset_page(
        db, query, PostFavorite.created_at, PostFavorite.post_id, cursor, limit, viewer
    )


async def set_link(db: AsyncSession, model, *, post_id: int, viewer: CurrentUser, on: bool) -> None:
    await get_visible_post(db, post_id, viewer)
    existing = await db.get(model, (viewer.id, post_id))
    if on and existing is None:
        db.add(model(user_id=viewer.id, post_id=post_id, created_at=_now()))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
    elif not on and existing is not None:
        await db.delete(existing)
        await db.commit()


async def count_for(db: AsyncSession, model, post_id: int) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(model.post_id == post_id))


async def report(
    db: AsyncSession, *, post_id: int, viewer: CurrentUser, reason: ReportReason, description: str
) -> None:
    await get_visible_post(db, post_id, viewer)
    db.add(
        PostReport(
            post_id=post_id,
            reporter_id=viewer.id,
            description=f"[{reason.value}] {description}".strip(),
            created_at=_now(),
        )
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise errors.conflict("AlreadyReported") from exc
