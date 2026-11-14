"""Задача анализа v2 на реальной БД (SQLite) вместе с API: от запуска анализа до правок.

Celery, S3, Redis и ML-конвейер не используются: задача вызывается напрямую через
analyze_document, конвейер — управляемая заглушка DocumentAnalyzer.
"""

from __future__ import annotations

import logging
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
from app.domain.source_text import SourceLayout
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.suggestion import Suggestion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.layout.cached_reader import CachedDocumentLayoutReader
from app.workers.tasks.runtime import get_parser_registry
from app.domain.value_objects import DocumentStatusVO
from tests.contract.conftest import (
    InMemoryFileStorage,
    create_project,
    register_and_login,
    set_document_status,
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

    async def fetch_layout(self, source: SourceRef) -> SourceLayout:
        return SourceLayout(await self.fetch_structured(source))


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
    reader = CachedDocumentLayoutReader(get_parser_registry(), file_storage)
    monkeypatch.setattr(v2, "get_document_layout_reader", lambda: reader)
    monkeypatch.setattr(v2, "get_connector_for", lambda kind: connector)
    monkeypatch.setattr(v2, "event_publisher", Recorder)
    monkeypatch.setattr(v2, "_get_analyzer", lambda: analyzer)
    monkeypatch.setattr(v2, "_settings", get_settings().model_copy())
    return Worker(analyzer, connector, events, file_storage.files)


async def _start(
    client: AsyncClient,
    queue: FakeAnalysisQueue,
    source_names: list[str],
    content: bytes = DOCUMENT,
) -> tuple[dict[str, str], str, str, str, list[str]]:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = await upload_document(
        client, headers, project_id, filename="spec.md", content=content
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
    # Источники созданы в одну секунду, а SQLite хранит created_at с точностью до секунды:
    # порядок между ними задаёт uuid, поэтому сравниваем без порядка.
    assert sorted(s.name for s in request.sources) == ["A", "B"]
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

    # Снимки источников пишутся всегда и проверяются отдельным тестом.
    stored = sorted(
        key for key in set(worker.uploads) - keys_before if key.startswith("analysis_runs/")
    )
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


async def test_suggestions_are_bound_to_blocks_and_keep_pipeline_order(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Конвейер вернул правки не в порядке документа: выдача должна следовать ответу.
    def payment_then_deadline(request: AnalysisRequest) -> AnalysisResult:
        return AnalysisResult(
            status=AnalysisStatus.COMPLETED,
            suggestions=(
                ProposedChange(
                    kind="modify",
                    block_ref=_block_ref(request, "картой"),
                    old_text="картой",
                    new_text="картой или по счёту",
                ),
                ProposedChange(
                    kind="add",
                    block_ref=_block_ref(request, "10 рабочих"),
                    old_text=None,
                    new_text=" Срок можно продлить.",
                    anchor_text="10 рабочих дней.",
                ),
            ),
            meta={"checked_blocks": 2, "errors": 0, "llm_seconds": 1.5},
        )

    worker.analyzer.respond = payment_then_deadline
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])

    await v2.analyze_document(job_id, source_ids, 0)

    editor = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document_id}/editor", headers=headers
    )
    assert editor.status_code == 200, editor.text
    body = editor.json()
    plain_text = body["content"]["plain_text"]
    blocks = {block["ref"]: block for block in body["content"]["blocks"]}
    items = body["suggestions"]
    assert [(item["original_text"], item["position"]) for item in items] == [
        ("картой", 0),
        ("10 рабочих дней.", 1),
    ]
    for item in items:
        block = blocks[item["block_id"]]
        assert block["start_offset"] <= item["start_offset"] < item["end_offset"]
        assert item["end_offset"] <= block["end_offset"]
        assert plain_text[item["start_offset"] : item["end_offset"]] == item["original_text"]

    job = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document_id}/analysis-jobs/{job_id}",
        headers=headers,
    )
    assert job.status_code == 200, job.text
    assert (job.json()["checked_blocks"], job.json()["failed_blocks"]) == (2, 0)

    stored = await _job(sessionmaker, job_id)
    assert stored.request_id == f"{job_id}:0"
    assert stored.pipeline_meta is not None
    document_hash = stored.pipeline_meta["incremental"]["document_hash"]
    assert len(document_hash) == 64
    assert stored.pipeline_meta == {
        "contract_version": "2.0",
        "provider": v2._settings.analysis_provider,
        "meta": {"checked_blocks": 2, "errors": 0, "llm_seconds": 1.5},
        "evidence": {
            "evidence_exact": 0,
            "evidence_approximate": 0,
            "evidence_not_found": 0,
            "evidence_unparsed": 0,
        },
        "incremental": {
            "mode": "full",
            "reason": "no_baseline",
            "baseline_job_id": None,
            "chain_length": 0,
            "fallback_reason": "no_baseline",
            "document_hash": document_hash,
        },
        # Один источник «Решения: срок ответа 5 рабочих дней (A)»: один сегмент, 39 символов.
        "run_stats": {
            "segments_total": 1,
            "segments_sent": 1,
            "chars_total": 39,
            "chars_sent": 39,
            # Заголовки уходят как контекст и не проверяются.
            "blocks_total": 4,
            "blocks_checked": 2,
            "harness": {"llm_seconds": 1.5},
        },
    }
    assert job.json()["incremental"]["reason"] == "no_baseline"
    assert job.json()["run_stats"]["harness"] == {"llm_seconds": 1.5}


