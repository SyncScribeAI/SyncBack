"""Доставка SSE-событий подключённым клиентам API.

RedisPubSubBroker — основной режим: каждый процесс API подписан на общий канал
Redis, поэтому события от воркера и от других процессов доходят до всех
клиентов. InMemorySSEBroker — запасной режим для одного процесса без Redis:
события воркера в него не попадают.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("syncscribe.infrastructure.sse")

SUBSCRIBER_QUEUE_SIZE = 256


@dataclass
class SSEEvent:
    """Одно SSE-событие. user_id ограничивает доставку одним пользователем,
    document_id позволяет клиенту фильтровать поток по документам."""

    event: str
    data: dict[str, Any] = field(default_factory=dict)
    document_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None

    def to_sse_bytes(self) -> bytes:
        payload = {"event": self.event, **self.data}
        return f"event: {self.event}\ndata: {json.dumps(payload)}\n\n".encode()

    def to_json(self) -> str:
        return json.dumps(
            {
                "event": self.event,
                "data": self.data,
                "document_id": str(self.document_id) if self.document_id else None,
                "user_id": str(self.user_id) if self.user_id else None,
            }
        )

    @staticmethod
    def from_json(raw: str) -> SSEEvent:
        d = json.loads(raw)
        return SSEEvent(
            event=d["event"],
            data=d.get("data", {}),
            document_id=uuid.UUID(d["document_id"]) if d.get("document_id") else None,
            user_id=uuid.UUID(d["user_id"]) if d.get("user_id") else None,
        )


SubscriberQueue = asyncio.Queue["SSEEvent | None"]


class _Subscribers:
    """Очереди подключённых клиентов и раздача событий по их фильтрам."""

    def __init__(self) -> None:
        self._queues: dict[str, SubscriberQueue] = {}
        self._filters: dict[str, tuple[uuid.UUID, frozenset[uuid.UUID]]] = {}

    def add(
        self, user_id: uuid.UUID, document_ids: frozenset[uuid.UUID]
    ) -> tuple[str, SubscriberQueue]:
        sub_id = str(uuid.uuid4())
        queue: SubscriberQueue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._queues[sub_id] = queue
        self._filters[sub_id] = (user_id, document_ids)
        return sub_id, queue

    def remove(self, sub_id: str) -> None:
        self._queues.pop(sub_id, None)
        self._filters.pop(sub_id, None)

    def fan_out(self, event: SSEEvent) -> None:
        for sub_id, queue in list(self._queues.items()):
            user_id, document_ids = self._filters[sub_id]
            if event.user_id is not None and event.user_id != user_id:
                continue
            if (
                document_ids
                and event.document_id is not None
                and event.document_id not in document_ids
            ):
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "Очередь SSE-подписчика %s переполнена, событие пропущено", sub_id[:8]
                )

    async def close_all(self) -> None:
        """Положить маркер конца в каждую очередь, не дожидаясь потребителя.

        Медленный клиент может держать очередь заполненной; ожидание свободного места
        остановило бы shutdown. Ради маркера отбрасывается самое старое событие: при
        закрытии потока клиент всё равно перечитает состояние.
        """
        for queue in self._queues.values():
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)
        self._queues.clear()
        self._filters.clear()


class SSEBroker(ABC):
    @abstractmethod
    async def publish(self, event: SSEEvent) -> None: ...

    @abstractmethod
    async def subscribe(
        self, user_id: uuid.UUID, document_ids: frozenset[uuid.UUID]
    ) -> tuple[str, SubscriberQueue]: ...

    @abstractmethod
    def unsubscribe(self, sub_id: str) -> None: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...


class InMemorySSEBroker(SSEBroker):
    def __init__(self) -> None:
        self._subscribers = _Subscribers()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        await self._subscribers.close_all()

    async def subscribe(
        self, user_id: uuid.UUID, document_ids: frozenset[uuid.UUID]
    ) -> tuple[str, SubscriberQueue]:
        return self._subscribers.add(user_id, document_ids)

    def unsubscribe(self, sub_id: str) -> None:
        self._subscribers.remove(sub_id)

    async def publish(self, event: SSEEvent) -> None:
        self._subscribers.fan_out(event)


class RedisPubSubBroker(SSEBroker):
    def __init__(self, redis_url: str, channel: str) -> None:
        self._redis_url = redis_url
        self._channel = channel
        self._subscribers = _Subscribers()
        self._reader_task: asyncio.Task[None] | None = None
        self._redis: Any = None
        self._pubsub: Any = None

    async def start(self) -> None:
        import redis.asyncio as aioredis  # noqa: PLC0415 — redis нужен только в этом режиме

        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self._channel)
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.unsubscribe(self._channel)
            await self._pubsub.aclose()
        if self._redis:
            await self._redis.aclose()
        await self._subscribers.close_all()

    async def _reader_loop(self) -> None:
        try:
            async for message in self._pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    event = SSEEvent.from_json(message["data"])
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning("Невалидное SSE-сообщение из Redis: %s", exc)
                    continue
                self._subscribers.fan_out(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Чтение канала SSE из Redis прервано")

    async def subscribe(
        self, user_id: uuid.UUID, document_ids: frozenset[uuid.UUID]
    ) -> tuple[str, SubscriberQueue]:
        return self._subscribers.add(user_id, document_ids)

    def unsubscribe(self, sub_id: str) -> None:
        self._subscribers.remove(sub_id)

    async def publish(self, event: SSEEvent) -> None:
        if self._redis is None:
            return
        await self._redis.publish(self._channel, event.to_json())


_broker: SSEBroker | None = None


async def init_sse_broker(redis_url: str | None, channel: str) -> SSEBroker:
    """Поднять брокер при старте приложения. Без Redis — in-memory режим."""
    global _broker
    if redis_url:
        try:
            broker = RedisPubSubBroker(redis_url=redis_url, channel=channel)
            await broker.start()
            _broker = broker
            logger.info("SSE: Redis Pub/Sub, канал %s", channel)
        except Exception:
            logger.warning("SSE: Redis недоступен, используется in-memory брокер", exc_info=True)
            _broker = InMemorySSEBroker()
    else:
        _broker = InMemorySSEBroker()
        logger.info("SSE: in-memory брокер (только один процесс)")
    return _broker


async def shutdown_sse_broker() -> None:
    global _broker
    if _broker is not None:
        await _broker.stop()
        _broker = None


def get_sse_broker() -> SSEBroker:
    global _broker
    if _broker is None:
        _broker = InMemorySSEBroker()
    return _broker
