from datetime import datetime

from pydantic import BaseModel, Field


class ProfileShort(BaseModel):
    id: int
    username: str
    display_name: str
    avatar_url: str
    is_following: bool = False


class ProfileResponse(BaseModel):
    id: int
    username: str
    display_name: str
    bio: str
    avatar_url: str
    posts_count: int
    follower_count: int
    followee_count: int
    is_following: bool
    is_me: bool


class MeResponse(BaseModel):
    id: int
    email: str
    username: str
    role: str
    status: str
    created_at: datetime
    display_name: str
    bio: str
    avatar_url: str
    posts_count: int
    follower_count: int
    followee_count: int


class UpdateProfileRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    bio: str | None = Field(default=None, max_length=150)
    avatar_url: str | None = Field(default=None, max_length=255)


class FollowResponse(BaseModel):
    is_following: bool
    follower_count: int


class ProfilePage(BaseModel):
    items: list[ProfileShort]
    next_cursor: str | None


class DeleteAccountRequest(BaseModel):
    password: str
