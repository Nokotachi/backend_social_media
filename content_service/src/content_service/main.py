import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from content_service import users
from content_service.admin.router import router as admin_router
from content_service.auth import public_key
from content_service.comments.router import router as comments_router
from content_service.core import bus
from content_service.core.storage import init_storage
from content_service.database import AsyncSessionLocal, Base, engine
from content_service.posts.router import router as posts_router
from content_service.settings import get_settings

settings = get_settings()

logging.basicConfig(level=logging.INFO if not settings.debug else logging.DEBUG)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    public_key()
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                bus.OutboxEvent.__table__,
                users.UserReplica.__table__,
                users.FollowReplica.__table__,
            ],
        )
    await init_storage(settings)
    tasks = []
    if settings.events_enabled:
        async with AsyncSessionLocal() as db:
            await users.enqueue_snapshot(db)
        tasks = [
            asyncio.create_task(bus.run_relay(settings.rabbitmq_url, AsyncSessionLocal)),
            asyncio.create_task(
                bus.consume(
                    settings.rabbitmq_url,
                    users.QUEUE,
                    list(users.HANDLERS),
                    users.HANDLERS,
                    AsyncSessionLocal,
                )
            ),
        ]
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    lifespan=lifespan,
    title="Content Service",
    description="Посты, ленты, лайки, комментарии, жалобы — social media MVP",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(posts_router)
app.include_router(comments_router)
app.include_router(admin_router)

if settings.storage_backend == "local":
    Path(settings.media_root).mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.media_root), name="media")


@app.get("/health", tags=["service"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
