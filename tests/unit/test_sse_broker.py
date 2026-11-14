"""Раздача SSE-событий подписчикам и отображение доменных событий в SSE."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from app.api.v1.routers.sse import DOCUMENT_IDS_MAX, parse_document_ids
from app.domain.events import DocumentStatusChanged
from app.domain.value_objects import DocumentStatusVO
from app.infrastructure.events.publishers import SSEEventPublisher, to_sse_event
from app.infrastructure.events.sse_broker import SUBSCRIBER_QUEUE_SIZE, InMemorySSEBroker, SSEEvent

pytestmark = pytest.mark.asyncio


def _event(user_id: uuid.UUID, document_id: uuid.UUID | None = None) -> SSEEvent:
    return SSEEvent(
        event="document_status_changed", data={}, document_id=document_id, user_id=user_id
    )


async def test_in_memory_broker_delivers_only_to_owner_and_matching_filter() -> None:
    broker = InMemorySSEBroker()
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    _, owner_all = await broker.subscribe(owner, frozenset())
    _, owner_only_a = await broker.subscribe(owner, frozenset({doc_a}))
    _, stranger_all = await broker.subscribe(stranger, frozenset())

    await broker.publish(_event(owner, doc_a))
    await broker.publish(_event(owner, doc_b))

    assert owner_all.qsize() == 2
    assert owner_only_a.qsize() == 1 and owner_only_a.get_nowait().document_id == doc_a
    assert stranger_all.empty()


async def test_in_memory_broker_drops_events_when_subscriber_queue_is_full() -> None:
    broker = InMemorySSEBroker()
    owner = uuid.uuid4()
    _, queue = await broker.subscribe(owner, frozenset())
    for _ in range(SUBSCRIBER_QUEUE_SIZE + 5):
        await broker.publish(_event(owner))
    assert queue.qsize() == SUBSCRIBER_QUEUE_SIZE


async def test_unsubscribed_queue_receives_nothing_and_stop_closes_streams() -> None:
    broker = InMemorySSEBroker()
    owner = uuid.uuid4()
    sub_id, gone = await broker.subscribe(owner, frozenset())
    _, alive = await broker.subscribe(owner, frozenset())
    broker.unsubscribe(sub_id)
    await broker.publish(_event(owner))
    assert gone.empty() and alive.qsize() == 1

    await broker.stop()
    alive.get_nowait()
    assert alive.get_nowait() is None


def test_sse_event_json_round_trip_and_wire_format() -> None:
    document_id, user_id = uuid.uuid4(), uuid.uuid4()
    event = SSEEvent("x", {"status": "ready"}, document_id=document_id, user_id=user_id)
    assert SSEEvent.from_json(event.to_json()) == event
    assert event.to_sse_bytes() == b'event: x\ndata: {"event": "x", "status": "ready"}\n\n'


async def test_domain_event_is_mapped_and_published_to_broker() -> None:
    broker = InMemorySSEBroker()
    owner, document_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _, queue = await broker.subscribe(owner, frozenset())
    domain_event = DocumentStatusChanged(
        document_id=document_id,
        project_id=uuid.uuid4(),
        owner_id=owner,
        status=DocumentStatusVO.AWAITING_APPROVAL,
        current_analysis_job_id=job_id,
    )

    await SSEEventPublisher(broker).publish(domain_event)

    sse = await asyncio.wait_for(queue.get(), timeout=1)
    assert sse == to_sse_event(domain_event)
    assert sse.user_id == owner and sse.document_id == document_id
    assert sse.data == {
        "document_id": str(document_id),
        "project_id": str(domain_event.project_id),
        "status": "awaiting_approval",
        "current_analysis_job_id": str(job_id),
    }


def test_parse_document_ids_validates_input() -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    assert parse_document_ids(None) == frozenset()
    assert parse_document_ids(",".join(map(str, ids)) + ", ") == frozenset(ids)
    with pytest.raises(HTTPException) as too_many:
        parse_document_ids(",".join(str(uuid.uuid4()) for _ in range(DOCUMENT_IDS_MAX + 1)))
    assert too_many.value.status_code == 422
    with pytest.raises(HTTPException) as invalid:
        parse_document_ids("not-a-uuid")
    assert invalid.value.status_code == 422


async def test_stop_finishes_when_subscriber_queue_is_full() -> None:
    broker = InMemorySSEBroker()
    user_id = uuid.uuid4()
    _, queue = await broker.subscribe(user_id, frozenset())
    for _ in range(SUBSCRIBER_QUEUE_SIZE + 5):
        await broker.publish(_event(user_id))

    await asyncio.wait_for(broker.stop(), timeout=1)

    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert items[-1] is None
    assert len(items) == SUBSCRIBER_QUEUE_SIZE
