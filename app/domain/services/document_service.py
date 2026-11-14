"""Бизнес-логика документов: загрузка, список, содержимое, удаление.

Зависит только от портов домена (IUnitOfWork, FileStorage, IDocumentParserRegistry)
и ограничений UploadLimits; один uow.commit() на операцию.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.domain.document_geometry import DocumentLayout
from app.domain.exceptions import (
    DocumentNotFoundError,
    FileTooLargeError,
    InvalidDocumentStatusError,
    UnsupportedFileFormatError,
)
from app.domain.interfaces.document_layout import DocumentLayoutReader
from app.domain.interfaces.document_parser import IDocumentParserRegistry, ParsedDocument
from app.domain.interfaces.file_storage import FileStorage, UploadContent
from app.domain.interfaces.parse_pool import UNBOUNDED_PARSE_POOL, ParsePool
from app.domain.interfaces.repositories import DocumentRow
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.interfaces.upload_file_inspector import UploadFileInspector, UploadFileReport
from app.domain.lifecycle import DocumentLifecycle
from app.domain.policies import UploadLimits
from app.domain.source_snapshot import fingerprints_storage_key
from app.domain.storage_keys import derived_document_keys, document_storage_key
from app.domain.value_objects import (
    DocumentFormatVO,
    DocumentStatusVO,
    KeysetPage,
    PaginationParams,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.analysis_job import AnalysisJob
    from app.infrastructure.db.models.document import Document

logger = logging.getLogger("syncscribe.services.document")


# Единственный источник правды о принимаемых расширениях: его же отдаёт /system/capabilities.
FORMAT_BY_EXTENSION: dict[str, DocumentFormatVO] = {
    ".docx": DocumentFormatVO.DOCX,
    ".txt": DocumentFormatVO.TXT,
    ".md": DocumentFormatVO.MARKDOWN,
    ".markdown": DocumentFormatVO.MARKDOWN,
    ".pdf": DocumentFormatVO.PDF,
}


def _extension_to_format(suffix: str) -> DocumentFormatVO:
    fmt = FORMAT_BY_EXTENSION.get(suffix)
    if fmt is None:
        raise UnsupportedFileFormatError(
            f"Формат '{suffix or 'без расширения'}' не поддерживается. "
            "Допустимые форматы: docx, txt, md, pdf"
        )
    return fmt


def _document_storage_keys(*keys: str | None) -> list[str]:
    """Ключи файлов документа и производных от них объектов без пустых значений и дубликатов."""
    return list(dict.fromkeys(k for key in keys if key for k in (key, *derived_document_keys(key))))


class DocumentService:
    def __init__(
        self,
        uow: IUnitOfWork,
        file_storage: FileStorage,
        parser_registry: IDocumentParserRegistry,
        upload_limits: UploadLimits,
        download_url_ttl_seconds: int,
        file_inspector: UploadFileInspector | None = None,
        layout_reader: DocumentLayoutReader | None = None,
        parse_pool: ParsePool = UNBOUNDED_PARSE_POOL,
    ):
        self._parse_pool = parse_pool
        self._uow = uow
        self._storage = file_storage
        self._parser_registry = parser_registry
        self._upload_limits = upload_limits
        self._download_url_ttl_seconds = download_url_ttl_seconds
        self._file_inspector = file_inspector
        self._layout_reader = layout_reader

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def _resolve_format(self, filename: str) -> DocumentFormatVO:
        return _extension_to_format(Path(filename).suffix.lower())

    async def upload_document(
        self,
        project_id: uuid.UUID,
        filename: str,
        content: UploadContent,
    ) -> tuple[Document, UploadFileReport]:
        """Загрузить документ в хранилище и создать запись в БД.

        Файл проверяется до записи: PDF без текстового слоя отклоняется сразу, а не при
        первом открытии в редакторе, и не оставляет в хранилище ничего.
        """
        if self._upload_limits.exceeded_by(content.size):
            raise FileTooLargeError(f"Файл превышает лимит {self._upload_limits.max_size_mb} МБ")
        document_format = self._resolve_format(filename)
        report = UploadFileReport()
        if self._file_inspector is not None:
            report = await self._file_inspector.inspect(filename, content)
        document_id = uuid.uuid4()
        storage_key = document_storage_key(project_id, document_id, filename)
        await self._storage.upload(storage_key, content)

        try:
            async with self._uow:
                saved = await self._uow.documents.create(
                    id=document_id,
                    project_id=project_id,
                    name=filename,
                    format=document_format,
                    storage_key=storage_key,
                    size_bytes=content.size,
                )
                await self._uow.commit()
        except Exception:
            await self._storage.delete(storage_key)
            raise
        return saved, report

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        project_id: uuid.UUID,
        pagination: PaginationParams | KeysetPage,
        *,
        status_filter: DocumentStatusVO | None = None,
    ) -> tuple[list[Document], int]:
        """Постраничный список документов проекта.

        status_filter — опциональный фильтр по статусу (пробрасывается в репозиторий).
        """
        async with self._uow:
            items = await self._uow.documents.list_for_project(
                project_id, pagination, status=status_filter
            )
            total = await self._uow.documents.count_for_project(project_id, status=status_filter)
        return items, total

    async def get_document(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> Document:
        async with self._uow:
            document = await self._uow.documents.get_by_id(document_id)
        if document is None or document.project_id != project_id:
            raise DocumentNotFoundError(f"Документ {document_id} не найден в проекте {project_id}")
        return document

    async def current_analysis_jobs(
        self, documents: Sequence[Document]
    ) -> dict[uuid.UUID, AnalysisJob]:
        """Текущая задача анализа каждого документа, ключ — id документа.

        Нужна, чтобы показать состояние анализа (ошибка, отмена, ход) рядом с документом;
        все задачи читаются одним запросом.
        """
        job_to_document = {
            d.current_analysis_job_id: d.id for d in documents if d.current_analysis_job_id
        }
        if not job_to_document:
            return {}
        async with self._uow:
            jobs = await self._uow.jobs.list_by_ids(list(job_to_document))
        return {job_to_document[job.id]: job for job in jobs}

    async def list_all_for_user(
        self,
        owner_id: uuid.UUID,
        *,
        status: DocumentStatusVO | None = None,
        outdated: bool = False,
        search: str | None = None,
        sort_by: Literal["created_at", "updated_at", "name"] = "updated_at",
        sort_dir: Literal["asc", "desc"] = "desc",
        pagination: PaginationParams | None = None,
    ) -> tuple[list[DocumentRow], int]:
        """Список всех документов пользователя с агрегированными счётчиками правок.

        Возвращает (строки, total); строка — dict с ключами document, project_name
        и suggestions_*, его собирает репозиторий.
        """
        _pagination = pagination or PaginationParams(limit=50, offset=0)
        async with self._uow:
            return await self._uow.documents.list_all_for_user(
                owner_id,
                status=status,
                outdated=outdated,
                search=search,
                sort_by=sort_by,
                sort_dir=sort_dir,
                limit=_pagination.limit,
                offset=_pagination.offset,
            )

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    async def _delete_storage_objects(
        self, keys: list[str], document_id: uuid.UUID, message: str
    ) -> None:
        """Best-effort удаление файлов после commit: сбой логируется и не прерывает остальные."""
        for key in keys:
            try:
                await self._storage.delete(key)
            except Exception:  # noqa: BLE001
                logger.warning(
                    message,
                    exc_info=True,
                    extra={"storage_key": key, "document_id": str(document_id)},
                )

    async def _unreferenced_snapshot_keys(self, project_id: uuid.UUID) -> list[str]:
        """Снимки, на которые после удаления задач документа не ссылается ни одна задача.

        Снимок общий для проекта, поэтому удаляется только без ссылок из других документов.
        """
        keys = await self._uow.snapshots.delete_unreferenced(project_id)
        return [k for key in keys for k in (key, fingerprints_storage_key(key))]

    async def delete_document(self, document: Document) -> None:
        """
        Удаление документа.

        Порядок операций внутри транзакции:
          1. delete_document_scoped_sources — удаляем orphan-источники scope=DOCUMENT
             ДО flush document, пока document_sources ещё существуют (подзапрос их читает).
          2. delete(document) — удаляем саму запись документа;
             FK-каскад по document_sources срабатывает здесь.
          3. commit() — единственный коммит на операцию.
        После коммита best-effort удаление всех файлов документа
        (storage_key, original_storage_key, exported_storage_key) и файлов
        его собственных источников.

        Используется когда ORM-объект уже загружен.
        Для удаления только по ID без предварительного SELECT — см. delete_document_by_id.
        """
        document_id = document.id
        keys_to_delete = _document_storage_keys(
            document.storage_key,
            getattr(document, "original_storage_key", None),
            getattr(document, "exported_storage_key", None),
        )

        async with self._uow:
            keys_to_delete += await self._uow.documents.delete_document_scoped_sources(document_id)
            await self._uow.documents.delete(document)
            keys_to_delete += await self._unreferenced_snapshot_keys(document.project_id)
            await self._uow.commit()

        await self._delete_storage_objects(
            keys_to_delete,
            document_id,
            "Не удалось удалить файл из хранилища после удаления документа",
        )

    async def delete_document_by_id(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None:
        """Удалить документ проекта вместе с его собственными источниками.

        Порядок операций внутри транзакции:
          1. delete_document_scoped_sources — удаляем orphan-источники scope=DOCUMENT
             ДО flush документа, пока document_sources ещё существуют (подзапрос их читает).
          2. delete_by_id — DELETE FROM documents RETURNING ключи файлов.
             FK-каскад по document_sources срабатывает здесь.
          3. commit() — единственный коммит на операцию.
        После коммита best-effort удаление файлов из хранилища
        (storage_key, original_storage_key, exported_storage_key) и файлов
        собственных источников документа.

        DocumentNotFoundError — документа нет в проекте;
        InvalidDocumentStatusError — документ сейчас анализируется.
        """
        async with self._uow:
            document = await self._uow.documents.get_by_id(document_id)
            if document is None or document.project_id != project_id:
                raise DocumentNotFoundError(
                    f"Документ {document_id} не найден в проекте {project_id}"
                )
            if not DocumentLifecycle.can_delete(document.status):
                raise InvalidDocumentStatusError(
                    "Нельзя удалить документ, пока идёт анализ. Сначала отмените задачу."
                )
            source_keys = await self._uow.documents.delete_document_scoped_sources(document_id)
            deleted = await self._uow.documents.delete_by_id(
                document_id=document_id,
                project_id=project_id,
            )
            if deleted is None:
                raise DocumentNotFoundError(
                    f"Документ {document_id} не найден в проекте {project_id}"
                )
            keys_to_delete = _document_storage_keys(
                deleted.get("storage_key"),
                deleted.get("original_storage_key"),
                deleted.get("exported_storage_key"),
            )
            keys_to_delete += source_keys
            keys_to_delete += await self._unreferenced_snapshot_keys(project_id)
            await self._uow.commit()

        await self._delete_storage_objects(
            keys_to_delete,
            document_id,
            "Не удалось удалить файл из хранилища (delete_by_id)",
        )

    async def get_download_url(self, document: Document) -> tuple[str, int]:
        expires_in = self._download_url_ttl_seconds
        url = await self._storage.get_presigned_url(
            document.storage_key, expires_in, download_name=document.name
        )
        return url, expires_in

    async def get_original_file(self, document: Document) -> bytes:
        """Байты исходного файла для просмотра в браузере (режим «Оригинал»).

        Выбор ключа тот же, что у get_original_content: original_storage_key, если он когда-нибудь
        начнёт отличаться от storage_key.
        """
        original_key: str | None = getattr(document, "original_storage_key", None)
        return await self._storage.download(original_key or document.storage_key)

    async def get_document_content(self, document: Document) -> ParsedDocument:
        return (await self.get_document_layout(document)).document

    async def get_document_layout(self, document: Document) -> DocumentLayout:
        """Разбор вместе с размерами страниц; у PDF берётся из кэша рядом с файлом."""
        return await self._read_layout(document.storage_key)

    async def get_editor_layouts(
        self, document: Document, *, with_original: bool
    ) -> tuple[DocumentLayout, DocumentLayout | None]:
        """Текущий разбор и разбор оригинала для редактора.

        Пока оригинал лежит под тем же ключом, разбор один: два параллельных чтения PDF
        без кэша разбирали бы один файл дважды.
        """
        current = document.storage_key
        original = getattr(document, "original_storage_key", None) or current
        if not with_original:
            return await self._read_layout(current), None
        if original == current:
            layout = await self._read_layout(current)
            return layout, layout
        current_layout, original_layout = await asyncio.gather(
            self._read_layout(current), self._read_layout(original)
        )
        return current_layout, original_layout

    async def get_original_content(self, document: Document) -> ParsedDocument:
        """Режим «Оригинал» (#7): вернуть текст до правок."""
        original_key: str | None = getattr(document, "original_storage_key", None)
        return (await self._read_layout(original_key or document.storage_key)).document

    async def _read_layout(self, storage_key: str) -> DocumentLayout:
        raw_bytes = await self._storage.download(storage_key)
        if self._layout_reader is None:
            parsed = await self._parse_pool.run(
                self._parser_registry.parse_by_filename, storage_key, raw_bytes
            )
            return DocumentLayout(parsed)
        return await self._layout_reader.read(storage_key, raw_bytes)
