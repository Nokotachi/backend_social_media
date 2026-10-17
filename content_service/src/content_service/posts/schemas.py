from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class AuthorShort(BaseModel):
    id: int
    username: str
    display_name: str
    avatar_url: str


class PostImageOut(BaseModel):
    id: int
    url: str
    position: int


class PostOut(BaseModel):
    id: int
    author: AuthorShort
    caption: str
    aspect_ratio: str
    status: str
    created_at: datetime
    images: list[PostImageOut]
    likes_count: int
    comments_count: int
    reposts_count: int
    is_liked: bool
    is_favorited: bool
    is_reposted: bool
    is_mine: bool


class PostPage(BaseModel):
    items: list[PostOut]
    next_cursor: str | None


class BatchRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=50)


class BatchResponse(BaseModel):
    items: list[PostOut]


class LikeState(BaseModel):
    is_liked: bool
    likes_count: int


class FavoriteState(BaseModel):
    is_favorited: bool


class RepostState(BaseModel):
    is_reposted: bool
    reposts_count: int


class ReportReason(str, Enum):
    spam = "spam"
    nudity = "nudity"
    violence = "violence"
    harassment = "harassment"
    hate = "hate"
    false_information = "false_information"
    other = "other"


class ReportRequest(BaseModel):
    reason: ReportReason
    description: str = Field(default="", max_length=1000)
