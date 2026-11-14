"""Логическая отмена анализа на реальной БД (SQLite): API, воркер v2 и подтверждение остановки.

ML-конвейер и источники заменены заглушками из test_worker_pipeline_v2: тесты проверяют,
что SyncBack перестаёт ждать и честно отражает состояние вычисления, а не работу ML.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.workers.tasks.analysis_v2_tasks as v2
from app.domain.exceptions import LLMTimeoutError
from app.domain.interfaces.document_analyzer import (
    AnalysisError,
    AnalysisErrorCode,
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
)
from app.domain.interfaces.source_connector import SourceRef
from app.domain.services.analysis_job_service import AnalysisJobService
from app.domain.value_objects import AnalysisExecutionStateVO
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.enums import AnalysisJobStatus
from app.infrastructure.db.repositories.analysis_job_repository import AnalysisJobRepository
from tests.contract.test_analysis_jobs import FakeAnalysisQueue, queue  # noqa: F401
from tests.contract.test_worker_pipeline_v2 import (
    Worker,
    _job,
    _start,
    _suggestions,
    worker,  # noqa: F401
)

pytestmark = pytest.mark.asyncio


async def _cancel(
    client: AsyncClient, headers: dict[str, str], project_id: str, doc: str, job: str
) -> Response:
    return await client.delete(
        f"/api/v1/projects/{project_id}/documents/{doc}/analysis-jobs/{job}", headers=headers
    )


async def _set_status(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: str,
    status: AnalysisJobStatus,
    **values: object,
) -> None:
    async with sessionmaker() as session:
        await session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == uuid.UUID(job_id))
            .values(status=status, **values)
        )
        await session.commit()


async def test_cancel_before_start_is_stopped_at_once(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])

    response = await _cancel(client, headers, project_id, document_id, job_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["execution_state"]) == ("cancelled", "stopped")
    assert body["cancel_requested_at"] is not None
    assert body["execution_stopped_at"] is not None

    # Доставка уже снятой задачи ничего не запускает и состояние не портит.
    assert await v2.analyze_document(job_id, source_ids, 0) is None
    assert worker.analyzer.requests == []
    assert (await _job(sessionmaker, job_id)).execution_state == "stopped"


async def test_cancel_of_running_job_waits_for_worker_acknowledgement(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, project_id, document_id, job_id, _ = await _start(client, queue, ["A"])
    await _set_status(sessionmaker, job_id, AnalysisJobStatus.PROCESSING, execution_state="running")

    first = (await _cancel(client, headers, project_id, document_id, job_id)).json()
    assert (first["status"], first["execution_state"]) == ("cancelled", "cancel_requested")
    assert first["execution_stopped_at"] is None

    again = await _cancel(client, headers, project_id, document_id, job_id)
    assert again.status_code == 200
    assert again.json()["execution_state"] == "cancel_requested"
    # SQLite теряет пояс времени при чтении, поэтому сравниваем без суффикса Z.
    repeated_at = again.json()["cancel_requested_at"].rstrip("Z")
    assert repeated_at == first["cancel_requested_at"].rstrip("Z")

    async with sessionmaker() as session:
        repo = AnalysisJobRepository(session)
        assert await repo.acknowledge_stop(uuid.UUID(job_id), AnalysisExecutionStateVO.STOPPED)
        # Повторное подтверждение не меняет итог: его могут прислать две доставки задачи.
        assert not await repo.acknowledge_stop(uuid.UUID(job_id), AnalysisExecutionStateVO.UNKNOWN)
        await session.commit()
    job = await _job(sessionmaker, job_id)
    assert (job.execution_state, job.execution_stopped_at is not None) == ("stopped", True)


async def test_cancel_does_not_overwrite_result_written_meanwhile(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, project_id, document_id, job_id, _ = await _start(client, queue, ["A"])
    await _set_status(sessionmaker, job_id, AnalysisJobStatus.PROCESSING)
    read_job = AnalysisJobService._get_job

    async def result_lands_after_read(self: AnalysisJobService, *args: uuid.UUID) -> AnalysisJob:
        job = await read_job(self, *args)
        # Воркер записал success, пока отмена держала прочитанную строку.
        await _set_status(sessionmaker, job_id, AnalysisJobStatus.SUCCESS)
        return job

    monkeypatch.setattr(AnalysisJobService, "_get_job", result_lands_after_read)

    response = await _cancel(client, headers, project_id, document_id, job_id)
    assert response.status_code == 409, response.text
    assert (await _job(sessionmaker, job_id)).status.value == "success"


async def test_pause_for_retry_fails_after_cancel(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, project_id, document_id, job_id, _ = await _start(client, queue, ["A"])
    await _set_status(sessionmaker, job_id, AnalysisJobStatus.PROCESSING, execution_state="running")
    await _cancel(client, headers, project_id, document_id, job_id)

    async with sessionmaker() as session:
        repo = AnalysisJobRepository(session)
        assert not await repo.pause_for_retry(
            uuid.UUID(job_id), AnalysisExecutionStateVO.NOT_STARTED
        )


async def test_cancel_during_ml_wait_ends_attempt_quickly(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker.configure(monkeypatch, analysis_cancel_poll_seconds=0.02)
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])
    interrupted = asyncio.Event()

    async def cancel_and_hang(request: AnalysisRequest) -> AnalysisResult:
        worker.analyzer.requests.append(request)
        response = await _cancel(client, headers, project_id, document_id, job_id)
        assert response.json()["execution_state"] == "cancel_requested"
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            interrupted.set()
            raise
        raise AssertionError("ожидание ML должно быть снято")

    worker.analyzer.analyze = cancel_and_hang  # type: ignore[method-assign]
    started = time.perf_counter()
    step = await v2.analyze_document(job_id, source_ids, 0)

    assert step is None
    assert time.perf_counter() - started < 10
    assert interrupted.is_set()
    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code) == ("cancelled", "ANALYSIS_CANCELLED")
    # Запрос к ML брошен без ответа: остановку вычисления в ML никто не подтвердил.
    assert job.execution_state == "unknown"
    assert job.execution_stopped_at is not None
    assert await _suggestions(sessionmaker) == []


async def test_cancel_while_fetching_sources_skips_ml(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])
    fetch = worker.connector.fetch_structured

    async def cancel_then_fetch(source: SourceRef) -> str:
        await _cancel(client, headers, project_id, document_id, job_id)
        return await fetch(source)

    monkeypatch.setattr(worker.connector, "fetch_structured", cancel_then_fetch)

    assert await v2.analyze_document(job_id, source_ids, 0) is None

    assert worker.analyzer.requests == []
    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.execution_state) == ("cancelled", "stopped")


async def test_cancelled_job_is_not_retried_after_timeout(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])

    async def cancel_then_timeout(request: AnalysisRequest) -> AnalysisResult:
        await _cancel(client, headers, project_id, document_id, job_id)
        raise LLMTimeoutError("нет ответа")

    worker.analyzer.analyze = cancel_then_timeout  # type: ignore[method-assign]

    assert await v2.analyze_document(job_id, source_ids, 0) is None

    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code) == ("cancelled", "ANALYSIS_CANCELLED")
    assert job.execution_state == "unknown"


async def test_retry_wait_is_not_started_and_cancel_stops_at_once(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker.configure(monkeypatch, analysis_retry_limit=1)
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])

    def pipeline_error(request: AnalysisRequest) -> AnalysisResult:
        return AnalysisResult(
            status=AnalysisStatus.FAILED,
            error=AnalysisError(AnalysisErrorCode.PIPELINE_ERROR, "GPU занят"),
        )

    worker.analyzer.respond = pipeline_error
    step = await v2.analyze_document(job_id, source_ids, 0)
    assert isinstance(step, v2.RetryLater)
    assert (await _job(sessionmaker, job_id)).execution_state == "not_started"

    body = (await _cancel(client, headers, project_id, document_id, job_id)).json()
    assert body["execution_state"] == "stopped"


async def test_redelivery_of_cancel_requested_job_marks_unknown(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])
    await _set_status(sessionmaker, job_id, AnalysisJobStatus.PROCESSING, execution_state="running")
    await _cancel(client, headers, project_id, document_id, job_id)

    # Воркер, принявший отмену, пропал; новая доставка закрывает ожидание подтверждения.
    assert await v2.analyze_document(job_id, source_ids, 0) is None

    assert worker.analyzer.requests == []
    assert (await _job(sessionmaker, job_id)).execution_state == "unknown"
