"""Источники истины внутри проекта.

POST /projects/{id}/sources                 — создать URL-источник
POST /projects/{id}/sources/note            — создать текстовую заметку (deprecated)
POST /projects/{id}/sources/file            — загрузить файл
GET  /projects/{id}/sources                 — список с пагинацией
GET  /projects/{id}/sources/{sid}/file      — исходный файл (см. source_files.py)
GET  /projects/{id}/sources/{sid}/content   — текст для просмотра
POST /projects/{id}/sources/{sid}/check     — пробная загрузка и разбор без сохранения
DEL  /projects/{id}/sources/{sid}           — удалить источник

Правила «можно ли менять источники документа» живут в SourceService:
роутер только переводит доменные исключения в HTTP-статусы. Источники заблокированы (423)
только пока документ анализируется; на ревью их можно менять.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from app.api.deps import get_allowed_project
from app.api.schemas.pagination import Page
from app.api.schemas.source import (
    NoteCreateRequest,
    SourceCheckResponse,
    SourceContentResponse,
    SourceCreateRequest,
    SourceResponse,
)
from app.api.upload_utils import upload_content
from app.core.config import Settings, get_settings
from app.core.dependencies import get_source_service
from app.domain.exceptions import (
    DocumentNotFoundError,
    FileTooLargeError,
    InvalidSourceScopeError,
    InvalidSourceUrlError,
    SourceLockError,
    SourceNotFoundError,
    UnsupportedFormatError,
)
from app.domain.services.source_service import SourceService
from app.domain.value_objects import SourceScopeVO
from app.infrastructure.db.models.project import Project

logger = logging.getLogger("syncscribe.api.sources")

router = APIRouter(prefix="/projects/{project_id}/sources", tags=["sources"])


def _to_http(exc: Exception) -> HTTPException:
    if isinstance(exc, DocumentNotFoundError | SourceNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, SourceLockError):
        return HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(exc))
    if isinstance(exc, FileTooLargeError):
        return HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc))
    if isinstance(exc, InvalidSourceScopeError | InvalidSourceUrlError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    raise exc


_SOURCE_ERRORS = (
    DocumentNotFoundError,
    SourceNotFoundError,
    SourceLockError,
    FileTooLargeError,
    InvalidSourceScopeError,
    InvalidSourceUrlError,
)


@router.post("", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def create_url_source(
    payload: SourceCreateRequest,
    project: Project = Depends(get_allowed_project),
    source_service: SourceService = Depends(get_source_service),
) -> SourceResponse:
    """Создать источник типа URL."""
    try:
        source = await source_service.create_url_source(
            project,
            payload.name,
            payload.url,
            scope=SourceScopeVO(payload.scope),
            document_id=payload.document_id,
        )
    except _SOURCE_ERRORS as exc:
        raise _to_http(exc) from exc
    return SourceResponse.model_validate(source)


@router.post(
    "/note",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
)
async def create_note_source(
    payload: NoteCreateRequest,
    project: Project = Depends(get_allowed_project),
    source_service: SourceService = Depends(get_source_service),
) -> SourceResponse:
    """Создать текстовую заметку. Текст сохраняется как .txt-файл, type в ответе — file.

    Эндпоинт внутренний; для внешних клиентов — POST /sources/file.
    """
    try:
        source = await source_service.create_note_source(
            project,
            payload.name,
            payload.text_content,
            scope=SourceScopeVO(payload.scope),
            document_id=payload.document_id,
        )
    except _SOURCE_ERRORS as exc:
        raise _to_http(exc) from exc
    return SourceResponse.model_validate(source)


@router.post("/file", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def upload_file_source(
    name: str = Form(..., min_length=1, max_length=255),
    scope: SourceScopeVO = Form(default=SourceScopeVO.PROJECT),
    document_id: uuid.UUID | None = Form(
        default=None, description="UUID документа (обязателен при scope=document)"
    ),
    file: UploadFile = File(...),
    project: Project = Depends(get_allowed_project),
    source_service: SourceService = Depends(get_source_service),
    settings: Settings = Depends(get_settings),
) -> SourceResponse:
    """Загрузить файловый источник."""
    try:
        source = await source_service.create_file_source(
            project,
            name=name,
            filename=file.filename or "upload",
            content=upload_content(file, settings.max_upload_size_bytes),
            scope=scope,
            document_id=document_id,
        )
    except _SOURCE_ERRORS as exc:
        raise _to_http(exc) from exc
    return SourceResponse.model_validate(source)


@router.get("", response_model=Page[SourceResponse])
async def list_sources(
    project: Project = Depends(get_allowed_project),
    source_service: SourceService = Depends(get_source_service),
    scope: SourceScopeVO | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[SourceResponse]:
    """Список источников проекта с пагинацией; ?scope= фильтрует по области."""
    items, total = await source_service.list_sources(
        project.id, limit=limit, offset=offset, scope=scope
    )
    return Page(
        items=[SourceResponse.model_validate(s) for s in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{source_id}/content", response_model=SourceContentResponse)
async def get_source_content(
    source_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    source_service: SourceService = Depends(get_source_service),
) -> SourceContentResponse:
    """Текст источника для просмотра без редактирования.

    Для url-источника и файла неподдерживаемого формата ответ 200 с preview_available=false:
    это не ошибка, а подсказка фронту показать ссылку или кнопку скачивания.
    """
    try:
        source = await source_service.get_source(project.id, source_id)
    except _SOURCE_ERRORS as exc:
        raise _to_http(exc) from exc
    base = SourceContentResponse(
        id=source.id,
        name=source.name,
        type=source.type.value,
        url=source.url,
        created_at=source.created_at,
        preview_available=False,
    )
    if not source.storage_key:
        return base.model_copy(update={"reason": "Источник-ссылка открывается по адресу"})
    try:
        text = await source_service.get_source_text(source)
    except UnsupportedFormatError as exc:
        return base.model_copy(update={"reason": str(exc)})
    except OSError as exc:
        logger.error(
            "Storage error while fetching source content",
            extra={"source_id": str(source_id)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ошибка хранилища: не удалось получить содержимое источника",
        ) from exc
    except Exception:
        logger.exception("Источник не разобран для просмотра", extra={"source_id": str(source_id)})
        return base.model_copy(
            update={"reason": "Файл не удалось прочитать как текст — скачайте его"}
        )
    return base.model_copy(update={"plain_text": text, "preview_available": True})


@router.post("/{source_id}/check", response_model=SourceCheckResponse)
async def check_source(
    source_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    source_service: SourceService = Depends(get_source_service),
) -> SourceCheckResponse:
    """Загрузить и разобрать источник так же, как это сделает анализ.

    Отказ загрузки приходит в ответе 200 с ok=false и текстом в error: фронтенд
    показывает его сразу после добавления ссылки.
    """
    try:
        result = await source_service.check_source(project.id, source_id)
    except _SOURCE_ERRORS as exc:
        raise _to_http(exc) from exc
    return SourceCheckResponse(ok=result.ok, chars=result.chars, error=result.error)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    source_service: SourceService = Depends(get_source_service),
) -> None:
    """Удалить источник. 423, если он прикреплён к документу, который сейчас анализируется."""
    try:
        await source_service.delete_source(project.id, source_id)
    except _SOURCE_ERRORS as exc:
        raise _to_http(exc) from exc
