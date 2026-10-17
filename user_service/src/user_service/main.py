import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from user_service.auth import security
from user_service.auth.router import router as auth_router
from user_service.core import bus
from user_service.core.email import EmailDeliveryFailed
from user_service.core.storage import init_storage
from user_service.database import AsyncSessionLocal, Base, engine
from user_service.settings import get_settings
from user_service.users import events
from user_service.users.router import router as users_router

settings = get_settings()

logging.basicConfig(level=logging.INFO if not settings.debug else logging.DEBUG)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    security._private_key()
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[bus.OutboxEvent.__table__, events.UserPostCount.__table__],
        )
    await init_storage(settings)
    tasks = []
    if settings.events_enabled:
        async with AsyncSessionLocal() as db:
            await events.enqueue_snapshot(db)
        tasks = [
            asyncio.create_task(bus.run_relay(settings.rabbitmq_url, AsyncSessionLocal)),
            asyncio.create_task(
                bus.consume(
                    settings.rabbitmq_url,
                    events.QUEUE,
                    list(events.HANDLERS),
                    events.HANDLERS,
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
    title="User Service",
    description="Аутентификация, авторизация и профили — social media MVP",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)


@app.exception_handler(EmailDeliveryFailed)
async def _email_failed(_request: Request, _exc: EmailDeliveryFailed) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "EmailDeliveryFailed"})


app.include_router(auth_router)
app.include_router(users_router)

if settings.storage_backend == "local":
    Path(settings.media_root).mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.media_root), name="media")


@app.get("/health", tags=["service"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
