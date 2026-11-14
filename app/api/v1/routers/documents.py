"""Документы внутри проекта: загрузка, список, содержимое, скачивание, экспорт,
прикрепление источников."""

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status

from app.api.deps import get_allowed_project, get_current_user
from app.api.schemas.document import (
    AttachSourcesRequest,
    AttachSourcesResponse,
    DocumentContentResponse,
    DocumentDownloadResponse,
    DocumentResponse,
    DocumentSectionResponse,
    document_response,
    uploaded_document_response,
)
from app.api.schemas.pagination import Page
from app.api.schemas.source import SourceResponse
from app.api.upload_utils import upload_content
from app.core.config import Settings, get_settings
from app.core.content_disposition import content_disposition
from app.core.dependencies import (
    get_audit_log_service,
    get_document_export_service,
    get_document_service,
    get_source_service,
)
from app.domain.exceptions import (
    DocumentNotFoundError,
    DocumentParseError,
    FileTooLargeError,
    InvalidDocumentStatusError,
    SourceLockError,
    SourceNotFoundError,
    UnreadablePdfError,
    UnsupportedExportFormatError,
    UnsupportedFileFormatError,
)
from app.domain.services.audit_log_service import AuditLogService
from app.domain.services.document_export_service import DocumentExportService, media_type_for
from app.domain.services.document_service import DocumentService
from app.domain.services.source_service import SourceService
from app.domain.value_objects import DocumentFormatVO, DocumentStatusVO, PaginationParams
from app.infrastructure.db.models.project import Project
from app.infrastructure.db.models.user import User

logger = logging.getLogger("syncscribe.api.documents")


_EXPORT_FORMAT_MAP: dict[str, DocumentFormatVO] = {
    "md": DocumentFormatVO.MARKDOWN,
    "docx": DocumentFormatVO.DOCX,
    "txt": DocumentFormatVO.TXT,
}

_STATUS_FILTER_MAP: dict[str, DocumentStatusVO] = {vo.value: vo for vo in DocumentStatusVO}

router = APIRouter(prefix="/projects/{project_id}/documents", tags=["documents"])


