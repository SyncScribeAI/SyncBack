"""Сверка очереди: задача, потерянная между записью статуса и отправкой в брокер."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.dispatch_reconciler import reconcile_once
from app.domain.services.analysis_job_service import AnalysisJobService
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.enums import AnalysisJobStatus, DocumentStatus
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from tests.contract.conftest import create_project, register_and_login, upload_document
from tests.contract.test_analysis_jobs import (
    FakeAnalysisQueue,
    _document_state,
    _job_row,
    queue,  # noqa: F401
)

pytestmark = pytest.mark.asyncio

_STALE = timedelta(minutes=5)


async def _start(
    client: AsyncClient, email: str = "user@example.com"
) -> tuple[str, str, str, dict[str, str]]:
    headers = await register_and_login(client, email)
    project_id = await create_project(client, headers)
    document = await upload_document(client, headers, project_id)
    url = f"/api/v1/projects/{project_id}/documents/{document['id']}/analysis-jobs"
    response = await client.post(url, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"], document["id"], url, headers


async def _lose(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: str,
    *,
    status: AnalysisJobStatus = AnalysisJobStatus.DISPATCHED,
    age: timedelta = timedelta(hours=1),
) -> None:
    """Состояние после падения API: статус записан, сообщения в брокере нет."""
    async with sessionmaker() as session:
        await session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == uuid.UUID(job_id))
            .values(
                status=status,
                celery_task_id=None,
                created_at=datetime.now(UTC) - age,
            )
        )
        if status is AnalysisJobStatus.PENDING:
            # До отправки документ остаётся черновиком, как после create_job.
            await session.execute(
                update(Document)
                .where(Document.current_analysis_job_id == uuid.UUID(job_id))
                .values(status=DocumentStatus.DRAFT)
            )
        await session.commit()


async def _requeue(sessionmaker: async_sessionmaker[AsyncSession], queue: FakeAnalysisQueue) -> int:
    async with sessionmaker() as session:
        service = AnalysisJobService(SqlAlchemyUnitOfWork(session), queue=queue)
        return await service.requeue_lost_jobs(older_than=_STALE, limit=10)


async def test_lost_dispatched_job_is_sent_again(
    client: AsyncClient,
    queue: FakeAnalysisQueue,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    job_id, document_id, _, _ = await _start(client)
    await _lose(sessionmaker, job_id)

    assert await _requeue(sessionmaker, queue) == 1

    assert [str(sent) for sent, _ in queue.enqueued] == [job_id, job_id]
    row = await _job_row(sessionmaker, job_id)
    assert row.status is AnalysisJobStatus.DISPATCHED
    assert row.celery_task_id == "task-2"
    assert (await _document_state(sessionmaker, document_id))[0] == "in_progress"
    # Идентификатор записан, и следующая сверка задачу не трогает.
    assert await _requeue(sessionmaker, queue) == 0
    assert len(queue.enqueued) == 2


async def test_lost_pending_job_is_dispatched(
    client: AsyncClient,
    queue: FakeAnalysisQueue,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    job_id, document_id, _, _ = await _start(client)
    await _lose(sessionmaker, job_id, status=AnalysisJobStatus.PENDING)

    assert await _requeue(sessionmaker, queue) == 1

    row = await _job_row(sessionmaker, job_id)
    assert row.status is AnalysisJobStatus.DISPATCHED
    assert row.celery_task_id == "task-2"
    assert (await _document_state(sessionmaker, document_id))[0] == "in_progress"


async def test_fresh_and_started_jobs_are_left_alone(
    client: AsyncClient,
    queue: FakeAnalysisQueue,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fresh_id, _, _, _ = await _start(client)
    await _lose(sessionmaker, fresh_id, age=timedelta(seconds=10))
    started_id, _, _, _ = await _start(client, "other@example.com")
    await _lose(sessionmaker, started_id, status=AnalysisJobStatus.PROCESSING)

    assert await _requeue(sessionmaker, queue) == 0
    assert len(queue.enqueued) == 2


async def test_broker_failure_releases_document(
    client: AsyncClient,
    queue: FakeAnalysisQueue,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    job_id, document_id, url, headers = await _start(client)
    await _lose(sessionmaker, job_id)
    queue.fail = True

    assert await _requeue(sessionmaker, queue) == 0

    row = await _job_row(sessionmaker, job_id)
    assert row.status is AnalysisJobStatus.FAILED
    assert row.error_code == "QUEUE_UNAVAILABLE"
    assert (await _document_state(sessionmaker, document_id))[0] == "draft"
    # Документ больше не заблокирован активной задачей.
    queue.fail = False
    response = await client.post(url, headers=headers)
    assert response.status_code == 201, response.text


async def test_reconcile_once_survives_database_errors() -> None:
    @asynccontextmanager
    async def broken() -> AsyncIterator[AnalysisJobService]:
        raise ConnectionError("db down")
        yield  # pragma: no cover

    assert await reconcile_once(get_settings(), broken) == 0
