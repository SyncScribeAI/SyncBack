"""Агрегированный endpoint редактора документа и сброс анализа.

GET  /projects/{project_id}/documents/{document_id}/editor
POST /projects/{project_id}/documents/{document_id}/editor/reset

Агрегат возвращает метаданные документа, содержимое (и оригинал до правок для
документов с результатами ревью), страницу правок с полным счётчиком
suggestions_total, агрегатные счётчики по статусам и права действий из
DocumentLifecycle.
"""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_allowed_project, get_current_user
from app.api.schemas.document import DocumentSectionResponse, SuggestionCounters
from app.api.schemas.editor import (
    EditorAggregateResponse,
    EditorBlockResponse,
    EditorContent,
    EditorDocumentMeta,
    EditorPermissions,
    ResetResponse,
)
from app.api.schemas.suggestion import SuggestionResponse
from app.core.dependencies import (
    get_analysis_job_service,
    get_document_service,
    get_suggestion_service,
)
from app.domain.exceptions import DocumentNotFoundError, InvalidDocumentStatusError
from app.domain.interfaces.document_parser import ParsedDocument
from app.domain.lifecycle import DocumentLifecycle
from app.domain.services.analysis_job_service import AnalysisJobService
from app.domain.services.document_service import DocumentService
from app.domain.services.suggestion_service import SuggestionService
from app.domain.value_objects import DocumentStatusVO, PaginationParams
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.project import Project
from app.infrastructure.db.models.user import User

router = APIRouter(
    prefix="/projects/{project_id}/documents/{document_id}/editor",
    tags=["editor"],
)

# Режим отображения содержимого: до результатов анализа показываем исходник,
# во время ревью — текст с предложенными правками, после — чистовик.
_STATUS_VIEW_MODE: dict[DocumentStatusVO, str] = {
    DocumentStatusVO.DRAFT: "original",
    DocumentStatusVO.IN_PROGRESS: "original",
    DocumentStatusVO.AWAITING_APPROVAL: "suggested",
    DocumentStatusVO.READY: "clean",
}

_SUGGESTIONS_MAX_LIMIT = 200


def _build_editor_content(parsed: ParsedDocument) -> EditorContent:
    return EditorContent(
        plain_text=parsed.plain_text,
        sections=[
            DocumentSectionResponse(
                ref=s.ref,
                start_offset=s.start_offset,
                end_offset=s.end_offset,
            )
            for s in parsed.sections
        ],
        blocks=[
            EditorBlockResponse(
                ref=b.ref,
                type=b.type,
                start_offset=b.start_offset,
                end_offset=b.end_offset,
                editable=b.editable,
                heading_level=b.heading_level,
            )
            for b in parsed.blocks
        ],
    )


@router.get("", response_model=EditorAggregateResponse)
async def get_editor_aggregate(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    document_service: DocumentService = Depends(get_document_service),
    suggestion_service: SuggestionService = Depends(get_suggestion_service),
    suggestions_limit: int = Query(
        default=50,
        ge=1,
        le=_SUGGESTIONS_MAX_LIMIT,
        description=(
            "Максимальное число правок на странице (1–200). "
            "Используйте suggestions_total из ответа для построения пагинатора."
        ),
    ),
    suggestions_offset: int = Query(
        default=0,
        ge=0,
        description="Смещение для пагинации правок.",
    ),
) -> EditorAggregateResponse:
    """Полный агрегат данных для экрана редактора."""
    try:
        document = await document_service.get_document(project.id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    review_version = getattr(document, "review_version", 0) or 0
    view_mode = _STATUS_VIEW_MODE.get(document.status, "original")

    # PR4-FIX: updated_at берётся из document.updated_at (если есть),
    # иначе fallback на uploaded_at. Ранее всегда использовался uploaded_at.
    updated_at = getattr(document, "updated_at", None) or document.uploaded_at

    meta = EditorDocumentMeta(
        id=document.id,
        title=document.name,
        format=document.format.value,
        status=document.status,
        current_analysis_job_id=document.current_analysis_job_id,
        created_at=document.uploaded_at,
        updated_at=updated_at,
        review_version=review_version,
        view_mode=view_mode,
    )

    needs_original = DocumentLifecycle.has_review_results(document.status)

    # Содержимое читается из файлового хранилища, поэтому оба файла можно
    # запрашивать параллельно. Запросы к БД ниже идут последовательно: у них одна
    # AsyncSession на запрос, а SQLAlchemy не допускает её конкурентного использования.
    editor_content, original_content_raw = await asyncio.gather(
        _editor_content(document_service, document),
        _original_content(document_service, document) if needs_original else _none(),
    )
    suggestions_raw, suggestions_total = await suggestion_service.list_suggestions_for_document(
        project.id,
        document_id,
        PaginationParams(limit=suggestions_limit, offset=suggestions_offset),
    )
    status_counts = await suggestion_service.count_by_document_and_status(project.id, document_id)

    original_content = original_content_raw if needs_original else editor_content

    suggestions = [SuggestionResponse.model_validate(s) for s in suggestions_raw]

    pending = status_counts.get("pending", 0)
    accepted = status_counts.get("accepted", 0)
    rejected = status_counts.get("rejected", 0)

    counters = SuggestionCounters(
        total=suggestions_total,
        pending=pending,
        accepted=accepted,
        rejected=rejected,
    )

    permissions = EditorPermissions(
        can_analyze=DocumentLifecycle.can_start_analysis(document.status),
        can_review=DocumentLifecycle.can_review(document.status),
        can_export=DocumentLifecycle.can_export(document.status),
        can_delete=DocumentLifecycle.can_delete(document.status),
        sources_is_editable=DocumentLifecycle.can_edit_sources(document.status),
    )

    return EditorAggregateResponse(
        document=meta,
        content=editor_content,
        original_content=original_content,
        suggestions=suggestions,
        suggestions_total=suggestions_total,
        counters=counters,
        permissions=permissions,
    )


async def _editor_content(document_service: DocumentService, document: Document) -> EditorContent:
    return _build_editor_content(await document_service.get_document_content(document))


async def _original_content(document_service: DocumentService, document: Document) -> EditorContent:
    return _build_editor_content(await document_service.get_original_content(document))


async def _none() -> None:
    return None


@router.post(
    "/reset",
    response_model=ResetResponse,
    status_code=status.HTTP_200_OK,
    summary="Сбросить все изменения",
)
async def reset_analysis(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    job_service: AnalysisJobService = Depends(get_analysis_job_service),
) -> ResetResponse:
    """Сбросить все правки текущего анализа: статус suggestions → pending,
    документ → AWAITING_APPROVAL.
    """
    try:
        reset_result = await job_service.reset_analysis(project.id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidDocumentStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    suggestions_reset_count = reset_result.reset_count
    updated_doc = reset_result.document
    new_status = updated_doc.status.value
    new_review_version = getattr(updated_doc, "review_version", 0) or 0

    return ResetResponse(
        document_id=document_id,
        document_status=new_status,
        review_version=new_review_version,
        suggestions_reset_count=suggestions_reset_count,
    )
