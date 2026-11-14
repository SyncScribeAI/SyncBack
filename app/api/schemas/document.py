"""Схемы документов.

Публичный status документа — 4 значения (draft/in_progress/awaiting_approval/ready);
техническое состояние анализа (pending/processing/failed/cancelled/completed) отдаётся
отдельным полем analysis: AnalysisStateResponse, маппинг — resolve_document_public_status_and_analysis.

DocumentListItem.sources: None — поле не запрашивалось, [] — источников нет.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.api.schemas.source import SourceBadge, SourceResponse
from app.domain.interfaces.upload_file_inspector import UploadFileReport

# ---------------------------------------------------------------------------
# Техническое состояние анализа (не путать с публичным статусом документа)
# ---------------------------------------------------------------------------


class AnalysisStateResponse(BaseModel):
    """Объект, описывающий состояние последнего analysis job.

    Отдаётся в поле ``analysis`` объектов DocumentResponse и DocumentListItem.
    Если анализ ни разу не запускался — поле равно null.

    Поля:
        job_id       — UUID последнего AnalysisJob (null при отсутствии job).
        state        — техническое состояние: pending | processing |
                       completed | failed | cancelled.
        error_code   — машинный код ошибки (только при failed).
        error_message — человекочитаемое описание (при failed / cancelled).
        can_retry    — True, если фронт может показать кнопку «Анализировать».
    """

    job_id: uuid.UUID | None = None
    state: Literal["pending", "processing", "completed", "failed", "cancelled"]
    error_code: str | None = None
    error_message: str | None = None
    can_retry: bool = True

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Хелпер маппинга (domain → API-контракт)
# ---------------------------------------------------------------------------


def resolve_document_public_status_and_analysis(
    doc_status: str,
    latest_job: Any,
) -> tuple[str, AnalysisStateResponse | None]:
    """Преобразует внутреннее состояние документа + последний job → публичный контракт.

    Аргументы:
        doc_status  — строковое значение DocumentStatusVO (из ORM-модели).
        latest_job  — ORM-объект AnalysisJob или None.

    Возвращает:
        (public_status, analysis_state)
        public_status  — одно из: draft | in_progress | awaiting_approval | ready.
        analysis_state — AnalysisStateResponse или None.
    """
    if latest_job is None:
        return doc_status, None

    job_status = str(latest_job.status)

    if job_status == "failed":
        return "draft", AnalysisStateResponse(
            job_id=latest_job.id,
            state="failed",
            error_code=getattr(latest_job, "error_code", None) or "ANALYSIS_FAILED",
            error_message=getattr(latest_job, "error_message", None) or "Ошибка выполнения анализа",
            can_retry=True,
        )

    if job_status == "cancelled":
        return "draft", AnalysisStateResponse(
            job_id=latest_job.id,
            state="cancelled",
            error_code="ANALYSIS_CANCELLED",
            error_message="Анализ отменён пользователем",
            can_retry=True,
        )

    if job_status == "pending":
        return "in_progress", AnalysisStateResponse(
            job_id=latest_job.id,
            state="pending",
            can_retry=False,
        )

    if job_status in ("processing", "dispatched"):
        return "in_progress", AnalysisStateResponse(
            job_id=latest_job.id,
            state="processing",
            can_retry=False,
        )

    # success / partial_success / completed
    return doc_status, AnalysisStateResponse(
        job_id=latest_job.id,
        state="completed",
        can_retry=True,
    )


# ---------------------------------------------------------------------------
# Схемы ответов
# ---------------------------------------------------------------------------


class DocumentResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    format: str
    size_bytes: int
    uploaded_at: datetime
    # Публичный статус: только 4 значения (техническое состояние → поле analysis)
    status: Literal["draft", "in_progress", "awaiting_approval", "ready"]
    current_analysis_job_id: uuid.UUID | None = None
    # Версия для If-Match / ETag
    review_version: int = 0
    # Техническое состояние последнего analysis job (null — анализ не запускался)
    analysis: AnalysisStateResponse | None = None
    pages_without_text: list[int] | None = Field(
        default=None,
        description=(
            "Только в ответе на загрузку PDF: страницы без текстового слоя (с единицы). "
            "Документ принят, но текст этих страниц в анализ и редактор не попадёт"
        ),
    )
    model_config = {"from_attributes": True}


def uploaded_document_response(document: Any, report: UploadFileReport) -> DocumentResponse:
    response = DocumentResponse.model_validate(document)
    if report.pages_without_text:
        response.pages_without_text = list(report.pages_without_text)
    return response


def document_response(document: Any, latest_job: Any = None) -> DocumentResponse:
    """Ответ по документу с публичным статусом и состоянием его текущей задачи анализа."""
    public_status, analysis = resolve_document_public_status_and_analysis(
        str(document.status), latest_job
    )
    response = DocumentResponse.model_validate(document)
    return response.model_copy(update={"status": public_status, "analysis": analysis})


class DocumentContentResponse(BaseModel):
    plain_text: str
    sections: list["DocumentSectionResponse"]


class DocumentSectionResponse(BaseModel):
    ref: str
    start_offset: int
    end_offset: int


class DocumentDownloadResponse(BaseModel):
    download_url: str
    expires_in: int


class AttachSourcesRequest(BaseModel):
    source_ids: list[uuid.UUID]


class AttachSourcesResponse(BaseModel):
    """Ответ POST /documents/{id}/sources.

    Возвращает обновлённые метаданные документа и полный список
    прикреплённых к нему источников, чтобы фронт не делал дополнительный
    GET /sources после операции attach.
    """

    document: DocumentResponse
    sources: list[SourceResponse]

    model_config = {"from_attributes": True}


# ── «Мои документы» ───────────────────────────────────────────────────────────────


class DocumentListProject(BaseModel):
    id: uuid.UUID
    name: str


class SuggestionCounters(BaseModel):
    total: int
    pending: int
    accepted: int
    rejected: int


class DocumentListItem(BaseModel):
    id: uuid.UUID
    name: str
    format: str
    size_bytes: int
    # Публичный статус: только 4 значения
    status: Literal["draft", "in_progress", "awaiting_approval", "ready"]
    current_analysis_job_id: uuid.UUID | None = None
    # Как и DocumentResponse.uploaded_at — когда загружен файл.
    uploaded_at: datetime
    updated_at: datetime
    project: DocumentListProject
    suggestions: SuggestionCounters
    # Бейджи источников документа (document-scope).
    # None  = поле не запрашивалось (например, в лёгком листинге).
    # []    = запрашивалось, источников нет.
    # [...]  = список бейджей для иконок на карточке.
    sources: list[SourceBadge] | None = None
    # Техническое состояние последнего analysis job
    analysis: AnalysisStateResponse | None = None


def document_list_item(
    document: Any,
    *,
    project_name: str,
    suggestions: SuggestionCounters,
    sources: list[Any] | None = None,
    latest_job: Any = None,
) -> DocumentListItem:
    """Собрать элемент списка документов из ORM-объекта Document.

    Единственное место, где внутренние поля документа переводятся в контракт
    карточки; роутеры «Мои документы» и страницы проекта используют его оба.
    """
    public_status, analysis = resolve_document_public_status_and_analysis(
        str(document.status), latest_job
    )
    return DocumentListItem(
        id=document.id,
        name=document.name,
        format=str(document.format),
        size_bytes=document.size_bytes,
        status=public_status,  # type: ignore[arg-type]
        current_analysis_job_id=document.current_analysis_job_id,
        uploaded_at=document.uploaded_at,
        updated_at=document.updated_at,
        project=DocumentListProject(id=document.project_id, name=project_name),
        suggestions=suggestions,
        sources=None
        if sources is None
        else [SourceBadge(id=s.id, name=s.name, type=s.type.value) for s in sources],
        analysis=analysis,
    )


class DocumentListPage(BaseModel):
    items: list[DocumentListItem]
    total: int
    limit: int
    offset: int
