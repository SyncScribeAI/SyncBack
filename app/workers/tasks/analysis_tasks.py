"""Celery-задачи пайплайна анализа документа.

Chord: process_source_for_analysis_job для каждого источника → finalize_analysis_job
с результатами всех источников. Каждая задача выполняется через asyncio.run()
(воркер должен работать с пулом prefork, см. celery_app.py), поэтому все
подключения (БД, Redis) создаются внутри запуска и закрываются при выходе.

Обработка источника идемпотентна по (job_id, source_id): правки и отметка
«completed» пишутся в одной транзакции, повторная доставка не вызывает LLM или
не дублирует правки.

Перед вызовом LLM источник захватывается (claim: processing + lease + токен
владельца), чтобы конкурентный воркер не платил за второй запрос, а воркер,
у которого источник перехватили по истечении lease, не мог записать результат. Проигравший получает статус busy
и повторяет задачу через Celery; таймаут LLM повторяется с backoff. Ожидание занятого
источника и повторы LLM считаются раздельно (busy_attempts / llm_attempts в kwargs
задачи), а не общим self.request.retries.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from celery import Task
from celery.exceptions import MaxRetriesExceededError
from redis.asyncio import Redis

from app.core.config import get_settings
from app.domain.exceptions import (
    DocumentParseError,
    LLMInputTooLargeError,
    LLMInvalidResponseError,
    LLMTimeoutError,
)
from app.domain.interfaces.event_publisher import IEventPublisher
from app.domain.interfaces.llm_client import LLMClient
from app.domain.interfaces.source_connector import SourceConnector, SourceKind, SourceRef
from app.domain.lifecycle import AnalysisJobLifecycle, DocumentLifecycle
from app.domain.services.document_events import DocumentEventOutbox
from app.domain.value_objects import AnalysisJobStatusVO, SuggestionStatusVO
from app.infrastructure.db.models.source import Source
from app.infrastructure.db.session import isolated_uow
from app.infrastructure.events.publishers import RedisEventPublisher
from app.infrastructure.llm.factory import get_llm_client
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.source_connectors.manual_upload_connector import ManualUploadConnector
from app.infrastructure.source_connectors.url_connector import UrlConnector
from app.infrastructure.storage.s3_storage import S3FileStorage
from app.workers.celery_app import celery_app
from app.workers.pipeline.suggestion_mapper import map_to_suggestions

logger = logging.getLogger("syncscribe.workers.analysis")

_PARSED_DOC_KEY_PREFIX = "parsed_doc:"
_PARSED_DOC_TTL_SECONDS = 3600

_SOURCE_LEASE_SECONDS = 600
_TASK_SOFT_TIME_LIMIT_SECONDS = 540
_TASK_TIME_LIMIT_SECONDS = 570
_BUSY_RETRY_COUNTDOWN_SECONDS = 30
_BUSY_RETRY_LIMIT = 20
_LLM_RETRY_LIMIT = 3
_LLM_RETRY_BACKOFF_SECONDS = 30
_TASK_MAX_RETRIES = _BUSY_RETRY_LIMIT + _LLM_RETRY_LIMIT

if _SOURCE_LEASE_SECONDS <= _TASK_TIME_LIMIT_SECONDS:  # pragma: no cover
    raise RuntimeError("_SOURCE_LEASE_SECONDS должен быть больше _TASK_TIME_LIMIT_SECONDS")

uow_factory = isolated_uow


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@functools.cache
def _get_storage() -> S3FileStorage:
    return S3FileStorage()


@functools.cache
def _get_parser_registry() -> DocumentParserRegistry:
    return DocumentParserRegistry()


@functools.cache
def _get_manual_connector() -> ManualUploadConnector:
    return ManualUploadConnector(_get_storage(), _get_parser_registry())


@functools.cache
def _get_url_connector() -> UrlConnector:
    return UrlConnector()


def _get_connector_for(source_kind: SourceKind) -> SourceConnector:
    if source_kind == SourceKind.FILE:
        return _get_manual_connector()
    if source_kind == SourceKind.URL:
        return _get_url_connector()
    raise ValueError(f"Неизвестный SourceKind: {source_kind}")


@functools.cache
def _get_llm_client() -> LLMClient:
    return get_llm_client()


def _event_publisher() -> IEventPublisher:
    settings = get_settings()
    return RedisEventPublisher(settings.redis_url, settings.redis_sse_channel)


@asynccontextmanager
async def _redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def _build_source_ref(source: Source) -> SourceRef:
    kind_map = {"file": SourceKind.FILE, "url": SourceKind.URL}
    source_kind = kind_map.get(source.type.value)
    if source_kind is None:
        raise ValueError(f"Неизвестный тип источника в БД: {source.type.value}")
    return SourceRef(
        id=source.id,
        name=source.name,
        type=source_kind,
        storage_key=source.storage_key,
        url=source.url,
        uploaded_at=source.created_at,
    )


async def _get_cached_plain_text(job_id: uuid.UUID) -> str | None:
    try:
        async with _redis() as redis:
            cached: str | None = await redis.get(f"{_PARSED_DOC_KEY_PREFIX}{job_id}")
            return cached
    except Exception:
        logger.warning("Redis недоступен, документ будет распарсен заново", exc_info=True)
        return None


async def _cache_plain_text(job_id: uuid.UUID, plain_text: str) -> None:
    try:
        async with _redis() as redis:
            await redis.set(
                f"{_PARSED_DOC_KEY_PREFIX}{job_id}", plain_text, ex=_PARSED_DOC_TTL_SECONDS
            )
    except Exception:
        logger.warning("Redis недоступен, текст документа не закэширован", exc_info=True)


async def _load_document_text(job_id: uuid.UUID, storage_key: str) -> str:
    plain_text = await _get_cached_plain_text(job_id)
    if plain_text is not None:
        return plain_text
    try:
        raw_bytes = await _get_storage().download(storage_key)
        plain_text = _get_parser_registry().parse_by_filename(storage_key, raw_bytes).plain_text
    except Exception as exc:
        raise DocumentParseError(
            f"Не удалось распарсить документ (storage_key={storage_key}): {exc}"
        ) from exc
    await _cache_plain_text(job_id, plain_text)
    return plain_text


_GENERATION_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (LLMInputTooLargeError, "LLM_INPUT_TOO_LARGE"),
    (LLMTimeoutError, "LLM_UNAVAILABLE"),
    (LLMInvalidResponseError, "LLM_INVALID_RESPONSE"),
    (DocumentParseError, "DOCUMENT_PARSE_ERROR"),
)


def _generation_error_code(exc: Exception) -> str:
    for error_type, code in _GENERATION_ERROR_CODES:
        if isinstance(exc, error_type):
            return code
    return "GENERATION_ERROR"


def _failed(source_id: str, error_code: str, error_message: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "status": "failed",
        "error_code": error_code,
        "error_message": error_message,
    }


def _retryable(source_id: str, error_code: str, error_message: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "status": "retryable",
        "error_code": error_code,
        "error_message": error_message,
    }


async def _release_source(job_uuid: uuid.UUID, source_uuid: uuid.UUID, token: uuid.UUID) -> None:
    try:
        async with uow_factory() as uow:
            await uow.job_sources.release(job_uuid, source_uuid, token)
            await uow.commit()
    except Exception:
        logger.warning(
            "Не удалось снять захват источника, он освободится по истечении lease",
            extra={"job_id": str(job_uuid), "source_id": str(source_uuid)},
            exc_info=True,
        )


async def _process_source(job_id: str, source_id: str) -> dict[str, Any]:
    job_uuid = uuid.UUID(job_id)
    source_uuid = uuid.UUID(source_id)
    async with uow_factory() as uow:
        job = await uow.jobs.get_by_id(job_uuid)
        if job is None:
            logger.error("process_source: job не найден", extra={"job_id": job_id})
            return _failed(source_id, "JOB_NOT_FOUND", "Задача анализа не найдена")
        if not await uow.jobs.mark_processing_if_active(job_uuid):
            return {"source_id": source_id, "status": "cancelled"}
        await uow.commit()

        source = await uow.sources.get_by_id(source_uuid)
        if source is None:
            return _failed(source_id, "SOURCE_NOT_FOUND", "Источник не найден")
        document = await uow.documents.get_by_id(job.document_id)
        if document is None:
            return _failed(source_id, "DOCUMENT_NOT_FOUND", "Документ не найден")

        run = await uow.job_sources.ensure(job_uuid, source_uuid)
        already_done = await uow.job_sources.get_completed_count(job_uuid, source_uuid)
        token: uuid.UUID | None = None
        if already_done is None:
            token = await uow.job_sources.claim(job_uuid, source_uuid, _SOURCE_LEASE_SECONDS)
        await uow.commit()
        if already_done is not None:
            logger.info(
                "process_source: источник уже обработан, повтор пропущен",
                extra={"job_id": job_id, "source_id": source_id, "run_id": str(run.id)},
            )
            return {"source_id": source_id, "status": "ok", "count": already_done}
        if token is None:
            logger.info(
                "process_source: источник захвачен другим воркером",
                extra={"job_id": job_id, "source_id": source_id, "run_id": str(run.id)},
            )
            return {"source_id": source_id, "status": "busy"}

        source_ref = _build_source_ref(source)
        document_id = document.id
        storage_key = document.storage_key
        document_format = document.format.value

    try:
        plain_text = await _load_document_text(job_uuid, storage_key)
        source_text = await _get_connector_for(source_ref.type).fetch(source_ref)
        batch = await _get_llm_client().generate_suggestions(
            plain_text, source_text, document_format
        )
    except Exception as exc:
        logger.exception(
            "Ошибка генерации правок для источника",
            extra={"job_id": job_id, "source_id": source_id},
        )
        await _release_source(job_uuid, source_uuid, token)
        error_code = _generation_error_code(exc)
        if isinstance(exc, LLMTimeoutError):
            return _retryable(source_id, error_code, str(exc))
        return _failed(source_id, error_code, str(exc))

    suggestions = map_to_suggestions(
        batch,
        job_uuid,
        document_id,
        document_text=plain_text,
        document_format=document_format,
        source_id=source_uuid,
    )

    async with uow_factory() as uow:
        job = await uow.jobs.get_by_id(job_uuid)
        if job is None or not AnalysisJobLifecycle.is_active(job.status):
            await uow.job_sources.release(job_uuid, source_uuid, token)
            await uow.commit()
            return {"source_id": source_id, "status": "cancelled"}
        won = await uow.job_sources.mark_completed(job_uuid, source_uuid, token, len(suggestions))
        if not won:
            await uow.rollback()
            stored = await uow.job_sources.get_completed_count(job_uuid, source_uuid)
            if stored is not None:
                return {"source_id": source_id, "status": "ok", "count": stored}
            logger.warning(
                "process_source: захват источника перехвачен, результат отброшен",
                extra={"job_id": job_id, "source_id": source_id},
            )
            return {"source_id": source_id, "status": "busy"}
        provenance_source = await uow.sources.get_by_id(source_uuid)
        if provenance_source is None:
            await uow.rollback()
            return _failed(source_id, "SOURCE_NOT_FOUND", "Источник не найден")
        for suggestion in suggestions:
            suggestion.sources = [provenance_source]
        await uow.suggestions.bulk_create(suggestions)
        await uow.commit()

    return {"source_id": source_id, "status": "ok", "count": len(suggestions)}


async def _finalize(results: list[dict[str, Any]], job_id: str) -> None:
    failed = [r for r in results if r.get("status") == "failed"]
    cancelled = [r for r in results if r.get("status") == "cancelled"]
    succeeded = [r for r in results if r.get("status") == "ok"]

    async with uow_factory() as uow:
        job = await uow.jobs.get_by_id(uuid.UUID(job_id))
        if job is None:
            logger.error("finalize: job не найден", extra={"job_id": job_id})
            return
        if AnalysisJobLifecycle.is_terminal(job.status):
            logger.info(
                "finalize: задача уже завершена",
                extra={"job_id": job_id, "status": job.status.value},
            )
            return

        document = await uow.documents.get_by_id(job.document_id)
        if document is None:
            logger.error("finalize: документ не найден", extra={"job_id": job_id})
            return

        error_code: str | None = None
        error_message: str | None = None
        if cancelled:
            job_status = AnalysisJobStatusVO.CANCELLED
            error_code, error_message = "ANALYSIS_CANCELLED", "Анализ отменён"
        elif failed and not succeeded:
            job_status = AnalysisJobStatusVO.FAILED
            error_code = failed[0].get("error_code") or "GENERATION_ERROR"
            error_message = _join_errors(failed)
        elif failed:
            job_status = AnalysisJobStatusVO.PARTIAL_SUCCESS
            error_code, error_message = "PARTIAL_FAILURE", _join_errors(failed)
        else:
            job_status = AnalysisJobStatusVO.SUCCESS

        pending = await uow.suggestions.count_by_analysis_job_and_status(
            job.id, SuggestionStatusVO.PENDING
        )
        job = await uow.jobs.update_status(
            job, AnalysisJobLifecycle.transition(job.status, job_status), error_code, error_message
        )
        job.partial_success = job_status is AnalysisJobStatusVO.PARTIAL_SUCCESS
        outbox = DocumentEventOutbox(_event_publisher())
        if document.current_analysis_job_id == job.id:
            doc_status = AnalysisJobLifecycle.document_status_for(
                job_status, has_pending_suggestions=pending > 0
            )
            document = await uow.documents.update_status(
                document, DocumentLifecycle.transition(document.status, doc_status)
            )
            outbox.record(document)
        await uow.commit()
        await outbox.flush(uow)


def _join_errors(failed: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{r.get('source_id')}: {r.get('error_message') or r.get('error_code')}" for r in failed
    )[:2000]


def _retry_later(
    task: Task,
    job_id: str,
    source_id: str,
    *,
    countdown: int,
    busy_attempts: int,
    llm_attempts: int,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    try:
        raise task.retry(
            args=(),
            kwargs={
                "job_id": job_id,
                "source_id": source_id,
                "busy_attempts": busy_attempts,
                "llm_attempts": llm_attempts,
            },
            countdown=countdown,
        )
    except MaxRetriesExceededError:
        return fallback


@celery_app.task(
    name="process_source_for_analysis_job",
    bind=True,
    max_retries=_TASK_MAX_RETRIES,
    soft_time_limit=_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=_TASK_TIME_LIMIT_SECONDS,
)
def process_source_for_analysis_job(
    self: Task,
    job_id: str,
    source_id: str,
    busy_attempts: int = 0,
    llm_attempts: int = 0,
) -> dict[str, Any]:
    result: dict[str, Any] = _run_async(_process_source(job_id, source_id))
    status = result.get("status")

    if status == "busy":
        busy_failed = _failed(
            source_id, "SOURCE_BUSY", "Источник занят другим воркером дольше допустимого"
        )
        if busy_attempts >= _BUSY_RETRY_LIMIT:
            return busy_failed
        return _retry_later(
            self,
            job_id,
            source_id,
            countdown=_BUSY_RETRY_COUNTDOWN_SECONDS,
            busy_attempts=busy_attempts + 1,
            llm_attempts=llm_attempts,
            fallback=busy_failed,
        )

    if status == "retryable":
        llm_failed = _failed(
            source_id,
            result.get("error_code") or "LLM_UNAVAILABLE",
            result.get("error_message") or "LLM недоступен",
        )
        if llm_attempts >= _LLM_RETRY_LIMIT:
            return llm_failed
        return _retry_later(
            self,
            job_id,
            source_id,
            countdown=_LLM_RETRY_BACKOFF_SECONDS * (2**llm_attempts),
            busy_attempts=busy_attempts,
            llm_attempts=llm_attempts + 1,
            fallback=llm_failed,
        )

    return result


@celery_app.task(name="finalize_analysis_job", bind=True)
def finalize_analysis_job(self: Task, results: list[dict[str, Any]], job_id: str) -> None:
    _run_async(_finalize(results, job_id))