async def test_failed_pipeline_answer_keeps_request_id(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    worker.analyzer.respond = lambda request: AnalysisResult(
        status=AnalysisStatus.FAILED,
        error=AnalysisError(AnalysisErrorCode.INVALID_REQUEST, "нет блоков"),
        meta={"blocks": 0},
    )
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])

    await v2.analyze_document(job_id, source_ids, 0)

    job = await _job(sessionmaker, job_id)
    assert (job.status.value, job.error_code) == ("failed", "ML_INVALID_REQUEST")
    assert job.request_id == f"{job_id}:0"
    assert job.pipeline_meta is not None and job.pipeline_meta["meta"] == {"blocks": 0}
    assert (job.checked_blocks, job.failed_blocks) == (None, None)


# ── повторяющиеся фразы (B5a) ────────────────────────────────────────────────────────────────

REPEATED = (
    "## Сроки\n\n"
    "Ответ — 10 дней. Повторный ответ — 10 дней.\n\n"
    "Возврат — 10 дней.\n\n"
    "## Прочее\n\n"
    "Хранение — 10 дней.\n"
).encode()


def _ten_days(request: AnalysisRequest, refs: list[str]) -> AnalysisResult:
    return AnalysisResult(
        status=AnalysisStatus.COMPLETED,
        suggestions=tuple(
            ProposedChange(kind="modify", block_ref=ref, old_text="10 дней", new_text="5 дней")
            for ref in refs
        ),
    )


async def test_repeated_phrase_gets_change_per_occurrence_and_one_group(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    def three_places(request: AnalysisRequest) -> AnalysisResult:
        first = _block_ref(request, "Повторный")
        return _ten_days(request, [first, first, _block_ref(request, "Возврат")])

    worker.analyzer.respond = three_places
    headers, project_id, document_id, job_id, source_ids = await _start(
        client, queue, ["A"], content=REPEATED
    )
    await v2.analyze_document(job_id, source_ids, 0)

    base = f"/api/v1/projects/{project_id}/documents/{document_id}"
    editor = (await client.get(f"{base}/editor", headers=headers)).json()
    plain_text = editor["content"]["plain_text"]
    items = editor["suggestions"]
    assert len(items) == 3
    starts = [item["start_offset"] for item in items]
    assert starts == sorted(set(starts)), "каждая правка — своё вхождение"
    assert all(plain_text[i["start_offset"] : i["end_offset"]] == "10 дней" for i in items)
    keys = {item["group_key"] for item in items}
    assert len(keys) == 1 and None not in keys
    assert {item["origin"] for item in items} == {"model"}
    (group_key,) = keys
    assert editor["suggestion_groups"] == [{"group_key": group_key, "size": 3, "pending": 3}]

    group = await client.get(
        f"{base}/suggestions", params={"group_key": group_key, "limit": 1}, headers=headers
    )
    assert group.status_code == 200, group.text
    assert group.json()["total"] == 3

    first, second, third = (item["id"] for item in items)
    response = await client.patch(
        f"{base}/suggestions", json={"ids": [first, third], "status": "accepted"}, headers=headers
    )
    assert response.status_code == 200, response.text
    response = await client.patch(
        f"{base}/suggestions", json={"ids": [second], "status": "rejected"}, headers=headers
    )
    assert response.status_code == 200, response.text
    editor = (await client.get(f"{base}/editor", headers=headers)).json()
    assert editor["suggestion_groups"] == [{"group_key": group_key, "size": 3, "pending": 0}]

    await set_document_status(sessionmaker, document_id, DocumentStatusVO.READY)
    exported = await client.get(f"{base}/export", headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.text == (
        "## Сроки\n\n"
        "Ответ — 5 дней. Повторный ответ — 10 дней.\n\n"
        "Возврат — 5 дней.\n\n"
        "## Прочее\n\n"
        "Хранение — 10 дней.\n"
    )


async def test_more_changes_than_occurrences_are_dropped(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
) -> None:
    worker.analyzer.respond = lambda request: _ten_days(
        request, [_block_ref(request, "Возврат")] * 2
    )
    headers, project_id, document_id, job_id, source_ids = await _start(
        client, queue, ["A"], content=REPEATED
    )
    await v2.analyze_document(job_id, source_ids, 0)

    editor = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document_id}/editor", headers=headers
    )
    body = editor.json()
    assert len(body["suggestions"]) == 1
    # Единственная правка фразы — без группы.
    assert body["suggestions"][0]["group_key"] is None
    assert body["suggestion_groups"] == []


