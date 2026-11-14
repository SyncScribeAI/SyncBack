"""
DocumentExportService — скачивает исходный файл из хранилища, применяет принятые
правки через DocumentExporter и возвращает готовые байты, имя файла и media type.

Принятые правки читаются страницами (_EXPORT_PAGE_SIZE), чтобы ограничить пик
памяти на документах с большим числом правок. Правки читаются напрямую через
uow.suggestions, а не через SuggestionService — иначе получается циклическая
зависимость сервисов.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.domain.document_geometry import rects_from_json
from app.domain.exceptions import InvalidDocumentStatusError, UnsupportedExportFormatError
from app.domain.interfaces.document_exporter import AppliedChange, DocumentExporterRegistry
from app.domain.interfaces.file_storage import FileStorage, UploadContent
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.lifecycle import DocumentLifecycle
from app.domain.value_objects import DocumentFormatVO, SuggestionStatusVO

if TYPE_CHECKING:
    from app.infrastructure.db.models.document import Document

_MEDIA_TYPES: dict[DocumentFormatVO, str] = {
    DocumentFormatVO.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    DocumentFormatVO.DOC: "application/msword",
    DocumentFormatVO.TXT: "text/plain",
    DocumentFormatVO.MARKDOWN: "text/markdown",
    DocumentFormatVO.PDF: "application/pdf",
}

# Размер страницы при обходе принятых правок: компромисс между числом
# round-trip к БД и пиком памяти.
_EXPORT_PAGE_SIZE: int = 500


def media_type_for(document_format: DocumentFormatVO) -> str:
    """MIME-тип файла документа по его формату (экспорт и отдача исходника)."""
    return _MEDIA_TYPES.get(document_format, "application/octet-stream")


class DocumentExportService:
    def __init__(
        self,
        uow: IUnitOfWork,
        file_storage: FileStorage,
        exporter_registry: DocumentExporterRegistry,
    ) -> None:
        self._uow = uow
        self._storage = file_storage
        self._exporters = exporter_registry

    async def export_document(
        self,
        document: Document,
        target_format: DocumentFormatVO | None = None,
    ) -> tuple[bytes, str, str]:
        """Вернуть (bytes, filename, media_type) документа с принятыми правками.

        Экспорт доступен только для документа в статусе ready, иначе
        InvalidDocumentStatusError. Формат — только исходный: конвертация не
        поддерживается, иной target_format → UnsupportedExportFormatError.
        """
        if not DocumentLifecycle.can_export(document.status):
            raise InvalidDocumentStatusError(
                f"Экспорт доступен только для готового документа, "
                f"текущий статус: {document.status.value}"
            )
        source_format = DocumentFormatVO(document.format)
        if target_format is not None and target_format != source_format:
            raise UnsupportedExportFormatError(
                f"Документ в формате {source_format.value} можно экспортировать только "
                f"в {source_format.value}, запрошен {target_format.value}"
            )
        exported_bytes = await self._apply_accepted_changes(document)
        return exported_bytes, document.name, media_type_for(source_format)

    async def export_and_save(self, document: Document) -> None:
        """Применить правки, сохранить результат в хранилище и записать exported_storage_key.

        Чтение правок и запись ключа идут в разных транзакциях: между ними
        загрузка в хранилище, и держать транзакцию открытой на время I/O нельзя.
        """
        exported_bytes = await self._apply_accepted_changes(document)
        media_type = media_type_for(DocumentFormatVO(document.format))

        export_key = f"{document.storage_key}.exported"
        await self._storage.upload(export_key, UploadContent.from_bytes(exported_bytes, media_type))

        async with self._uow:
            await self._uow.documents.update_exported_key(document, export_key)
            await self._uow.commit()

    async def _apply_accepted_changes(self, document: Document) -> bytes:
        """Исходный файл из хранилища с применёнными принятыми правками.

        Файл скачивается вне UoW: I/O хранилища не должен держать транзакцию БД.
        Экспортёр работает в отдельном потоке: сборка PDF или DOCX занимает CPU на секунды,
        и в потоке событий она остановила бы все запросы процесса API.
        """
        raw_bytes = await self._storage.download(document.storage_key)
        async with self._uow:
            changes = await self._iter_accepted_changes_pages(document)
        exporter = self._exporters.get_exporter(DocumentFormatVO(document.format))
        return await asyncio.to_thread(exporter.apply_changes, raw_bytes, changes)

    async def _iter_accepted_changes_pages(self, document: Document) -> list[AppliedChange]:
        """Курсорный обход принятых правок страницами по _EXPORT_PAGE_SIZE.

        ВАЖНО: метод НЕ открывает `async with self._uow`.
        Вызывающий обязан вызвать этот метод уже внутри открытого
        `async with self._uow` блока, иначе сессия не будет доступна.

        Причина: вложенные UoW-контексты используют одну сессию;
        исключение в этом методе вызвало бы rollback() через __aexit__
        вложенного контекстного менеджера, уничтожая состояние внешней
        транзакции.

        Останавливается когда:
          - страница пуста (данные закончились), или
          - последняя страница (количество записей < _EXPORT_PAGE_SIZE).
        """
        if document.current_analysis_job_id is None:
            return []

        changes: list[AppliedChange] = []
        offset = 0

        while True:
            page = await self._uow.suggestions.list_by_analysis_job_and_status_page(
                document.current_analysis_job_id,
                SuggestionStatusVO.ACCEPTED,
                limit=_EXPORT_PAGE_SIZE,
                offset=offset,
            )
            if not page:
                break
            # Ручная правка пользователя (edited_text) важнее текста модели.
            changes.extend(
                AppliedChange(
                    section_ref=s.section_ref,
                    change_type=s.change_type.value,
                    old_text=s.original_text,
                    new_text=s.edited_text or s.suggested_text,
                    start_offset=s.start_offset,
                    end_offset=s.end_offset,
                    rects=rects_from_json(s.geometry),
                )
                for s in page
            )
            if len(page) < _EXPORT_PAGE_SIZE:
                # Последняя страница — дальше данных нет.
                break
            offset += _EXPORT_PAGE_SIZE

        return changes
