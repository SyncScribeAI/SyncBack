"""Задача анализа v2 на реальной БД (SQLite) вместе с API: от запуска анализа до правок.

Celery, S3, Redis и ML-конвейер не используются: задача вызывается напрямую через
analyze_document, конвейер — управляемая заглушка DocumentAnalyzer.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

import app.workers.tasks.analysis_v2_tasks as v2
from app.core.config import get_settings
from app.domain.events import DomainEvent
from app.domain.exceptions import LLMInvalidResponseError, LLMTimeoutError
from app.domain.interfaces.document_analyzer import (
    AnalysisError,
    AnalysisErrorCode,
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    ProposedChange,
)
from app.domain.interfaces.source_connector import SourceRef
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.suggestion import Suggestion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from tests.contract.conftest import (
    InMemoryFileStorage,
    create_project,
    register_and_login,
    upload_document,
)
from tests.contract.test_analysis_jobs import FakeAnalysisQueue, queue  # noqa: F401

pytestmark = pytest.mark.asyncio

DOCUMENT = (
    "## Сроки\n\nСрок ответа — 10 рабочих дней.\n\n## Оплата\n\nОплата принимается картой.\n"
).encode()

Respond = Callable[[AnalysisRequest], AnalysisResult]


def _block_ref(request: AnalysisRequest, text: str) -> str:
    return next(block.ref for block in request.document.blocks if text in block.text)


def modify_deadline(request: AnalysisRequest) -> AnalysisResult:
    return AnalysisResult(
        status=AnalysisStatus.COMPLETED,
        suggestions=(
            ProposedChange(
                kind="modify",
                block_ref=_block_ref(request, "10 рабочих"),
                old_text="10 рабочих дней",
                new_text="5 рабочих дней",
                rationale="Так в решениях встречи",
                confidence=0.9,
            ),
            # Не дословно — должна быть отброшена маппером, а не уронить ответ.
            ProposedChange(
                kind="modify",
                block_ref=_block_ref(request, "картой"),
                old_text="наличными",
                new_text="картой",
            ),
        ),
        meta={"checked_blocks": 4, "errors": 0},
    )


@dataclass
class FakeAnalyzer:
    respond: Respond = modify_deadline
    failures: list[Exception] = field(default_factory=list)
    requests: list[AnalysisRequest] = field(default_factory=list)

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        return self.respond(request)

    async def health_check(self) -> bool:
        return True


@dataclass
class FakeConnector:
    broken: set[str] = field(default_factory=set)
    texts: dict[str, str] = field(default_factory=dict)

    async def fetch_structured(self, source: SourceRef) -> str:
        if source.name in self.broken:
            raise ConnectionError("источник недоступен")
        return self.texts.get(source.name, f"Решения: срок ответа 5 рабочих дней ({source.name})")


@dataclass
class Worker:
    analyzer: FakeAnalyzer
    connector: FakeConnector
    events: list[DomainEvent]
    uploads: dict[str, bytes]

    def configure(self, monkeypatch: pytest.MonkeyPatch, **settings: object) -> None:
        monkeypatch.setattr(v2, "_settings", v2._settings.model_copy(update=settings))


@pytest.fixture
def worker(
    monkeypatch: pytest.MonkeyPatch,
    sessionmaker: async_sessionmaker[AsyncSession],
    file_storage: InMemoryFileStorage,
) -> Worker:
    @asynccontextmanager
    async def uow_factory() -> AsyncIterator[SqlAlchemyUnitOfWork]:
        async with sessionmaker() as session:
            yield SqlAlchemyUnitOfWork(session)

    events: list[DomainEvent] = []

    class Recorder:
        async def publish(self, event: DomainEvent) -> None:
            events.append(event)

    analyzer = FakeAnalyzer()
    connector = FakeConnector()
    monkeypatch.setattr(v2, "uow_factory", uow_factory)
    monkeypatch.setattr(v2, "get_storage", lambda: file_storage)
    monkeypatch.setattr(v2, "get_connector_for", lambda kind: connector)
    monkeypatch.setattr(v2, "event_publisher", Recorder)
    monkeypatch.setattr(v2, "_get_analyzer", lambda: analyzer)
    monkeypatch.setattr(v2, "_settings", get_settings().model_copy())
    return Worker(analyzer, connector, events, file_storage.files)


async def _start(
    client: AsyncClient, queue: FakeAnalysisQueue, source_names: list[str]
) -> tuple[dict[str, str], str, str, str, list[str]]:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = await upload_document(
        client, headers, project_id, filename="spec.md", content=DOCUMENT
    )
    for name in source_names:
        response = await client.post(
            f"/api/v1/projects/{project_id}/sources/note",
            json={"name": name, "text_content": "t", "scope": "project"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
    response = await client.post(
        f"/api/v1/projects/{project_id}/documents/{document['id']}/analysis-jobs",
        headers=headers,
    )
    assert response.status_code == 201, response.text
    job_id, source_ids = queue.enqueued[-1]
    return headers, project_id, document["id"], str(job_id), [str(s) for s in source_ids]


async def _job(sessionmaker: async_sessionmaker[AsyncSession], job_id: str) -> AnalysisJob:
    async with sessionmaker() as session:
        job = await session.get(AnalysisJob, uuid.UUID(job_id))
        assert job is not None
        return job


async def _document_status(sessionmaker: async_sessionmaker[AsyncSession], doc_id: str) -> str:
    async with sessionmaker() as session:
        document = await session.get(Document, uuid.UUID(doc_id))
        assert document is not None
        return document.status.value


async def _suggestions(sessionmaker: async_sessionmaker[AsyncSession]) -> list[Suggestion]:
    async with sessionmaker() as session:
        query = select(Suggestion).options(selectinload(Suggestion.sources))
        return list((await session.execute(query)).scalars().all())


async def test_success_writes_suggestions_with_all_request_sources(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A", "B"])

    assert await v2.analyze_document(job_id, source_ids, 0) is None

    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code, job.partial_success) == ("success", None, False)
    assert await _document_status(sessionmaker, document_id) == "awaiting_approval"
    assert [(str(e.document_id), e.status.value) for e in worker.events] == [
        (document_id, "awaiting_approval")
    ]

    [request] = worker.analyzer.requests
    assert request.request_id == f"{job_id}:0"
    assert [s.name for s in request.sources] == ["A", "B"]
    assert [b.type.value for b in request.document.blocks][:2] == ["heading", "paragraph"]

    [suggestion] = await _suggestions(sessionmaker)
    assert (suggestion.section_ref, suggestion.change_type.value) == ("Сроки", "modify")
    assert (suggestion.original_text, suggestion.suggested_text) == (
        "10 рабочих дней",
        "5 рабочих дней",
    )
    assert float(suggestion.confidence_score or 0) == 0.9
    assert sorted(s.name for s in suggestion.sources) == ["A", "B"]

    response = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document_id}/suggestions", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


async def test_redelivery_after_completion_does_not_duplicate(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])
    await v2.analyze_document(job_id, source_ids, 0)
    await v2.analyze_document(job_id, source_ids, 0)

    assert len(worker.analyzer.requests) == 1
    assert len(await _suggestions(sessionmaker)) == 1


async def test_late_result_of_cancelled_job_is_dropped(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])

    async def cancel_while_waiting(request: AnalysisRequest) -> AnalysisResult:
        response = await client.delete(
            f"/api/v1/projects/{project_id}/documents/{document_id}/analysis-jobs/{job_id}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return modify_deadline(request)

    worker.analyzer.analyze = cancel_while_waiting  # type: ignore[method-assign]
    await v2.analyze_document(job_id, source_ids, 0)

    assert (await _job(sessionmaker, job_id)).status.value == "cancelled"
    assert await _document_status(sessionmaker, document_id) == "draft"
    assert await _suggestions(sessionmaker) == []


async def test_unavailable_source_and_failed_blocks_give_partial_success(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    worker.connector.broken = {"B"}

    def with_errors(request: AnalysisRequest) -> AnalysisResult:
        result = modify_deadline(request)
        return AnalysisResult(
            status=result.status, suggestions=result.suggestions, meta={"errors": 2}
        )

    worker.analyzer.respond = with_errors
    _, _, document_id, job_id, source_ids = await _start(client, queue, ["A", "B"])

    await v2.analyze_document(job_id, source_ids, 0)

    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code, job.partial_success) == (
        "partial_success",
        "PARTIAL_FAILURE",
        True,
    )
    assert job.error_message == "2 блоков не удалось проверить; недоступны источники: B"
    assert [s.name for s in worker.analyzer.requests[0].sources] == ["A"]
    [suggestion] = await _suggestions(sessionmaker)
    assert [s.name for s in suggestion.sources] == ["A"]
    assert await _document_status(sessionmaker, document_id) == "awaiting_approval"


async def test_all_sources_unavailable_fails_without_calling_pipeline(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    worker.connector.broken = {"A"}
    _, _, document_id, job_id, source_ids = await _start(client, queue, ["A"])

    await v2.analyze_document(job_id, source_ids, 0)

    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code) == ("failed", "SOURCES_UNAVAILABLE")
    assert job.error_message == "Недоступны все источники: A"
    assert worker.analyzer.requests == []
    assert await _document_status(sessionmaker, document_id) == "draft"


async def test_without_sources_document_is_ready_without_pipeline(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, _, document_id, job_id, source_ids = await _start(client, queue, [])

    await v2.analyze_document(job_id, source_ids, 0)

    assert (await _job(sessionmaker, job_id)).status.value == "success"
    assert await _document_status(sessionmaker, document_id) == "ready"
    assert worker.analyzer.requests == []


async def test_source_over_limit_fails_job_before_pipeline(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker.configure(monkeypatch, analysis_max_source_chars=10)
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])

    await v2.analyze_document(job_id, source_ids, 0)

    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code) == ("failed", "LLM_INPUT_TOO_LARGE")
    assert "«A»" in (job.error_message or "")
    assert worker.analyzer.requests == []


async def test_too_many_blocks_fails_job_before_pipeline(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker.configure(monkeypatch, analysis_max_blocks=2)
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])

    await v2.analyze_document(job_id, source_ids, 0)

    assert (await _job(sessionmaker, job_id)).error_code == "LLM_INPUT_TOO_LARGE"
    assert worker.analyzer.requests == []


async def test_timeout_is_retried_then_fails_as_unavailable(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker.configure(monkeypatch, analysis_retry_limit=1)
    worker.analyzer.failures = [LLMTimeoutError("нет ответа"), LLMTimeoutError("нет ответа")]
    _, _, document_id, job_id, source_ids = await _start(client, queue, ["A"])

    step = await v2.analyze_document(job_id, source_ids, 0)
    assert step == v2.RetryLater(countdown=30)
    job = await _job(sessionmaker, job_id)
    assert job.status.value == "processing"

    assert await v2.analyze_document(job_id, source_ids, 1) is None

    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code, job.retry_count) == ("failed", "LLM_UNAVAILABLE", 1)
    assert [r.request_id for r in worker.analyzer.requests] == [f"{job_id}:0", f"{job_id}:1"]
    assert await _document_status(sessionmaker, document_id) == "draft"


async def test_pipeline_error_is_retried_and_next_attempt_succeeds(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    attempts: list[str] = []

    def flaky(request: AnalysisRequest) -> AnalysisResult:
        attempts.append(request.request_id)
        if len(attempts) == 1:
            return AnalysisResult(
                status=AnalysisStatus.FAILED,
                error=AnalysisError(AnalysisErrorCode.PIPELINE_ERROR, "GPU занят"),
            )
        return modify_deadline(request)

    worker.analyzer.respond = flaky
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])

    assert await v2.analyze_document(job_id, source_ids, 0) == v2.RetryLater(countdown=30)
    assert await v2.analyze_document(job_id, source_ids, 1) is None

    assert (await _job(sessionmaker, job_id)).status.value == "success"
    assert len(await _suggestions(sessionmaker)) == 1


@pytest.mark.parametrize(
    ("respond_or_failure", "error_code"),
    [
        (
            lambda request: AnalysisResult(
                status=AnalysisStatus.FAILED,
                error=AnalysisError(AnalysisErrorCode.INVALID_REQUEST, "нет блоков"),
            ),
            "ML_INVALID_REQUEST",
        ),
        (LLMInvalidResponseError("Ответ не соответствует контракту"), "LLM_INVALID_RESPONSE"),
    ],
)
async def test_non_retryable_failures(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
    respond_or_failure: Respond | Exception,
    error_code: str,
) -> None:
    if isinstance(respond_or_failure, Exception):
        worker.analyzer.failures = [respond_or_failure]
    else:
        worker.analyzer.respond = respond_or_failure
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])

    assert await v2.analyze_document(job_id, source_ids, 0) is None

    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code) == ("failed", error_code)
    assert len(worker.analyzer.requests) == 1


async def test_broken_document_fails_with_parse_error(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])

    class MissingStorage:
        async def download(self, key: str) -> bytes:
            raise FileNotFoundError(key)

    monkeypatch.setattr(v2, "get_storage", MissingStorage)
    await v2.analyze_document(job_id, source_ids, 0)

    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code) == ("failed", "DOCUMENT_PARSE_ERROR")
    assert job.error_message == "Не удалось разобрать документ: FileNotFoundError"


async def test_payloads_are_stored_only_when_enabled(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])
    keys_before = set(worker.uploads)
    worker.configure(monkeypatch, analysis_store_payloads=True)

    await v2.analyze_document(job_id, source_ids, 0)

    stored = sorted(set(worker.uploads) - keys_before)
    assert stored == [
        f"analysis_runs/{job_id}/0.request.json",
        f"analysis_runs/{job_id}/0.response.json",
    ]
    assert b'"contract_version":"2.0"' in worker.uploads[stored[0]]


async def test_payloads_are_not_stored_by_default(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
) -> None:
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])
    await v2.analyze_document(job_id, source_ids, 0)
    assert not [key for key in worker.uploads if key.startswith("analysis_runs/")]
