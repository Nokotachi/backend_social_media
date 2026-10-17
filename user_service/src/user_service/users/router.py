from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from user_service.auth.dependencies import get_current_admin, get_current_user
from user_service.auth.models import User
from user_service.core import storage
from user_service.database import get_db
from user_service.settings import Settings, get_settings
from user_service.users import events, schemas, service
from user_service.users.exceptions import (
    CannotBlockAdmin,
    CannotFollowYourself,
    ProfileNotFound,
    UsersError,
    WrongPassword,
)
from user_service.users.models import Profile

router = APIRouter(tags=["users"])

_ERROR_STATUS_MAP: dict[type[UsersError], int] = {
    ProfileNotFound: status.HTTP_404_NOT_FOUND,
    CannotFollowYourself: status.HTTP_400_BAD_REQUEST,
    WrongPassword: status.HTTP_403_FORBIDDEN,
    CannotBlockAdmin: status.HTTP_400_BAD_REQUEST,
}


def _raise_http(exc: UsersError) -> None:
    http_status = _ERROR_STATUS_MAP.get(type(exc), status.HTTP_400_BAD_REQUEST)
    raise HTTPException(status_code=http_status, detail=exc.__class__.__name__) from exc


CurrentUser = Annotated[User, Depends(get_current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]
Storage = Annotated[storage.Storage, Depends(storage.get_storage)]
Cursor = Annotated[str | None, Query(description="next_cursor из предыдущего ответа")]
Limit = Annotated[int, Query(ge=1, le=50)]


@router.get("/users/me", response_model=schemas.MeResponse)
async def get_me(db: Db, current_user: CurrentUser) -> schemas.MeResponse:
    return await service.get_me(db, user=current_user)


@router.patch("/users/me", response_model=schemas.MeResponse)
async def update_me(
    body: schemas.UpdateProfileRequest, db: Db, current_user: CurrentUser
) -> schemas.MeResponse:
    return await service.update_me(
        db, user=current_user, changes=body.model_dump(exclude_unset=True)
    )


@router.post("/users/me/avatar", response_model=schemas.MeResponse)
async def upload_avatar(
    db: Db,
    current_user: CurrentUser,
    files: Storage,
    image: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
) -> schemas.MeResponse:
    data, ext = await storage.read_image(image, max_bytes=settings.avatar_max_bytes)
    data = storage.crop_to_square(data, ext)
    url = await files.save(key=storage.new_key(str(current_user.id), ext), data=data, ext=ext)
    profile = await db.get(Profile, current_user.id)
    old_url = profile.avatar_url
    profile.avatar_url = url
    await events.emit_user(db, current_user.id)
    await db.commit()
    if old_url:
        await files.delete_by_url(old_url)
    return await service.get_me(db, user=current_user)


@router.delete("/users/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    body: schemas.DeleteAccountRequest,
    db: Db,
    current_user: CurrentUser,
    files: Storage,
) -> None:
    try:
        avatar_url = await service.delete_me(db, user=current_user, password=body.password)
    except UsersError as exc:
        _raise_http(exc)
    if avatar_url:
        await files.delete_by_url(avatar_url)


@router.get("/search/users", response_model=schemas.ProfilePage)
async def search_users(
    db: Db,
    current_user: CurrentUser,
    q: Annotated[str, Query(min_length=2, max_length=50)],
    cursor: Cursor = None,
    limit: Limit = 20,
) -> schemas.ProfilePage:
    return await service.search_users(db, q=q, viewer=current_user, cursor=cursor, limit=limit)


@router.get("/users/{username}", response_model=schemas.ProfileResponse)
async def get_profile(username: str, db: Db, current_user: CurrentUser) -> schemas.ProfileResponse:
    try:
        return await service.get_profile(db, username=username, viewer=current_user)
    except UsersError as exc:
        _raise_http(exc)


@router.post("/users/{username}/follow", response_model=schemas.FollowResponse)
async def follow(username: str, db: Db, current_user: CurrentUser) -> schemas.FollowResponse:
    try:
        return await service.follow(db, viewer=current_user, username=username)
    except UsersError as exc:
        _raise_http(exc)


@router.delete("/users/{username}/follow", response_model=schemas.FollowResponse)
async def unfollow(username: str, db: Db, current_user: CurrentUser) -> schemas.FollowResponse:
    try:
        return await service.unfollow(db, viewer=current_user, username=username)
    except UsersError as exc:
        _raise_http(exc)


@router.get("/users/{username}/followers", response_model=schemas.ProfilePage)
async def followers(
    username: str, db: Db, current_user: CurrentUser, cursor: Cursor = None, limit: Limit = 20
) -> schemas.ProfilePage:
    try:
        return await service.list_followers(
            db, username=username, viewer=current_user, cursor=cursor, limit=limit
        )
    except UsersError as exc:
        _raise_http(exc)


@router.get("/users/{username}/following", response_model=schemas.ProfilePage)
async def following(
    username: str, db: Db, current_user: CurrentUser, cursor: Cursor = None, limit: Limit = 20
) -> schemas.ProfilePage:
    try:
        return await service.list_following(
            db, username=username, viewer=current_user, cursor=cursor, limit=limit
        )
    except UsersError as exc:
        _raise_http(exc)


@router.post(
    "/admin/users/{username}/block", status_code=status.HTTP_204_NO_CONTENT, tags=["admin"]
)
async def block_user(
    username: str, db: Db, _admin: Annotated[User, Depends(get_current_admin)]
) -> None:
    try:
        await service.set_blocked(db, username=username, blocked=True)
    except UsersError as exc:
        _raise_http(exc)


@router.post(
    "/admin/users/{username}/unblock", status_code=status.HTTP_204_NO_CONTENT, tags=["admin"]
)
async def unblock_user(
    username: str, db: Db, _admin: Annotated[User, Depends(get_current_admin)]
) -> None:
    try:
        await service.set_blocked(db, username=username, blocked=False)
    except UsersError as exc:
        _raise_http(exc)