async def _log_download(
    audit_log_service: AuditLogService,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
) -> None:
    try:
        await audit_log_service.log_download(user_id, document_id)
    except Exception:
        logger.exception(
            "Failed to write audit_log for document download",
            extra={"document_id": str(document_id), "user_id": str(user_id)},
        )


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    project: Project = Depends(get_allowed_project),
    document_service: DocumentService = Depends(get_document_service),
    settings: Settings = Depends(get_settings),
) -> DocumentResponse:
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Имя файла обязательно")
    try:
        content = upload_content(file, settings.max_upload_size_bytes)
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    try:
        document, report = await document_service.upload_document(
            project.id, file.filename, content
        )
    # Формат известен, но файл непригоден (скан, пароль, сломан): это 422, а не 415.
    except (UnreadablePdfError, DocumentParseError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except UnsupportedFileFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    return uploaded_document_response(document, report)


@router.get("", response_model=Page[DocumentResponse])
async def list_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=(
            f"Фильтр по статусу документа. Допустимые значения: {', '.join(_STATUS_FILTER_MAP)}"
        ),
    ),
    project: Project = Depends(get_allowed_project),
    document_service: DocumentService = Depends(get_document_service),
) -> Page[DocumentResponse]:
    status_vo: DocumentStatusVO | None = None
    if status_filter is not None:
        status_vo = _STATUS_FILTER_MAP.get(status_filter.lower())
        if status_vo is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Неподдерживаемый статус: {status_filter!r}. "
                    f"Допустимые значения: {', '.join(_STATUS_FILTER_MAP)}"
                ),
            )

    pagination = PaginationParams(limit=limit, offset=offset)
    documents, total = await document_service.list_documents(
        project.id, pagination, status_filter=status_vo
    )
    jobs = await document_service.current_analysis_jobs(documents)
    return Page[DocumentResponse](
        items=[document_response(d, jobs.get(d.id)) for d in documents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    try:
        document = await document_service.get_document(project.id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    jobs = await document_service.current_analysis_jobs([document])
    return document_response(document, jobs.get(document.id))


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    document_service: DocumentService = Depends(get_document_service),
) -> None:
    try:
        await document_service.delete_document_by_id(project.id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidDocumentStatusError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{document_id}/content", response_model=DocumentContentResponse)
async def get_document_content(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentContentResponse:
    try:
        document = await document_service.get_document(project.id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        parsed = await document_service.get_document_content(document)
    except (DocumentParseError, UnreadablePdfError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Не удалось распарсить документ: {exc}",
        ) from exc
    except OSError as exc:
        logger.error(
            "Storage error while fetching document content",
            extra={"document_id": str(document_id)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ошибка хранилища: не удалось получить содержимое документа",
        ) from exc
    except Exception as exc:
        logger.exception(
            "Unexpected error while fetching document content",
            extra={"document_id": str(document_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Внутренняя ошибка сервера при обработке документа",
        ) from exc
    return DocumentContentResponse(
        plain_text=parsed.plain_text,
        sections=[
            DocumentSectionResponse(ref=s.ref, start_offset=s.start_offset, end_offset=s.end_offset)
            for s in parsed.sections
        ],
    )


@router.get("/{document_id}/download", response_model=DocumentDownloadResponse)
async def get_download_url(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    document_service: DocumentService = Depends(get_document_service),
    audit_log_service: AuditLogService = Depends(get_audit_log_service),
) -> DocumentDownloadResponse:
    try:
        document = await document_service.get_document(project.id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    url, expires_in = await document_service.get_download_url(document)
    await _log_download(audit_log_service, current_user.id, document.id)
    return DocumentDownloadResponse(download_url=url, expires_in=expires_in)


@router.get(
    "/{document_id}/file",
    summary="Исходный файл документа для просмотра в браузере",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def get_document_file(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    document_service: DocumentService = Depends(get_document_service),
    audit_log_service: AuditLogService = Depends(get_audit_log_service),
) -> Response:
    """Байты исходника без правок, под обычной авторизацией API.

    В отличие от /download, не требует доступа браузера к хранилищу напрямую:
    фронт рендерит .docx в режиме «Оригинал» из этого ответа.
    """
    try:
        document = await document_service.get_document(project.id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        raw_bytes = await document_service.get_original_file(document)
    except OSError as exc:
        logger.error(
            "Storage error while fetching document file",
            extra={"document_id": str(document_id)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ошибка хранилища: не удалось получить файл документа",
        ) from exc
    await _log_download(audit_log_service, current_user.id, document.id)
    return Response(
        content=raw_bytes,
        media_type=media_type_for(DocumentFormatVO(document.format)),
        headers={
            "Content-Disposition": content_disposition(document.name, inline=True),
            "Cache-Control": "private, max-age=60",
        },
    )


@router.get("/{document_id}/export", summary="Экспорт документа с принятыми правками")
async def export_document(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    document_service: DocumentService = Depends(get_document_service),
    export_service: DocumentExportService = Depends(get_document_export_service),
    audit_log_service: AuditLogService = Depends(get_audit_log_service),
    export_format: str | None = Query(
        default=None,
        description="Целевой формат экспорта: md, docx, txt.",
    ),
) -> Response:
    target_format: DocumentFormatVO | None = None
    if export_format is not None:
        target_format = _EXPORT_FORMAT_MAP.get(export_format.lower())
        if target_format is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Неподдерживаемый формат экспорта: {export_format!r}. "
                    f"Допустимые значения: {', '.join(_EXPORT_FORMAT_MAP)}"
                ),
            )

    try:
        document = await document_service.get_document(project.id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        content, filename, media_type = await export_service.export_document(
            document, target_format=target_format
        )
    except UnsupportedExportFormatError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvalidDocumentStatusError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await _log_download(audit_log_service, current_user.id, document.id)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.post(
    "/{document_id}/sources",
    response_model=AttachSourcesResponse,
    summary="Прикрепить источники к документу",
)
async def attach_sources(
    document_id: uuid.UUID,
    payload: AttachSourcesRequest,
    project: Project = Depends(get_allowed_project),
    document_service: DocumentService = Depends(get_document_service),
    source_service: SourceService = Depends(get_source_service),
) -> AttachSourcesResponse:
    """Прикрепить источники проекта к документу.

    Возвращает документ вместе с прикреплёнными источниками, чтобы фронту
    не требовался отдельный GET /sources.
    """
    try:
        document = await document_service.get_document(project.id, document_id)
        sources = await source_service.get_sources_for_project(project.id, payload.source_ids)
    except (DocumentNotFoundError, SourceNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        sources = await source_service.attach_sources_to_document(document, sources)
    except SourceLockError as exc:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(exc)) from exc
    return AttachSourcesResponse(
        document=DocumentResponse.model_validate(document),
        sources=[SourceResponse.model_validate(s) for s in sources],
    )
