"""Задача анализа документа по контракту ML 2.0 (ANALYSIS_PROVIDER ≠ legacy_llm).

Одна задача на документ вместо chord по источникам: документ разбирается один раз, все
источники уходят в один запрос к DocumentAnalyzer, ответ разбирается одним проходом и
пишется одним bulk_create.

Шаги: задача → processing; блоки документа из файла; тексты источников параллельно (не
больше ANALYSIS_SOURCE_FETCH_CONCURRENCY одновременно); лимиты запроса; вызов адаптера;
доменный маппер; запись правок и статусов.

Повторная доставка и отмена. Результат пишется под блокировкой строки задачи
(lock_if_active) только пока она активна: вторая доставка той же задачи или отмена
пользователем во время ожидания конвейера ничего не запишут. Лишний вызов конвейера при
повторной доставке возможен, лишних правок — нет.

Если конвейер ответил, сводка вызова (request_id, meta, счётчики блоков) пишется в задачу
той же транзакцией, что и результат, — в том числе при отказе: по request_id отказ ищут в
логах harness.

Повторы. Таймаут адаптера и временный сбой конвейера (pipeline_error) повторяются до
ANALYSIS_RETRY_LIMIT раз с экспоненциальной паузой; номер попытки передаётся в kwargs и
входит в request_id. Ошибка нашего запроса (invalid_request), некорректный ответ и
превышение лимитов не повторяются — повтор дал бы тот же результат.

Время. soft_time_limit = ANALYSIS_TASK_TIME_LIMIT_SECONDS, а ML_WAIT_SECONDS по настройкам
строго меньше: адаптер сам вернёт таймаут раньше, чем Celery снимет задачу. Если лимит всё
же сработал (например, на загрузке источников), задача завершается failed.

Логи — только идентификаторы, коды и счётчики: тексты документов и источников в них не
попадают. Полный запрос и разобранный ответ сохраняются в S3 только при
ANALYSIS_STORE_PAYLOADS.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any

import pydantic_core
from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.core.config import get_settings
from app.domain.exceptions import (
    LLMInputTooLargeError,
    LLMInvalidResponseError,
    LLMTimeoutError,
)
from app.domain.interfaces.document_analyzer import (
    AnalysisRequest,
    AnalysisResult,
    DocumentAnalyzer,
    SourcePayload,
)
from app.domain.interfaces.file_storage import UploadContent
from app.domain.interfaces.source_connector import SourceRef
from app.domain.lifecycle import AnalysisJobLifecycle
from app.domain.pipeline_run import PipelineRunSummary, summarize_run
from app.domain.services.analysis_request_builder import DocumentInput, build_analysis_request
from app.domain.services.analysis_result_mapper import SuggestionDraft, map_result
from app.domain.source_text import ensure_source_fits
from app.domain.value_objects import AnalysisJobStatusVO
from app.infrastructure.db.session import isolated_uow
from app.infrastructure.ml_pipeline.factory import analysis_limits, get_document_analyzer
from app.infrastructure.ml_pipeline.schemas import CONTRACT_VERSION, encode_request
from app.workers.celery_app import celery_app
from app.workers.pipeline.suggestion_drafts import to_suggestions
from app.workers.tasks.job_completion import complete_job
from app.workers.tasks.runtime import (
    build_source_ref,
    event_publisher,
    get_connector_for,
    get_parser_registry,
    get_storage,
)

logger = logging.getLogger("syncscribe.workers.analysis_v2")

uow_factory = isolated_uow

_settings = get_settings()
_RETRY_BACKOFF_SECONDS = 30
# Запас между мягким и жёстким лимитом: за него задача успевает записать failed.
_HARD_LIMIT_GRACE_SECONDS = 30
_PAYLOAD_PREFIX = "analysis_runs"


@functools.cache
def _get_analyzer() -> DocumentAnalyzer:
    # Один раз на процесс: плагин импортируется при первой задаче, а не на каждую.
    return get_document_analyzer()


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@dataclass(frozen=True, slots=True)
class RetryLater:
    """Попытка не удалась временно; задачу нужно повторить через countdown секунд."""

    countdown: int


class _JobFailed(Exception):
    """Терминальная ошибка шага: задача завершается failed с этим кодом.

    run — сводка вызова ML, если ответ был: request_id нужен и для разбора отказа.
    """

    def __init__(self, code: str, message: str, run: PipelineRunSummary | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run = run


@dataclass(frozen=True, slots=True)
class _StartedJob:
    id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    document_format: str
    storage_key: str
    sources: tuple[SourceRef, ...]
    # Источники, которые не удалось даже описать (удалены, неизвестный тип).
    unavailable: tuple[str, ...]


@dataclass(slots=True)
class _FetchedSources:
    payloads: list[SourcePayload] = field(default_factory=list)
    ids: list[uuid.UUID] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


async def analyze_document(
    job_id: str, source_ids: Sequence[str], attempt: int
) -> RetryLater | None:
    """Одна попытка анализа. None — задача завершена (или уже не активна)."""
    job_uuid = uuid.UUID(job_id)
    job = await _start(job_uuid, source_ids, attempt)
    if job is None:
        return None
    try:
        return await _run_attempt(job, attempt)
    except _JobFailed as failure:
        logger.warning(
            "Анализ v2 завершён ошибкой",
            extra={"job_id": job_id, "attempt": attempt, "error_code": failure.code},
        )
        await finish(
            job_uuid, AnalysisJobStatusVO.FAILED, failure.code, failure.message, run=failure.run
        )
        return None


async def _run_attempt(job: _StartedJob, attempt: int) -> RetryLater | None:
    if not job.sources and not job.unavailable:
        # Сравнивать не с чем — как и прежний конвейер без источников, без вызова ML.
        await finish(job.id, AnalysisJobStatusVO.SUCCESS)
        return None
    document = await _load_document(job)
    fetched = await _fetch_sources(job.sources)
    fetched.failed[:0] = job.unavailable
    if not fetched.payloads:
        raise _JobFailed(
            "SOURCES_UNAVAILABLE", "Недоступны все источники: " + ", ".join(fetched.failed)
        )
    request = _build_request(job, attempt, document, fetched.payloads)
    # Документ больше не нужен: тексты блоков уже в запросе.
    del document
    await _store_payload(job.id, attempt, "request", lambda: encode_request(request))

    try:
        result = await _get_analyzer().analyze(request)
    except LLMTimeoutError as exc:
        return await _retry_or_fail(job.id, attempt, str(exc))
    except LLMInvalidResponseError as exc:
        raise _JobFailed("LLM_INVALID_RESPONSE", str(exc)) from exc
    await _store_payload(job.id, attempt, "response", lambda: pydantic_core.to_json(result))
    run = _summarize(job.id, request, result)

    outcome = AnalysisJobLifecycle.outcome_for(result, fetched.failed)
    if outcome.retryable:
        return await _retry_or_fail(job.id, attempt, outcome.error_message or "", run)
    if outcome.status is AnalysisJobStatusVO.FAILED:
        raise _JobFailed(outcome.error_code or "LLM_UNAVAILABLE", outcome.error_message or "", run)

    drafts = _map(job.id, attempt, result, request)
    await finish(
        job.id,
        outcome.status,
        outcome.error_code,
        outcome.error_message,
        drafts=drafts,
        source_ids=fetched.ids,
        run=run,
    )
    return None


async def _start(job_id: uuid.UUID, source_ids: Sequence[str], attempt: int) -> _StartedJob | None:
    async with uow_factory() as uow:
        job = await uow.jobs.get_by_id(job_id)
        if job is None:
            logger.error("Анализ v2: задача не найдена", extra={"job_id": str(job_id)})
            return None
        if not await uow.jobs.mark_processing_if_active(job_id):
            logger.info(
                "Анализ v2: задача уже завершена или отменена",
                extra={"job_id": str(job_id), "attempt": attempt},
            )
            return None
        if attempt:
            job.retry_count = attempt
        document = await uow.documents.get_by_id(job.document_id)
        if document is None:
            logger.error("Анализ v2: документ не найден", extra={"job_id": str(job_id)})
            return None
        wanted = [uuid.UUID(source_id) for source_id in source_ids]
        found = {source.id: source for source in await uow.sources.get_many_by_ids(wanted)}
        refs: list[SourceRef] = []
        unavailable: list[str] = []
        for source_id in wanted:
            source = found.get(source_id)
            if source is None:
                unavailable.append(str(source_id))
                continue
            try:
                refs.append(build_source_ref(source))
            except ValueError:
                unavailable.append(source.name)
        started = _StartedJob(
            id=job_id,
            document_id=document.id,
            document_name=document.name,
            document_format=document.format.value,
            storage_key=document.storage_key,
            sources=tuple(refs),
            unavailable=tuple(unavailable),
        )
        await uow.commit()
    return started


async def _load_document(job: _StartedJob) -> DocumentInput:
    """Блоки строятся из файла на каждую попытку: одна задача на документ, разбор — один."""
    try:
        raw = await get_storage().download(job.storage_key)
        parsed = get_parser_registry().parse_by_filename(job.storage_key, raw)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        raise _JobFailed(
            "DOCUMENT_PARSE_ERROR", f"Не удалось разобрать документ: {type(exc).__name__}"
        ) from exc
    return DocumentInput(
        id=job.document_id,
        name=job.document_name,
        format=job.document_format,
        plain_text=parsed.plain_text,
        blocks=parsed.blocks,
    )


async def _fetch_sources(refs: Sequence[SourceRef]) -> _FetchedSources:
    """Тексты источников; недоступный источник не роняет задачу, а делает её частичной.

    Слишком длинный источник — ошибка всей задачи: без него правки были бы ложными, а
    обрезать его молча нельзя (см. ensure_source_fits).
    """
    semaphore = asyncio.Semaphore(_settings.analysis_source_fetch_concurrency)
    max_chars = _settings.analysis_max_source_chars

    async def fetch(ref: SourceRef) -> str:
        async with semaphore:
            text = await get_connector_for(ref.type).fetch_structured(ref)
        ensure_source_fits(ref.name, text, max_chars)
        return text

    results = await asyncio.gather(*(fetch(ref) for ref in refs), return_exceptions=True)
    fetched = _FetchedSources()
    for ref, result in zip(refs, results, strict=True):
        if isinstance(result, SoftTimeLimitExceeded):
            raise result
        if isinstance(result, LLMInputTooLargeError):
            raise _JobFailed("LLM_INPUT_TOO_LARGE", str(result)) from result
        if isinstance(result, BaseException):
            logger.warning(
                "Анализ v2: источник недоступен",
                extra={"source_id": str(ref.id), "error_type": type(result).__name__},
            )
            fetched.failed.append(ref.name)
            continue
        fetched.payloads.append(
            SourcePayload(id=str(ref.id), name=ref.name, type=ref.type, text=result)
        )
        fetched.ids.append(ref.id)
    return fetched


def _build_request(
    job: _StartedJob, attempt: int, document: DocumentInput, sources: Sequence[SourcePayload]
) -> AnalysisRequest:
    try:
        return build_analysis_request(
            job_id=job.id,
            attempt=attempt,
            document=document,
            sources=sources,
            limits=analysis_limits(_settings),
        )
    except LLMInputTooLargeError as exc:
        raise _JobFailed("LLM_INPUT_TOO_LARGE", str(exc)) from exc


def _map(
    job_id: uuid.UUID, attempt: int, result: AnalysisResult, request: AnalysisRequest
) -> list[SuggestionDraft]:
    mapped = map_result(result, request.document.blocks)
    if mapped.dropped:
        logger.warning(
            "Анализ v2: часть правок отброшена",
            extra={"job_id": str(job_id), "attempt": attempt, "dropped": mapped.dropped},
        )
    if mapped.flags:
        # Хранение флагов — отдельный пакет (B7); до него — только счётчик в логе.
        logger.info(
            "Анализ v2: флаги ручной проверки не сохранены",
            extra={"job_id": str(job_id), "flags": len(mapped.flags)},
        )
    return mapped.suggestions


def _summarize(
    job_id: uuid.UUID, request: AnalysisRequest, result: AnalysisResult
) -> PipelineRunSummary:
    run = summarize_run(
        result,
        request_id=request.request_id,
        contract_version=CONTRACT_VERSION,
        provider=_settings.analysis_provider,
    )
    if run.meta_dropped:
        logger.warning(
            "Анализ v2: meta конвейера слишком большая и не сохранена",
            extra={"job_id": str(job_id), "request_id": run.request_id},
        )
    return run


async def _retry_or_fail(
    job_id: uuid.UUID, attempt: int, message: str, run: PipelineRunSummary | None = None
) -> RetryLater | None:
    if attempt < _settings.analysis_retry_limit:
        countdown = _RETRY_BACKOFF_SECONDS * (2**attempt)
        logger.info(
            "Анализ v2: временный сбой конвейера, повтор",
            extra={"job_id": str(job_id), "attempt": attempt, "countdown": countdown},
        )
        return RetryLater(countdown)
    await finish(job_id, AnalysisJobStatusVO.FAILED, "LLM_UNAVAILABLE", message or None, run=run)
    return None


async def finish(
    job_id: uuid.UUID,
    status: AnalysisJobStatusVO,
    error_code: str | None = None,
    error_message: str | None = None,
    *,
    drafts: Sequence[SuggestionDraft] = (),
    source_ids: Sequence[uuid.UUID] = (),
    run: PipelineRunSummary | None = None,
) -> bool:
    """Записать результат одной транзакцией. False — задача уже не активна, ничего не записано."""
    async with uow_factory() as uow:
        job = await uow.jobs.lock_if_active(job_id)
        if job is None:
            logger.info(
                "Анализ v2: задача уже не активна, результат отброшен",
                extra={"job_id": str(job_id)},
            )
            return False
        document = await uow.documents.get_by_id(job.document_id)
        if document is None:
            return False
        if run is not None:
            await uow.jobs.record_pipeline_run(job, run)
        if drafts:
            sources = await uow.sources.get_many_by_ids(list(source_ids))
            await uow.suggestions.bulk_create(
                to_suggestions(drafts, job_id=job.id, document_id=document.id, sources=sources)
            )
        outbox = await complete_job(
            uow,
            job,
            document,
            status,
            error_code=error_code,
            error_message=error_message,
            publisher=event_publisher(),
        )
        await uow.commit()
        await outbox.flush(uow)
    logger.info(
        "Анализ v2 завершён",
        extra={"job_id": str(job_id), "status": status.value, "suggestions": len(drafts)},
    )
    return True


async def _store_payload(
    job_id: uuid.UUID, attempt: int, kind: str, encode: Callable[[], bytes]
) -> None:
    """Архив запроса/ответа для разбора качества; сбой архива не влияет на анализ."""
    if not _settings.analysis_store_payloads:
        return
    key = f"{_PAYLOAD_PREFIX}/{job_id}/{attempt}.{kind}.json"
    try:
        await get_storage().upload(key, UploadContent.from_bytes(encode(), "application/json"))
    except Exception:
        logger.warning(
            "Анализ v2: payload не сохранён", extra={"job_id": str(job_id), "kind": kind}
        )


@celery_app.task(
    name="analyze_document_for_job",
    bind=True,
    # Число попыток считает сама задача (attempt в kwargs), Celery его не ограничивает.
    max_retries=None,
    soft_time_limit=_settings.analysis_task_time_limit_seconds,
    time_limit=_settings.analysis_task_time_limit_seconds + _HARD_LIMIT_GRACE_SECONDS,
)
def analyze_document_for_job(
    self: Task, job_id: str, source_ids: list[str], attempt: int = 0
) -> None:
    try:
        step = _run_async(analyze_document(job_id, source_ids, attempt))
    except SoftTimeLimitExceeded:
        limit = _settings.analysis_task_time_limit_seconds
        logger.warning("Анализ v2: превышен лимит времени задачи", extra={"job_id": job_id})
        _finish_safely(job_id, "LLM_UNAVAILABLE", f"Анализ не уложился в {limit} с")
        return
    except Exception:
        # Без этого задача осталась бы в processing навсегда: chord-финализации здесь нет.
        logger.exception("Анализ v2: непредвиденная ошибка", extra={"job_id": job_id})
        _finish_safely(job_id, "GENERATION_ERROR", "Внутренняя ошибка анализа")
        return
    if step is not None:
        raise self.retry(
            args=(),
            kwargs={"job_id": job_id, "source_ids": source_ids, "attempt": attempt + 1},
            countdown=step.countdown,
        )


def _finish_safely(job_id: str, error_code: str, error_message: str) -> None:
    try:
        _run_async(finish(uuid.UUID(job_id), AnalysisJobStatusVO.FAILED, error_code, error_message))
    except Exception:
        logger.exception("Анализ v2: не удалось записать ошибку задачи", extra={"job_id": job_id})