async def test_propagation_covers_remaining_exact_repeats(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker.configure(monkeypatch, suggestion_propagate_exact=True)
    worker.analyzer.respond = lambda request: _ten_days(request, [_block_ref(request, "Возврат")])
    headers, project_id, document_id, job_id, source_ids = await _start(
        client, queue, ["A"], content=REPEATED
    )
    await v2.analyze_document(job_id, source_ids, 0)

    editor = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document_id}/editor", headers=headers
    )
    body = editor.json()
    items = body["suggestions"]
    assert [item["origin"] for item in items] == ["model"] + ["propagated"] * 3
    assert len({item["group_key"] for item in items}) == 1
    assert {item["section_ref"] for item in items} == {"Сроки", "Прочее"}
    assert all("Найдено автоматически" in (item["rationale"] or "") for item in items[1:])
    assert body["suggestion_groups"][0]["size"] == 4


async def test_propagation_is_off_by_default(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
) -> None:
    worker.analyzer.respond = lambda request: _ten_days(request, [_block_ref(request, "Возврат")])
    headers, project_id, document_id, job_id, source_ids = await _start(
        client, queue, ["A"], content=REPEATED
    )
    await v2.analyze_document(job_id, source_ids, 0)

    editor = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document_id}/editor", headers=headers
    )
    assert [item["origin"] for item in editor.json()["suggestions"]] == ["model"]


def _call_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "Анализ v2: вызов конвейера"]


async def test_pipeline_call_is_logged_with_counters_and_without_texts(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, _, _, job_id, source_ids = await _start(client, queue, ["A", "B"])
    caplog.set_level(logging.INFO, logger=v2.logger.name)

    assert await v2.analyze_document(job_id, source_ids, 0) is None

    [record] = _call_records(caplog)
    fields = record.__dict__
    assert (fields["job_id"], fields["request_id"], fields["attempt"]) == (job_id, f"{job_id}:0", 0)
    assert (fields["outcome"], fields["sources"], fields["suggestions"]) == ("completed", 2, 2)
    assert (fields["checked_blocks"], fields["flags"]) == (4, 0)
    assert fields["blocks"] == len(worker.analyzer.requests[0].document.blocks)
    assert isinstance(fields["duration_ms"], int) and fields["duration_ms"] >= 0
    [dropped] = [r for r in caplog.records if r.getMessage() == "Анализ v2: часть правок отброшена"]
    assert (dropped.__dict__["dropped_total"], dropped.__dict__["request_id"]) == (1, f"{job_id}:0")
    # Тексты документа и правок в записи не попадают.
    logged = " ".join(str(value) for value in fields.values())
    assert "рабочих" not in logged and "картой" not in logged


async def test_pipeline_timeout_is_logged(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker.analyzer.failures = [LLMTimeoutError("нет ответа")]
    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])
    caplog.set_level(logging.INFO, logger=v2.logger.name)

    assert await v2.analyze_document(job_id, source_ids, 0) == v2.RetryLater(countdown=30)

    [record] = _call_records(caplog)
    assert record.__dict__["outcome"] == "timeout"
    assert "suggestions" not in record.__dict__


