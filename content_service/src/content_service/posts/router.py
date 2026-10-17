from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from content_service.auth import Auth
from content_service.core import errors
from content_service.core.storage import Storage, get_storage
from content_service.database import get_db
from content_service.models import HiddenPost, PostFavorite, PostLike, Repost
from content_service.posts import schemas, service
from content_service.settings import Settings, get_settings

router = APIRouter(tags=["posts"])

Db = Annotated[AsyncSession, Depends(get_db)]
Cfg = Annotated[Settings, Depends(get_settings)]
Files = Annotated[Storage, Depends(get_storage)]
Cursor = Annotated[str | None, Query(description="next_cursor из предыдущего ответа")]


@router.post("/posts", response_model=schemas.PostOut, status_code=status.HTTP_201_CREATED)
async def create_post(
    db: Db,
    viewer: Auth,
    settings: Cfg,
    files: Files,
    background: BackgroundTasks,
    images: Annotated[
        list[UploadFile], File(description="1–10 файлов JPEG/PNG/WEBP, по порядку карусели")
    ],
    aspect_ratio: Annotated[str, Form(description="одно из ALLOWED_ASPECT_RATIOS, напр. 1:1")],
    caption: Annotated[str, Form(max_length=2200)] = "",
) -> schemas.PostOut:
    post = await service.create_post(
        db,
        viewer=viewer,
        images=images,
        aspect_ratio=aspect_ratio,
        caption=caption,
        settings=settings,
        files=files,
    )
    if settings.recommendation_service_url:
        background.add_task(service.request_embedding_and_publish, post.id, settings)
    return (await service.hydrate(db, [post.id], viewer))[0]


@router.post("/posts/batch", response_model=schemas.BatchResponse)
async def posts_batch(body: schemas.BatchRequest, db: Db, viewer: Auth) -> schemas.BatchResponse:
    ids = list(dict.fromkeys(body.ids))
    return schemas.BatchResponse(items=await service.hydrate(db, ids, viewer))


@router.get("/posts/popular", response_model=schemas.PostPage)
async def popular_posts(
    db: Db,
    viewer: Auth,
    days: Annotated[int, Query(ge=1, le=365)] = 7,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> schemas.PostPage:
    return await service.popular(db, viewer=viewer, days=days, cursor=cursor, limit=limit)


@router.get("/posts", response_model=schemas.PostPage)
async def author_posts(
    db: Db,
    viewer: Auth,
    author: Annotated[str, Query(description="username автора")],
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 12,
) -> schemas.PostPage:
    return await service.author_posts(
        db, username=author, viewer=viewer, cursor=cursor, limit=limit
    )


@router.get("/posts/{post_id}", response_model=schemas.PostOut)
async def get_post(post_id: int, db: Db, viewer: Auth) -> schemas.PostOut:
    items = await service.hydrate(db, [post_id], viewer)
    if not items:
        raise errors.not_found()
    return items[0]


@router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(post_id: int, db: Db, viewer: Auth, files: Files) -> None:
    await service.delete_post(db, post_id=post_id, viewer=viewer, files=files)


@router.get("/feed/following", response_model=schemas.PostPage, tags=["feed"])
async def following_feed(
    db: Db, viewer: Auth, cursor: Cursor = None, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> schemas.PostPage:
    return await service.following_feed(db, viewer=viewer, cursor=cursor, limit=limit)


@router.get("/search/posts", response_model=schemas.PostPage, tags=["feed"])
async def search_posts(
    db: Db,
    viewer: Auth,
    q: Annotated[str | None, Query(max_length=100)] = None,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 18,
) -> schemas.PostPage:
    return await service.search_posts(db, viewer=viewer, q=q, cursor=cursor, limit=limit)


@router.get("/favorites", response_model=schemas.PostPage, tags=["feed"])
async def favorites(
    db: Db, viewer: Auth, cursor: Cursor = None, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> schemas.PostPage:
    return await service.favorites(db, viewer=viewer, cursor=cursor, limit=limit)


@router.post("/posts/{post_id}/like", response_model=schemas.LikeState)
async def like(post_id: int, db: Db, viewer: Auth) -> schemas.LikeState:
    await service.set_link(db, PostLike, post_id=post_id, viewer=viewer, on=True)
    return schemas.LikeState(
        is_liked=True, likes_count=await service.count_for(db, PostLike, post_id)
    )


@router.delete("/posts/{post_id}/like", response_model=schemas.LikeState)
async def unlike(post_id: int, db: Db, viewer: Auth) -> schemas.LikeState:
    await service.set_link(db, PostLike, post_id=post_id, viewer=viewer, on=False)
    return schemas.LikeState(
        is_liked=False, likes_count=await service.count_for(db, PostLike, post_id)
    )


@router.post("/posts/{post_id}/favorite", response_model=schemas.FavoriteState)
async def favorite(post_id: int, db: Db, viewer: Auth) -> schemas.FavoriteState:
    await service.set_link(db, PostFavorite, post_id=post_id, viewer=viewer, on=True)
    return schemas.FavoriteState(is_favorited=True)


@router.delete("/posts/{post_id}/favorite", response_model=schemas.FavoriteState)
async def unfavorite(post_id: int, db: Db, viewer: Auth) -> schemas.FavoriteState:
    await service.set_link(db, PostFavorite, post_id=post_id, viewer=viewer, on=False)
    return schemas.FavoriteState(is_favorited=False)


@router.post("/posts/{post_id}/repost", response_model=schemas.RepostState)
async def repost(post_id: int, db: Db, viewer: Auth) -> schemas.RepostState:
    await service.set_link(db, Repost, post_id=post_id, viewer=viewer, on=True)
    return schemas.RepostState(
        is_reposted=True, reposts_count=await service.count_for(db, Repost, post_id)
    )


@router.delete("/posts/{post_id}/repost", response_model=schemas.RepostState)
async def unrepost(post_id: int, db: Db, viewer: Auth) -> schemas.RepostState:
    await service.set_link(db, Repost, post_id=post_id, viewer=viewer, on=False)
    return schemas.RepostState(
        is_reposted=False, reposts_count=await service.count_for(db, Repost, post_id)
    )


@router.post("/posts/{post_id}/hide", status_code=status.HTTP_204_NO_CONTENT)
async def hide(post_id: int, db: Db, viewer: Auth) -> None:
    await service.set_link(db, HiddenPost, post_id=post_id, viewer=viewer, on=True)


@router.delete("/posts/{post_id}/hide", status_code=status.HTTP_204_NO_CONTENT)
async def unhide(post_id: int, db: Db, viewer: Auth) -> None:
    await service.set_link(db, HiddenPost, post_id=post_id, viewer=viewer, on=False)


@router.post("/posts/{post_id}/report", status_code=status.HTTP_201_CREATED)
async def report(post_id: int, body: schemas.ReportRequest, db: Db, viewer: Auth) -> dict[str, str]:
    await service.report(
        db, post_id=post_id, viewer=viewer, reason=body.reason, description=body.description
    )
    return {"status": "received"}
