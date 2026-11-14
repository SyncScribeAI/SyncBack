"""Схемы API задач анализа: одиночный и массовый запуск."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.value_objects import AnalysisJobStatusVO


class AnalysisJobCreateRequest(BaseModel):
    """Тело POST /analysis-jobs. force нужен только для документа в статусе ready."""

    force: bool = False
    strategy: Literal["auto", "full"] = Field(
        default="auto",
        description=(
            "auto переносит результат прошлой полной задачи, если вход анализа не изменился; "
            "full всегда вызывает ML-конвейер"
        ),
    )


class AnalysisDeltaAudit(BaseModel):
    """Сравнение выборочной задачи с контрольным полным анализом того же входа по блокам."""

    blocks_full: int
    blocks_delta: int
    missed: int
    extra: int
    recall: float


class AnalysisIncrementalInfo(BaseModel):
    """Как задача получила результат: полным анализом, переносом или выборочной проверкой."""

    mode: Literal["reused", "full", "delta"]
    reason: str = Field(
        description=(
            "reused, mode_off, strategy_full, sources_failed, no_baseline или input_changed"
        )
    )
    baseline_job_id: uuid.UUID | None = None
    chain_length: int | None = Field(
        default=None, description="Выборочных задач подряд после последнего полного анализа"
    )
    fallback_reason: str | None = Field(
        default=None,
        description=(
            "Почему вход проверен полным анализом: причина решения о переносе или предел"
            " выборочной проверки (changed_share, blocks_share, chain_limit и другие)"
        ),
    )
    changed_share: float | None = Field(
        default=None, description="Доля символов источников в новых сегментах"
    )
    segments_added: int | None = None
    segments_removed: int | None = None
    blocks_selected: int | None = Field(
        default=None, description="Блоки, проверенные заново выборочной задачей"
    )
    selected_by_overlap: int | None = None
    selected_by_token: int | None = None
    selected_by_evidence: int | None = None
    selected_by_flag: int | None = None
    carried_suggestions: int | None = None
    carried_flags: int | None = None
    stale: int | None = Field(
        default=None, description="Перенесённые решения, основание которых изменилось"
    )
    replaced: int | None = Field(
        default=None, description="Ожидающие правки проверенных блоков, заменённые новым ответом"
    )
    inherited: int | None = Field(
        default=None, description="Новые правки, совпавшие с прошлыми и получившие их решение"
    )
    dropped_add: int | None = Field(
        default=None, description="Добавления, опиравшиеся только на неизменённые сегменты"
    )
    evidence_flags: int | None = Field(
        default=None, description="Флаги «основание удалено из источника»"
    )
    audit: AnalysisDeltaAudit | None = None


class AnalysisRunStats(BaseModel):
    """Объём входа и отправленного в ML; в harness счётчики вызовов модели, если они есть."""

    segments_total: int
    segments_sent: int
    chars_total: int
    chars_sent: int
    blocks_total: int
    blocks_checked: int
    harness: dict[str, float] | None = None


class AnalysisJobResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    status: AnalysisJobStatusVO
    error_code: str | None
    error_message: str | None
    retry_count: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    partial_success: bool = False
    checked_blocks: int | None = Field(
        default=None, description="Сколько блоков проверил ML-конвейер; None — неизвестно"
    )
    failed_blocks: int | None = Field(
        default=None, description="Сколько блоков конвейер не смог проверить; None — неизвестно"
    )
    strategy: Literal["auto", "full"] = "auto"
    incremental: AnalysisIncrementalInfo | None = Field(
        default=None, validation_alias="incremental_view"
    )
    run_stats: AnalysisRunStats | None = Field(default=None, validation_alias="run_stats_view")
    model_config = {"from_attributes": True, "populate_by_name": True}


class AnalysisJobConflictResponse(BaseModel):
    """Ответ 409 на повторный анализ готового документа без force: клиент спрашивает подтверждение."""

    detail: str
    confirmation_required: bool = True


class BulkAnalysisRequest(BaseModel):
    """Тело массового запуска.

    document_ids не задан — запускаются все документы проекта, из которых анализ
    возможен. force=true включает в запуск готовые документы.
    """

    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    force: bool = False
    strategy: Literal["auto", "full"] = Field(
        default="auto",
        description=(
            "auto переносит результат прошлой полной задачи, если вход анализа не изменился; "
            "full всегда вызывает ML-конвейер"
        ),
    )


class BulkJobResult(BaseModel):
    document_id: uuid.UUID
    job: AnalysisJobResponse | None = None
    skip_reason: (
        Literal["not_found", "confirmation_required", "analysis_running", "invalid_status"] | None
    ) = None
    error: str | None = None


class BulkAnalysisJobsResponse(BaseModel):
    started: int
    skipped: int
    confirmation_required: int = Field(
        description="Сколько готовых документов пропущено: повторите запрос с force=true"
    )
    results: list[BulkJobResult]