async def test_success_stores_source_snapshots_and_serves_their_text(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.source_snapshot import JobSourceStatus, unpack_fingerprints
    from app.infrastructure.db.repositories.source_snapshot_repository import (
        SourceSnapshotRepository,
    )

    headers, project_id, _, job_id, source_ids = await _start(client, queue, ["A", "B", "C"])
    worker.connector.texts = {"A": "Одинаковый текст", "B": "Одинаковый текст"}
    worker.connector.broken = {"C"}

    await v2.analyze_document(job_id, source_ids, 0)

    async with sessionmaker() as session:
        rows = await SourceSnapshotRepository(session).list_job_sources(uuid.UUID(job_id))
    assert [row.position for row in rows] == [0, 1, 2]
    by_name = {row.name: row for row in rows}
    assert by_name["C"].status is JobSourceStatus.FAILED and by_name["C"].snapshot is None
    snapshot_a, snapshot_b = by_name["A"].snapshot, by_name["B"].snapshot
    assert snapshot_a is not None and snapshot_b is not None
    # Одинаковый текст двух источников хранится одним снимком.
    assert snapshot_a.id == snapshot_b.id
    assert worker.uploads[snapshot_a.storage_key] == "Одинаковый текст".encode()
    fp_key = snapshot_a.storage_key.removesuffix(".txt") + ".fp"
    assert len(unpack_fingerprints(worker.uploads[fp_key])) == snapshot_a.segments == 1

    response = await client.get(
        f"/api/v1/projects/{project_id}/source-snapshots/{snapshot_a.id}/content",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "Одинаковый текст"
    assert response.json()["chars"] == len("Одинаковый текст")

    other_project = await create_project(client, headers)
    response = await client.get(
        f"/api/v1/projects/{other_project}/source-snapshots/{snapshot_a.id}/content",
        headers=headers,
    )
    assert response.status_code == 404


async def test_deleting_document_removes_unreferenced_snapshots(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from app.infrastructure.db.models.source_snapshot import SourceSnapshotModel

    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])
    await v2.analyze_document(job_id, source_ids, 0)
    async with sessionmaker() as session:
        [snapshot] = (await session.execute(select(SourceSnapshotModel))).scalars().all()
    assert snapshot.storage_key in worker.uploads

    response = await client.delete(
        f"/api/v1/projects/{project_id}/documents/{document_id}", headers=headers
    )
    assert response.status_code == 204, response.text

    async with sessionmaker() as session:
        assert (await session.execute(select(SourceSnapshotModel))).scalars().all() == []
    assert snapshot.storage_key not in worker.uploads


async def test_retry_reuses_snapshot_objects_and_writes_rows_once(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
    file_storage: InMemoryFileStorage,
) -> None:
    from app.infrastructure.db.models.source_snapshot import (
        AnalysisJobSourceSnapshot,
        SourceSnapshotModel,
    )

    _, _, _, job_id, source_ids = await _start(client, queue, ["A"])
    worker.analyzer.failures = [LLMTimeoutError("таймаут")]
    uploads: list[str] = []
    original_upload = file_storage.upload

    async def counting_upload(key: str, content: object) -> None:
        uploads.append(key)
        await original_upload(key, content)  # type: ignore[arg-type]

    file_storage.upload = counting_upload  # type: ignore[method-assign]

    assert isinstance(await v2.analyze_document(job_id, source_ids, 0), v2.RetryLater)
    assert await v2.analyze_document(job_id, source_ids, 1) is None

    assert len([key for key in uploads if key.startswith("source_snapshots/")]) == 2
    async with sessionmaker() as session:
        assert len((await session.execute(select(SourceSnapshotModel))).scalars().all()) == 1
        rows = (await session.execute(select(AnalysisJobSourceSnapshot))).scalars().all()
    assert [(row.position, row.status) for row in rows] == [(0, "fetched")]


def _quoted_deadline(request: AnalysisRequest) -> AnalysisResult:
    from app.domain.evidence import Quote

    return AnalysisResult(
        status=AnalysisStatus.COMPLETED,
        suggestions=(
            ProposedChange(
                kind="modify",
                block_ref=_block_ref(request, "10 рабочих"),
                old_text="10 рабочих дней",
                new_text="5 рабочих дней",
                rationale="Источник: «срок ответа — 5 рабочих дней» (A)",
                quotes=(
                    Quote("A", "срок ответа:  5 рабочих дней"),
                    Quote("A", "этой фразы в источнике нет совсем"),
                ),
            ),
        ),
    )


async def test_suggestion_cites_only_quoted_source_and_shows_evidence(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A", "B"])
    worker.analyzer.respond = _quoted_deadline
    worker.connector.texts = {"A": "# Решения\n\nСрок ответа: 5 рабочих дней.", "B": "Другое"}

    await v2.analyze_document(job_id, source_ids, 0)

    [suggestion] = await _suggestions(sessionmaker)
    assert [s.name for s in suggestion.sources] == ["A"]
    job = await _job(sessionmaker, job_id)
    assert job.pipeline_meta["evidence"] == {
        "evidence_exact": 1,
        "evidence_approximate": 0,
        "evidence_not_found": 1,
        "evidence_unparsed": 0,
    }

    response = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document_id}/suggestions", headers=headers
    )
    assert response.status_code == 200, response.text
    found, missing = response.json()["items"][0]["evidence"]
    assert (found["source_name"], found["match"]) == ("A", "exact")
    snapshot = await client.get(
        f"/api/v1/projects/{project_id}/source-snapshots/{found['snapshot_id']}/content",
        headers=headers,
    )
    text = snapshot.json()["text"]
    assert text[found["start_offset"] : found["end_offset"]] == "Срок ответа: 5 рабочих дней"
    assert found["segment_index"] == 1
    # Заметка не файл .docx, но её storage_key не менялся: оригинал тот же.
    assert found["original_file_available"] is True
    assert (missing["match"], missing["start_offset"]) == ("not_found", None)
    assert "file_storage_key" not in found
