import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import aio_pika
from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, Session, mapped_column

from content_service.database import Base

EXCHANGE = "social.events"
DEAD_LETTER_EXCHANGE = "social.events.dead"
SOURCE = "content_service"
OUTBOX_RETENTION = timedelta(days=7)

logger = logging.getLogger("content_service.bus")
_wakeup = asyncio.Event()

Handler = Callable[[AsyncSession, int, dict[str, Any]], Awaitable[None]]


class OutboxEvent(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def add_event(db: AsyncSession, routing_key: str, payload: dict[str, Any]) -> None:
    db.add(OutboxEvent(routing_key=routing_key, payload=payload, created_at=datetime.now(UTC)))
    db.info["outbox_dirty"] = True


@event.listens_for(Session, "after_commit")
def _kick_relay(session: Session) -> None:
    if session.info.pop("outbox_dirty", False):
        _wakeup.set()


async def _declare(channel: aio_pika.abc.AbstractChannel) -> aio_pika.abc.AbstractExchange:
    return await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)


async def _publish_pending(
    session_factory: async_sessionmaker, exchange: aio_pika.abc.AbstractExchange
) -> int:
    async with session_factory() as db:
        rows = list(
            await db.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            body = {"event_id": row.id, "type": row.routing_key, "data": row.payload}
            await exchange.publish(
                aio_pika.Message(
                    json.dumps(body).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=f"{SOURCE}:{row.id}",
                ),
                routing_key=row.routing_key,
            )
            row.published_at = datetime.now(UTC)
        await db.execute(
            delete(OutboxEvent).where(
                OutboxEvent.published_at < datetime.now(UTC) - OUTBOX_RETENTION
            )
        )
        await db.commit()
        return len(rows)


async def run_relay(url: str, session_factory: async_sessionmaker) -> None:
    while True:
        try:
            connection = await aio_pika.connect_robust(url)
            async with connection:
                channel = await connection.channel(publisher_confirms=True)
                exchange = await _declare(channel)
                logger.info("Публикация событий в RabbitMQ запущена")
                while True:
                    if await _publish_pending(session_factory, exchange) == 100:
                        continue
                    _wakeup.clear()
                    try:
                        await asyncio.wait_for(_wakeup.wait(), timeout=1)
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Публикация событий прервана, повтор через 3 с")
            await asyncio.sleep(3)


async def consume(
    url: str,
    queue_name: str,
    routing_keys: list[str],
    handlers: dict[str, Handler],
    session_factory: async_sessionmaker,
) -> None:
    while True:
        try:
            connection = await aio_pika.connect_robust(url)
            async with connection:
                channel = await connection.channel()
                await channel.set_qos(prefetch_count=20)
                exchange = await _declare(channel)
                dead_exchange = await channel.declare_exchange(
                    DEAD_LETTER_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
                )
                dead_queue = await channel.declare_queue(f"{queue_name}.dead", durable=True)
                await dead_queue.bind(dead_exchange, routing_key=queue_name)
                queue = await channel.declare_queue(
                    queue_name,
                    durable=True,
                    arguments={
                        "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
                        "x-dead-letter-routing-key": queue_name,
                    },
                )
                for key in routing_keys:
                    await queue.bind(exchange, routing_key=key)
                logger.info("Чтение очереди %s запущено", queue_name)
                async with queue.iterator() as messages:
                    async for message in messages:
                        await _handle(message, handlers, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Чтение очереди %s прервано, повтор через 3 с", queue_name)
            await asyncio.sleep(3)


async def _handle(
    message: aio_pika.abc.AbstractIncomingMessage,
    handlers: dict[str, Handler],
    session_factory: async_sessionmaker,
) -> None:
    try:
        body = json.loads(message.body)
        handler = handlers.get(body["type"])
        if handler is not None:
            async with session_factory() as db:
                await handler(db, body["event_id"], body["data"])
                await db.commit()
    except Exception:
        logger.exception("Событие %s не обработано", message.message_id)
        await message.nack(requeue=not message.redelivered)
    else:
        await message.ack()


async def dispatch(
    db: AsyncSession, handlers: dict[str, Handler], event_type: str, event_id: int, data: dict
) -> None:
    await handlers[event_type](db, event_id, data)
