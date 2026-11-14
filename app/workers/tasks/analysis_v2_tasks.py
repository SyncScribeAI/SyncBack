"""Задача анализа документа по контракту ML 2.0 (ANALYSIS_PROVIDER ≠ legacy_llm).

Одна задача на документ вместо chord по источникам: документ разбирается один раз, все
источники уходят в один запрос к DocumentAnalyzer, ответ разбирается одним проходом и
пишется одним bulk_create вместе с флагами «проверьте вручную».

Шаги: задача → processing; блоки документа из файла; тексты источников параллельно (не
больше ANALYSIS_SOURCE_FETCH_CONCURRENCY одновременно); лимиты запроса; вызов адаптера;
доменный маппер; группы правок одной фразы (и догоняющие правки при
SUGGESTION_PROPAGATE_EXACT); запись правок, флагов и статусов.

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
import hashlib
import logging
import random
import time
import uuid
from collections.abc import Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import pydantic_core
from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.core.config import get_settings
from app.domain.analysis_reuse import (
    AnalysisStrategy,
    Baseline,
    DeltaFallback,
    DeltaPolicy,
    DeltaSummary,
    IncrementalMode,
    ReuseDecision,
    ReuseReason,
    RunInput,
    analysis_input_hash,
    decide_reuse,
    delta_precheck,
    sources_replaced_check,
)
from app.domain.document_geometry import highlight_span
from app.domain.evidence import EvidenceSource, evidence_sources_of
from app.domain.exceptions import (
    LLMInputTooLargeError,
    LLMInvalidResponseError,
    LLMTimeoutError,
)
from app.domain.interfaces.document_analyzer import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    DocumentAnalyzer,
    SourcePayload,
)
from app.domain.interfaces.file_storage import UploadContent
from app.domain.interfaces.source_connector import SourceRef
from app.domain.lifecycle import AnalysisJobLifecycle, JobOutcome
from app.domain.pipeline_run import PipelineRunSummary, summarize_run
from app.domain.services.analysis_request_builder import DocumentInput, build_analysis_request
from app.domain.services.analysis_result_mapper import FlagDraft, SuggestionDraft, map_result
from app.domain.services.delta_merge import MergePlan, audit_diff, plan_merge
from app.domain.services.delta_request import DeltaRequest, add_on_unchanged, build_delta_request
from app.domain.services.delta_selection import DeltaLimits
from app.domain.services.evidence_locator import (
    EvidenceLocator,
    LocatedSource,
    attach_evidence,
)
from app.domain.services.suggestion_groups import PropagationLimits, finalize_drafts
from app.domain.source_snapshot import (
    JobSourceRecord,
    JobSourceStatus,
    SnapshotDraft,
    SourceSegment,
    distinct_drafts,
    fingerprints_storage_key,
    pack_fingerprints,
    segment_fingerprints,
)
from app.domain.source_text import SourceLayout, ensure_source_fits
from app.domain.value_objects import AnalysisJobStatusVO, DocumentFormatVO
from app.infrastructure.db.session import isolated_uow
from app.infrastructure.ml_pipeline.factory import analysis_limits, get_document_analyzer
from app.infrastructure.ml_pipeline.schemas import CONTRACT_VERSION, encode_request
from app.workers.celery_app import celery_app
from app.workers.pipeline.flag_drafts import to_flags
from app.workers.pipeline.incremental import (
    delta_against,
    full_run_stats,
    load_baseline_fingerprints,
    reused_run_stats,
)
from app.workers.pipeline.suggestion_drafts import to_suggestions
from app.workers.tasks.job_completion import complete_job
from app.workers.tasks.runtime import (
    build_source_ref,
    event_publisher,
    get_connector_for,
    get_document_layout_reader,
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
    project_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    document_format: str
    storage_key: str
    sources: tuple[SourceRef, ...]
    # Источники, которые не удалось даже описать (удалены, неизвестный тип).
    unavailable: tuple[str, ...]
    unavailable_records: tuple[JobSourceRecord, ...] = ()
    strategy: str = AnalysisStrategy.AUTO.value


@dataclass(slots=True)
class _FetchedSources:
    payloads: list[SourcePayload] = field(default_factory=list)
    ids: list[uuid.UUID] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # Строки задачи в порядке источников запроса и снимки удачно загруженных текстов.
    records: list[JobSourceRecord] = field(default_factory=list)
    snapshots: list[SnapshotDraft] = field(default_factory=list)
    # Положение фрагментов в исходных файлах; параллельно payloads.
    layouts: list[SourceLayout] = field(default_factory=list)
    # Сегменты с отпечатками; параллельно snapshots, нужны differ без второго прохода.
    segments: list[list[tuple[SourceSegment, int]]] = field(default_factory=list)


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
    document, document_hash = await _load_document(job)
    fetched = await _fetch_sources(job.sources, job.project_id)
    fetched.failed[:0] = job.unavailable
    fetched.records[:0] = job.unavailable_records
    if not fetched.payloads:
        raise _JobFailed(
            "SOURCES_UNAVAILABLE", "Недоступны все источники: " + ", ".join(fetched.failed)
        )
    await _store_snapshots(job.id, fetched.snapshots)
    decision, input_hash = await _decide(job, document_hash, fetched)
    if decision.reuse and decision.baseline is not None:
        await _carry_over(
            job, decision, input_hash, document_hash, fetched, sum(1 for _ in document.blocks)
        )
        return None
    delta = await _plan_delta(job, decision, document, document_hash, fetched)
    delta_request = delta if isinstance(delta, DeltaRequest) else None
    fallback = delta if isinstance(delta, DeltaFallback) else None
    run_input = RunInput(
        input_hash=input_hash,
        contract_version=CONTRACT_VERSION,
        provider=_settings.analysis_provider,
        decision=decision,
        stats=reused_run_stats(fetched.segments, fetched.snapshots, 0),
        document_hash=document_hash,
        fallback=fallback,
    )
    if delta_request is not None and not delta_request.calls_model:
        # Источник только потерял текст: модели нечего показать, перенос сам отметит
        # правки, основание которых исчезло.
        blocks = {block.ref: block.text_in(document.plain_text) for block in document.blocks}
        await _finish_delta(job, delta_request, run_input, fetched, (), (), None, blocks)
        return None
    audit = delta_request is not None and random.random() < _settings.analysis_delta_audit_rate
    full_request = _build_request(job, attempt, document, fetched.payloads) if audit else None
    if delta_request is None:
        request = full_request or _build_request(job, attempt, document, fetched.payloads)
        run_input = replace(
            run_input,
            stats=full_run_stats(
                fetched.segments,
                fetched.snapshots,
                blocks=len(request.document.blocks),
                checked=sum(1 for block in request.document.blocks if block.editable),
            ),
            delta=await _delta(job, decision, fetched),
        )
    else:
        request = _build_request(
            job, attempt, document, delta_request.sources, delta_request.selected.refs
        )
        run_input = replace(
            run_input,
            stats=delta_request.stats,
            delta=delta_request.summary,
            selective=True,
            selection=delta_request.selected.as_meta(),
        )
    # Документ больше не нужен: тексты блоков уже в запросе.
    del document
    await _store_payload(job.id, attempt, "request", lambda: encode_request(request))

    started_at = time.perf_counter()
    try:
        result = await _get_analyzer().analyze(request)
    except LLMTimeoutError as exc:
        _log_call(job.id, attempt, request, started_at, "timeout")
        return await _retry_or_fail(job.id, attempt, str(exc))
    except LLMInvalidResponseError as exc:
        _log_call(job.id, attempt, request, started_at, "invalid_response")
        raise _JobFailed("LLM_INVALID_RESPONSE", str(exc)) from exc
    await _store_payload(job.id, attempt, "response", lambda: pydantic_core.to_json(result))
    run = _summarize(job.id, request, result)
    _log_call(job.id, attempt, request, started_at, result.status.value, result, run)

    outcome = AnalysisJobLifecycle.outcome_for(result, fetched.failed)
    if outcome.retryable:
        return await _retry_or_fail(job.id, attempt, outcome.error_message or "", run)
    if outcome.status is AnalysisJobStatusVO.FAILED:
        raise _JobFailed(outcome.error_code or "LLM_UNAVAILABLE", outcome.error_message or "", run)

    job_sources = _numbered(fetched.records)
    drafts, flags, run = await _located_result(job, attempt, result, request, fetched, run)
    if delta_request is not None:
        blocks = {block.ref: block.text for block in request.document.blocks}
        delta_blocks = await _finish_delta(
            job, delta_request, run_input, fetched, drafts, flags, run, blocks, outcome
        )
        if full_request is not None and delta_blocks is not None:
            await _audit(job, attempt, full_request, fetched, delta_blocks)
        return None
    await finish(
        job.id,
        outcome.status,
        outcome.error_code,
        outcome.error_message,
        drafts=drafts,
        flags=flags,
        source_ids=fetched.ids,
        run=run,
        snapshots=fetched.snapshots,
        job_sources=job_sources,
        run_input=run_input,
    )
    return None


async def _located_result(
    job: _StartedJob,
    attempt: int,
    result: AnalysisResult,
    request: AnalysisRequest,
    fetched: _FetchedSources,
    run: PipelineRunSummary,
) -> tuple[list[SuggestionDraft], list[FlagDraft], PipelineRunSummary]:
    """Черновики правок и флагов с основаниями и, у PDF, с геометрией на страницах."""
    drafts, flags = _map(job.id, attempt, result, request)
    drafts, flags, counters = attach_evidence(
        drafts,
        flags,
        EvidenceLocator(
            _located_sources(fetched, _numbered(fetched.records)),
            _settings.analysis_evidence_min_containment,
        ),
    )
    run = replace(run, evidence=counters.as_meta())
    logger.info("Анализ v2: основания правок", extra={"job_id": str(job.id), **counters.as_meta()})
    if job.document_format == DocumentFormatVO.PDF.value:
        drafts, flags = await _attach_geometry(job, drafts, flags)
    return drafts, flags, run


async def _plan_delta(
    job: _StartedJob,
    decision: ReuseDecision,
    document: DocumentInput,
    document_hash: str,
    fetched: _FetchedSources,
) -> DeltaRequest | DeltaFallback | None:
    """Выборочный запрос, причина полного анализа или None, если выбора не было.

    None означает, что полный анализ задан самим решением о переносе: нет baseline,
    strategy=full, режим off или упал источник.
    """
    baseline = decision.baseline
    if decision.reason is not ReuseReason.INPUT_CHANGED or baseline is None:
        return None
    policy = _delta_policy()
    fallback = delta_precheck(
        baseline,
        mode=IncrementalMode(_settings.analysis_incremental_mode),
        document_hash=document_hash,
        contract_version=CONTRACT_VERSION,
        provider=_settings.analysis_provider,
        policy=policy,
    )
    if fallback is None:
        planned = await _plan_from_baseline(job, baseline, document, fetched, policy)
        if isinstance(planned, DeltaRequest):
            return planned
        fallback = planned
    logger.info(
        "Анализ v2: выборочная проверка заменена полным анализом",
        extra={"job_id": str(job.id), "fallback_reason": fallback.value},
    )
    return fallback


async def _plan_from_baseline(
    job: _StartedJob,
    baseline: Baseline,
    document: DocumentInput,
    fetched: _FetchedSources,
    policy: DeltaPolicy,
) -> DeltaRequest | DeltaFallback:
    """Выборочный запрос против прошлой задачи или причина отказа от него."""
    async with uow_factory() as uow:
        baseline_sources = await uow.snapshots.list_job_sources(baseline.job_id)
        suggestions = await uow.suggestions.baseline_rows(baseline.job_id)
        flags = await uow.flags.baseline_rows(baseline.job_id)
    replaced = sources_replaced_check(
        (row.source_id for row in baseline_sources if row.source_id is not None),
        (uuid.UUID(payload.id) for payload in fetched.payloads),
    )
    if replaced is not None:
        return replaced
    fingerprints = await load_baseline_fingerprints(
        get_storage(), baseline_sources, fetched.snapshots, job.id
    )
    if fingerprints is None:
        return DeltaFallback.FINGERPRINTS_UNAVAILABLE
    planned = build_delta_request(
        baseline=baseline,
        fingerprints=fingerprints,
        document=document,
        payloads=fetched.payloads,
        segments=fetched.segments,
        suggestions=suggestions,
        flags=flags,
        policy=policy,
        limits=_delta_limits(),
    )
    if isinstance(planned, DeltaRequest):
        logger.info(
            "Анализ v2: выборочная проверка блоков",
            extra={
                "job_id": str(job.id),
                "baseline_job_id": str(baseline.job_id),
                **planned.selected.as_meta(),
                "segments_sent": planned.stats.segments_sent,
                "segments_total": planned.stats.segments_total,
            },
        )
    return planned


def _delta_policy() -> DeltaPolicy:
    return DeltaPolicy(
        max_changed_share=_settings.analysis_delta_max_changed_share,
        max_blocks_share=_settings.analysis_delta_max_blocks_share,
        max_chain=_settings.analysis_delta_max_chain,
    )


def _delta_limits() -> DeltaLimits:
    return DeltaLimits(
        blocks_per_segment=_settings.analysis_delta_blocks_per_segment,
        score_threshold=_settings.analysis_delta_score_threshold,
        rare_block_count=_settings.analysis_delta_rare_blocks,
        context_segments=_settings.analysis_delta_context_segments,
        support_segments=_settings.analysis_delta_support_segments,
        stem_chars=_settings.analysis_delta_stem_chars,
    )


async def _finish_delta(
    job: _StartedJob,
    delta: DeltaRequest,
    run_input: RunInput,
    fetched: _FetchedSources,
    drafts: Sequence[SuggestionDraft],
    flags: Sequence[FlagDraft],
    run: PipelineRunSummary | None,
    blocks: Mapping[str, str],
    outcome: JobOutcome | None = None,
) -> set[str] | None:
    """Свести ответ с прошлым результатом и записать задачу; блоки с правкой или флагом.

    None, если задача уже не активна и ничего не записано.
    """
    job_sources = _numbered(fetched.records)
    segments_by_position = {
        record.position: segments
        for record, segments in zip(
            (record for record in job_sources if record.status is JobSourceStatus.FETCHED),
            fetched.segments,
            strict=True,
        )
    }
    plan = plan_merge(
        baseline_suggestions=delta.suggestions,
        baseline_flags=delta.flags,
        rechecked=delta.rechecked,
        drafts=drafts,
        flags=flags,
        add_on_unchanged=add_on_unchanged(drafts, segments_by_position, delta.base),
        changed=delta.changed,
        blocks=blocks,
    )
    selection = {**delta.selected.as_meta(), **plan.as_meta()}
    written = await finish(
        job.id,
        outcome.status if outcome else AnalysisJobStatusVO.SUCCESS,
        outcome.error_code if outcome else None,
        outcome.error_message if outcome else None,
        drafts=plan.drafts,
        flags=plan.flags,
        source_ids=fetched.ids,
        run=run,
        snapshots=fetched.snapshots,
        job_sources=job_sources,
        run_input=replace(
            run_input,
            stats=delta.stats,
            delta=delta.summary,
            selective=True,
            selection=selection,
        ),
        carry_from=delta.baseline.job_id,
        merge=plan,
    )
    if not written:
        return None
    copied = set(plan.copy_suggestions)
    copied_flags = set(plan.copy_flags)
    found = {draft.block_ref for draft in plan.drafts} | {flag.block_ref for flag in plan.flags}
    found.update(row.block_ref for row in delta.suggestions if row.id in copied and row.block_ref)
    found.update(row.block_ref for row in delta.flags if row.id in copied_flags)
    return found


async def _audit(
    job: _StartedJob,
    attempt: int,
    request: AnalysisRequest,
    fetched: _FetchedSources,
    delta_blocks: set[str],
) -> None:
    """Контрольный полный анализ того же входа после выборочной задачи.

    Результат задачи уже записан и не меняется: расхождение по блокам пишется только в
    meta, по нему настраиваются пороги отбора. Сбой аудита на задачу не влияет.
    """
    try:
        result = await _get_analyzer().analyze(request)
        if result.status is AnalysisStatus.FAILED:
            return
        run = _summarize(job.id, request, result)
        drafts, flags, _ = await _located_result(job, attempt, result, request, fetched, run)
        audit = audit_diff(
            {draft.block_ref for draft in drafts} | {flag.block_ref for flag in flags},
            delta_blocks,
        )
        async with uow_factory() as uow:
            await uow.jobs.record_audit(job.id, audit)
            await uow.commit()
        logger.info("Анализ v2: аудит выборочной задачи", extra={"job_id": str(job.id), **audit})
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.warning(
            "Анализ v2: аудит выборочной задачи не выполнен",
            exc_info=True,
            extra={"job_id": str(job.id)},
        )


async def _decide(
    job: _StartedJob, document_hash: str, fetched: _FetchedSources
) -> tuple[ReuseDecision, str]:
    input_hash = analysis_input_hash(
        contract_version=CONTRACT_VERSION,
        provider=_settings.analysis_provider,
        document_hash=document_hash,
        sources=[
            (payload.id, payload.type.value, payload.name, snapshot.content_hash)
            for payload, snapshot in zip(fetched.payloads, fetched.snapshots, strict=True)
        ],
    )
    mode = IncrementalMode(_settings.analysis_incremental_mode)
    baseline = None
    if mode is not IncrementalMode.OFF:
        # Прошлая задача нужна и при strategy=full: по ней считается доля изменений.
        async with uow_factory() as uow:
            baseline = await uow.jobs.find_reuse_baseline(job.document_id, job.id)
    decision = decide_reuse(
        mode=mode,
        strategy=AnalysisStrategy(job.strategy),
        input_hash=input_hash,
        baseline=baseline,
        failed_sources=len(fetched.failed),
    )
    logger.info(
        "Анализ v2: решение о переносе результата",
        extra={
            "job_id": str(job.id),
            "reason": decision.reason.value,
            "baseline_job_id": str(baseline.job_id) if baseline else None,
        },
    )
    return decision, input_hash


async def _carry_over(
    job: _StartedJob,
    decision: ReuseDecision,
    input_hash: str,
    document_hash: str,
    fetched: _FetchedSources,
    blocks: int,
) -> None:
    """Вход совпал с прошлой полной задачей: её результат переносится без вызова ML."""
    assert decision.baseline is not None
    logger.info(
        "Анализ v2: перенесён результат прошлой задачи",
        extra={"job_id": str(job.id), "baseline_job_id": str(decision.baseline.job_id)},
    )
    await finish(
        job.id,
        AnalysisJobStatusVO.SUCCESS,
        source_ids=fetched.ids,
        snapshots=fetched.snapshots,
        job_sources=_numbered(fetched.records),
        run_input=RunInput(
            input_hash=input_hash,
            contract_version=CONTRACT_VERSION,
            provider=_settings.analysis_provider,
            decision=decision,
            stats=reused_run_stats(fetched.segments, fetched.snapshots, blocks),
            document_hash=document_hash,
        ),
        carry_from=decision.baseline.job_id,
    )


async def _delta(
    job: _StartedJob, decision: ReuseDecision, fetched: _FetchedSources
) -> DeltaSummary | None:
    if decision.baseline is None or _settings.analysis_incremental_mode != IncrementalMode.DELTA:
        return None
    async with uow_factory() as uow:
        baseline_sources = await uow.snapshots.list_job_sources(decision.baseline.job_id)
    return await delta_against(
        get_storage(), baseline_sources, fetched.snapshots, fetched.segments, job.id
    )


def _located_sources(
    fetched: _FetchedSources, job_sources: Sequence[JobSourceRecord]
) -> list[LocatedSource]:
    """Источники запроса с номерами строк задачи: по номеру основание найдёт свой снимок."""
    positions = {
        record.source_id: record.position
        for record in job_sources
        if record.status is JobSourceStatus.FETCHED and record.source_id is not None
    }
    return [
        LocatedSource(
            position=positions[source_id],
            source_id=payload.id,
            name=payload.name,
            text=payload.text,
            spans=layout.spans,
        )
        for source_id, payload, layout in zip(
            fetched.ids, fetched.payloads, fetched.layouts, strict=True
        )
    ]


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
        unavailable_records: list[JobSourceRecord] = []
        for source_id in wanted:
            source = found.get(source_id)
            if source is None:
                unavailable.append(str(source_id))
                unavailable_records.append(
                    _failed_record(None, str(source_id), "unknown", storage_key=None)
                )
                continue
            try:
                refs.append(build_source_ref(source))
            except ValueError:
                unavailable.append(source.name)
                unavailable_records.append(
                    _failed_record(
                        source.id, source.name, source.type.value, storage_key=source.storage_key
                    )
                )
        started = _StartedJob(
            id=job_id,
            project_id=document.project_id,
            document_id=document.id,
            document_name=document.name,
            document_format=document.format.value,
            storage_key=document.storage_key,
            sources=tuple(refs),
            unavailable=tuple(unavailable),
            unavailable_records=tuple(unavailable_records),
            strategy=job.strategy,
        )
        await uow.commit()
    return started


async def _load_document(job: _StartedJob) -> tuple[DocumentInput, str]:
    """Блоки строятся из файла на каждую попытку: одна задача на документ, разбор — один.

    sha256 файла считается от уже скачанных байтов: отдельная колонка у документа не нужна.
    """
    try:
        raw = await get_storage().download(job.storage_key)
        document_hash = hashlib.sha256(raw).hexdigest()
        parsed = (await get_document_layout_reader().read(job.storage_key, raw)).document
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        raise _JobFailed(
            "DOCUMENT_PARSE_ERROR", f"Не удалось разобрать документ: {type(exc).__name__}"
        ) from exc
    document = DocumentInput(
        id=job.document_id,
        name=job.document_name,
        format=job.document_format,
        plain_text=parsed.plain_text,
        blocks=parsed.blocks,
    )
    return document, document_hash


async def _attach_geometry(
    job: _StartedJob, drafts: list[SuggestionDraft], flags: list[FlagDraft]
) -> tuple[list[SuggestionDraft], list[FlagDraft]]:
    """Прямоугольники правок и флагов на страницах PDF.

    Считаются до записи результата, а не внутри неё: поиск открывает страницы PDF, и
    держать на это время блокировку строки задачи незачем. Файл скачивается заново, а не
    хранится с разбора: во время вызова конвейера он занимал бы память воркера. Разбор
    берётся из кэша. Сбой геометрии не роняет анализ: правки видны в тексте, без
    подсветки на странице.
    """
    if not drafts and not flags:
        return drafts, flags
    reader = get_document_layout_reader()
    try:
        raw = await get_storage().download(job.storage_key)
        layout = await reader.read(job.storage_key, raw)
        blocks = {b.ref: (b.start_offset, b.end_offset) for b in layout.document.blocks}
        spans = [highlight_span(d.start_offset, d.end_offset) for d in drafts]
        spans += [blocks.get(f.block_ref, (0, 0)) for f in flags]
        rects = await reader.locate(job.storage_key, raw, layout, spans)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.warning(
            "Анализ v2: не удалось найти правки на страницах PDF",
            exc_info=True,
            extra={"job_id": str(job.id)},
        )
        return drafts, flags
    if rects is None:
        return drafts, flags
    split = len(drafts)
    return (
        [replace(d, geometry=r) for d, r in zip(drafts, rects[:split], strict=True)],
        [replace(f, geometry=r) for f, r in zip(flags, rects[split:], strict=True)],
    )


async def _fetch_sources(refs: Sequence[SourceRef], project_id: uuid.UUID) -> _FetchedSources:
    """Тексты источников; недоступный источник не роняет задачу, а делает её частичной.

    Слишком длинный источник — ошибка всей задачи: без него правки были бы ложными, а
    обрезать его молча нельзя (см. ensure_source_fits).
    """
    semaphore = asyncio.Semaphore(_settings.analysis_source_fetch_concurrency)
    max_chars = _settings.analysis_max_source_chars

    async def fetch(ref: SourceRef) -> SourceLayout:
        async with semaphore:
            layout = await get_connector_for(ref.type).fetch_layout(ref)
        ensure_source_fits(ref.name, layout.text, max_chars)
        return layout

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
            fetched.records.append(
                _failed_record(ref.id, ref.name, ref.type.value, storage_key=ref.storage_key)
            )
            continue
        fetched.payloads.append(
            SourcePayload(id=str(ref.id), name=ref.name, type=ref.type, text=result.text)
        )
        fetched.layouts.append(result)
        fetched.ids.append(ref.id)
        # Снимок считается от того же текста, что уходит в запрос: цитаты модели
        # относятся именно к нему.
        segments = segment_fingerprints(result.text)
        snapshot = SnapshotDraft.from_segments(project_id, result.text, segments)
        fetched.snapshots.append(snapshot)
        fetched.segments.append(segments)
        fetched.records.append(
            JobSourceRecord(
                position=0,
                source_id=ref.id,
                name=ref.name,
                kind=ref.type.value,
                status=JobSourceStatus.FETCHED,
                content_hash=snapshot.content_hash,
                file_storage_key=ref.storage_key,
            )
        )
    return fetched


def _failed_record(
    source_id: uuid.UUID | None, name: str, kind: str, *, storage_key: str | None
) -> JobSourceRecord:
    return JobSourceRecord(
        position=0,
        source_id=source_id,
        name=name,
        kind=kind,
        status=JobSourceStatus.FAILED,
        file_storage_key=storage_key,
    )


def _numbered(records: Sequence[JobSourceRecord]) -> list[JobSourceRecord]:
    return [
        JobSourceRecord(
            position=position,
            source_id=record.source_id,
            name=record.name,
            kind=record.kind,
            status=record.status,
            content_hash=record.content_hash,
            file_storage_key=record.file_storage_key,
        )
        for position, record in enumerate(records)
    ]


async def _store_snapshots(job_id: uuid.UUID, snapshots: Sequence[SnapshotDraft]) -> None:
    """Текст и отпечатки снимков в хранилище до вызова ML.

    Ключ задан содержимым, поэтому уже записанный снимок не перезаписывается. Без снимка
    правки нельзя связать с текстом источника, поэтому сбой хранилища завершает задачу.
    """
    storage = get_storage()
    try:
        for snapshot in distinct_drafts(snapshots):
            if await storage.exists(snapshot.storage_key):
                continue
            await storage.upload(
                fingerprints_storage_key(snapshot.storage_key),
                UploadContent.from_bytes(
                    pack_fingerprints(snapshot.fingerprints), "application/octet-stream"
                ),
            )
            # Текст пишется последним: его наличие означает, что снимок записан целиком.
            await storage.upload(
                snapshot.storage_key,
                UploadContent.from_bytes(
                    snapshot.text.encode("utf-8"), "text/plain; charset=utf-8"
                ),
            )
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        logger.warning(
            "Анализ v2: снимок источника не сохранён",
            extra={"job_id": str(job_id), "error_type": type(exc).__name__},
        )
        raise _JobFailed("SOURCE_SNAPSHOT_ERROR", "Не удалось сохранить снимок источника") from exc


def _build_request(
    job: _StartedJob,
    attempt: int,
    document: DocumentInput,
    sources: Sequence[SourcePayload],
    editable: Collection[str] | None = None,
) -> AnalysisRequest:
    try:
        return build_analysis_request(
            job_id=job.id,
            attempt=attempt,
            document=document,
            sources=sources,
            limits=analysis_limits(_settings),
            editable=editable,
        )
    except LLMInputTooLargeError as exc:
        raise _JobFailed("LLM_INPUT_TOO_LARGE", str(exc)) from exc


def _log_call(
    job_id: uuid.UUID,
    attempt: int,
    request: AnalysisRequest,
    started_at: float,
    outcome: str,
    result: AnalysisResult | None = None,
    run: PipelineRunSummary | None = None,
) -> None:
    """Одна запись на вызов конвейера: по request_id её можно сопоставить с логами harness.

    Только счётчики и идентификаторы — тексты документа и правок в лог не попадают.
    """
    extra: dict[str, Any] = {
        "job_id": str(job_id),
        "request_id": request.request_id,
        "attempt": attempt,
        "provider": _settings.analysis_provider,
        "outcome": outcome,
        "duration_ms": round((time.perf_counter() - started_at) * 1000),
        "blocks": len(request.document.blocks),
        "sources": len(request.sources),
    }
    if result is not None:
        extra["suggestions"] = len(result.suggestions)
        extra["flags"] = len(result.flags)
    if run is not None:
        extra["checked_blocks"] = run.checked_blocks
        extra["failed_blocks"] = run.failed_blocks
    logger.info("Анализ v2: вызов конвейера", extra=extra)


def _map(
    job_id: uuid.UUID, attempt: int, result: AnalysisResult, request: AnalysisRequest
) -> tuple[list[SuggestionDraft], list[FlagDraft]]:
    mapped = map_result(result, request.document.blocks, max_flags=_settings.analysis_max_flags)
    if mapped.dropped:
        logger.warning(
            "Анализ v2: часть правок отброшена",
            extra={
                "job_id": str(job_id),
                "request_id": request.request_id,
                "attempt": attempt,
                "dropped": mapped.dropped,
                "dropped_total": sum(mapped.dropped.values()),
            },
        )
    drafts = finalize_drafts(mapped.suggestions, request.document.blocks, _propagation())
    propagated = len(drafts) - len(mapped.suggestions)
    if propagated:
        logger.info(
            "Анализ v2: добавлены догоняющие правки",
            extra={"job_id": str(job_id), "propagated": propagated},
        )
    return drafts, mapped.flags


def _propagation() -> PropagationLimits | None:
    if not _settings.suggestion_propagate_exact:
        return None
    return PropagationLimits(
        max_groups=_settings.suggestion_propagate_max_groups,
        max_per_group=_settings.suggestion_propagate_max_per_group,
    )


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
    flags: Sequence[FlagDraft] = (),
    source_ids: Sequence[uuid.UUID] = (),
    run: PipelineRunSummary | None = None,
    snapshots: Sequence[SnapshotDraft] = (),
    job_sources: Sequence[JobSourceRecord] = (),
    run_input: RunInput | None = None,
    carry_from: uuid.UUID | None = None,
    merge: MergePlan | None = None,
) -> bool:
    """Записать результат одной транзакцией. False — задача уже не активна, ничего не записано.

    carry_from: вместо drafts и flags копируется результат этой задачи вместе с решениями
    пользователя. С merge копируются только выбранные им строки, а drafts и flags
    записываются рядом: так выборочная задача получает полный результат.
    """
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
        if carry_from is not None and run is None:
            await uow.jobs.record_reuse(job, carry_from)
        elif run is not None:
            await uow.jobs.record_pipeline_run(job, run)
        if run_input is not None:
            await uow.jobs.record_input(job, run_input)
        # Снимки пишутся первыми: основания правок и флагов ссылаются на их id.
        evidence_sources: dict[int, EvidenceSource] = {}
        if job_sources:
            snapshot_ids = await uow.snapshots.ensure_snapshots(distinct_drafts(snapshots))
            await uow.snapshots.add_job_sources(job.id, job_sources, snapshot_ids)
            evidence_sources = evidence_sources_of(job_sources, snapshot_ids)
        suggestions_written, flags_written = len(drafts), len(flags)
        if carry_from is not None and merge is not None:
            suggestions_written += await uow.suggestions.carry_over(
                carry_from, job.id, merge.copy_suggestions, list(merge.stale_suggestions)
            )
            flags_written += await uow.flags.carry_over(carry_from, job.id, merge.copy_flags)
        elif carry_from is not None:
            suggestions_written = await uow.suggestions.carry_over(carry_from, job.id)
            flags_written = await uow.flags.carry_over(carry_from, job.id)
        if drafts:
            sources = await uow.sources.get_many_by_ids(list(source_ids))
            await uow.suggestions.bulk_create(
                to_suggestions(
                    drafts,
                    job_id=job.id,
                    document_id=document.id,
                    sources=sources,
                    evidence_sources=evidence_sources,
                )
            )
        await uow.flags.bulk_create(
            to_flags(
                flags, job_id=job.id, document_id=document.id, evidence_sources=evidence_sources
            )
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
        extra={
            "job_id": str(job_id),
            "status": status.value,
            "suggestions": suggestions_written,
            "flags": flags_written,
            "carried_from": str(carry_from) if carry_from else None,
        },
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
