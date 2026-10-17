from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from content_service.auth import Auth
from content_service.comments import schemas, service
from content_service.database import get_db

router = APIRouter(tags=["comments"])

Db = Annotated[AsyncSession, Depends(get_db)]
Cursor = Annotated[str | None, Query(description="next_cursor из предыдущего ответа")]


@router.get("/posts/{post_id}/comments", response_model=schemas.CommentPage)
async def list_comments(
    post_id: int,
    db: Db,
    viewer: Auth,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> schemas.CommentPage:
    return await service.list_top_level(
        db, post_id=post_id, viewer=viewer, cursor=cursor, limit=limit
    )


@router.get("/comments/{comment_id}/replies", response_model=schemas.CommentPage)
async def list_replies(
    comment_id: int,
    db: Db,
    viewer: Auth,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> schemas.CommentPage:
    return await service.list_replies(
        db, comment_id=comment_id, viewer=viewer, cursor=cursor, limit=limit
    )


@router.post(
    "/posts/{post_id}/comments",
    response_model=schemas.CommentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_comment(
    post_id: int, body: schemas.CreateCommentRequest, db: Db, viewer: Auth
) -> schemas.CommentOut:
    return await service.create(
        db, post_id=post_id, viewer=viewer, text=body.text, parent_id=body.parent_comment_id
    )


@router.delete("/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(comment_id: int, db: Db, viewer: Auth) -> None:
    await service.remove(db, comment_id=comment_id, viewer=viewer)


@router.post("/comments/{comment_id}/like", response_model=schemas.CommentLikeState)
async def like_comment(comment_id: int, db: Db, viewer: Auth) -> schemas.CommentLikeState:
    count = await service.set_like(db, comment_id=comment_id, viewer=viewer, on=True)
    return schemas.CommentLikeState(is_liked=True, likes_count=count)


@router.delete("/comments/{comment_id}/like", response_model=schemas.CommentLikeState)
async def unlike_comment(comment_id: int, db: Db, viewer: Auth) -> schemas.CommentLikeState:
    count = await service.set_like(db, comment_id=comment_id, viewer=viewer, on=False)
    return schemas.CommentLikeState(is_liked=False, likes_count=count)
