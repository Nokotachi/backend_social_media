from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from content_service.auth import CurrentUser
from content_service.comments.schemas import CommentOut, CommentPage
from content_service.core import errors
from content_service.core.pagination import decode_cursor, encode_cursor
from content_service.models import Comment, CommentLike, Post
from content_service.posts.schemas import AuthorShort
from content_service.posts.service import _author_visible, get_visible_post


def _now() -> datetime:
    return datetime.now(UTC)


async def _hydrate(
    db: AsyncSession, comments: list[Comment], viewer: CurrentUser, post_author_id: int
) -> list[CommentOut]:
    if not comments:
        return []
    author_ids = {c.author_id for c in comments}
    authors = await viewer.users.authors(author_ids)
    comments = [c for c in comments if c.author_id in authors]
    if not comments:
        return []
    ids = [c.id for c in comments]
    likes = dict(
        (
            await db.execute(
                select(CommentLike.comment_id, func.count())
                .where(CommentLike.comment_id.in_(ids))
                .group_by(CommentLike.comment_id)
            )
        ).all()
    )
    replies = dict(
        (
            await db.execute(
                select(Comment.parent_comment_id, func.count())
                .where(Comment.parent_comment_id.in_(ids))
                .group_by(Comment.parent_comment_id)
            )
        ).all()
    )
    liked = set(
        await db.scalars(
            select(CommentLike.comment_id).where(
                CommentLike.user_id == viewer.id, CommentLike.comment_id.in_(ids)
            )
        )
    )
    out = []
    for c in comments:
        p = authors[c.author_id]
        out.append(
            CommentOut(
                id=c.id,
                post_id=c.post_id,
                parent_comment_id=c.parent_comment_id,
                author=AuthorShort(
                    id=p.id,
                    username=p.username,
                    display_name=p.display_name,
                    avatar_url=p.avatar_url,
                ),
                text=c.text,
                created_at=c.created_at,
                likes_count=likes.get(c.id, 0),
                replies_count=replies.get(c.id, 0),
                is_liked=c.id in liked,
                can_delete=viewer.id in (c.author_id, post_author_id) or viewer.is_admin,
            )
        )
    return out


async def _page(
    db: AsyncSession,
    query,
    cursor: str | None,
    limit: int,
    viewer: CurrentUser,
    post_author_id: int,
) -> CommentPage:
    query = query.where(_author_visible(viewer, Comment.author_id))
    cur = decode_cursor(cursor)
    if cur is not None:
        t = datetime.fromisoformat(cur["t"])
        query = query.where(
            or_(Comment.created_at > t, and_(Comment.created_at == t, Comment.id > cur["id"]))
        )
    rows = list(await db.scalars(query.order_by(Comment.created_at, Comment.id).limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor({"t": rows[-1].created_at.isoformat(), "id": rows[-1].id})
        if has_more
        else None
    )
    return CommentPage(
        items=await _hydrate(db, rows, viewer, post_author_id), next_cursor=next_cursor
    )


async def list_top_level(db, *, post_id: int, viewer: CurrentUser, cursor, limit) -> CommentPage:
    post = await get_visible_post(db, post_id, viewer)
    query = select(Comment).where(Comment.post_id == post_id, Comment.parent_comment_id.is_(None))
    return await _page(db, query, cursor, limit, viewer, post.author_id)


async def _get_visible_comment(db: AsyncSession, comment_id: int, viewer: CurrentUser):
    comment = await db.get(Comment, comment_id)
    if comment is None:
        raise errors.not_found("CommentNotFound")
    post = await get_visible_post(db, comment.post_id, viewer)
    return comment, post


async def list_replies(db, *, comment_id: int, viewer: CurrentUser, cursor, limit) -> CommentPage:
    comment, post = await _get_visible_comment(db, comment_id, viewer)
    query = select(Comment).where(Comment.parent_comment_id == comment.id)
    return await _page(db, query, cursor, limit, viewer, post.author_id)


async def create(
    db: AsyncSession, *, post_id: int, viewer: CurrentUser, text: str, parent_id: int | None
) -> CommentOut:
    post = await get_visible_post(db, post_id, viewer)
    text = text.strip()
    if not text:
        raise errors.bad_request("EmptyComment")
    if parent_id is not None:
        parent = await db.get(Comment, parent_id)
        if parent is None:
            raise errors.not_found("CommentNotFound")
        if parent.post_id != post_id:
            raise errors.bad_request("InvalidParentComment")
        parent_id = parent.parent_comment_id or parent.id
    comment = Comment(
        post_id=post_id,
        author_id=viewer.id,
        parent_comment_id=parent_id,
        text=text,
        created_at=_now(),
    )
    db.add(comment)
    await db.commit()
    return (await _hydrate(db, [comment], viewer, post.author_id))[0]


async def remove(db: AsyncSession, *, comment_id: int, viewer: CurrentUser) -> None:
    comment = await db.get(Comment, comment_id)
    if comment is None:
        raise errors.not_found("CommentNotFound")
    post_author_id = await db.scalar(select(Post.author_id).where(Post.id == comment.post_id))
    if viewer.id not in (comment.author_id, post_author_id) and not viewer.is_admin:
        raise errors.forbidden("CannotDeleteComment")
    await db.execute(delete(Comment).where(Comment.id == comment_id))
    await db.commit()


async def set_like(db: AsyncSession, *, comment_id: int, viewer: CurrentUser, on: bool) -> int:
    await _get_visible_comment(db, comment_id, viewer)
    existing = await db.get(CommentLike, (viewer.id, comment_id))
    if on and existing is None:
        db.add(CommentLike(user_id=viewer.id, comment_id=comment_id, created_at=_now()))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
    elif not on and existing is not None:
        await db.delete(existing)
        await db.commit()
    return await db.scalar(
        select(func.count()).select_from(CommentLike).where(CommentLike.comment_id == comment_id)
    )
