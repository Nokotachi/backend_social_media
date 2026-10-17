from datetime import datetime

from pydantic import BaseModel, Field

from content_service.posts.schemas import AuthorShort


class CommentOut(BaseModel):
    id: int
    post_id: int
    parent_comment_id: int | None
    author: AuthorShort
    text: str
    created_at: datetime
    likes_count: int
    replies_count: int
    is_liked: bool
    can_delete: bool


class CommentPage(BaseModel):
    items: list[CommentOut]
    next_cursor: str | None


class CreateCommentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    parent_comment_id: int | None = None


class CommentLikeState(BaseModel):
    is_liked: bool
    likes_count: int
