"""Схемы API задач анализа: одиночный и массовый запуск."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.value_objects import AnalysisJobStatusVO


class AnalysisJobCreateRequest(BaseModel):
    """Тело POST /analysis-jobs. force нужен только для документа в статусе ready."""

    force: bool = False


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
    model_config = {"from_attributes": True}


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
