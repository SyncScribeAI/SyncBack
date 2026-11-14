"""Источники истины проекта.

Базовые источники (scope=project) участвуют в анализе всех документов проекта,
документные (scope=document) прикрепляются к конкретному документу через M2M.
Файлы и заметки хранятся в файловом хранилище, в БД лежит только ключ.

Источники документа заморожены только пока идёт анализ (DocumentLifecycle.SOURCES_LOCKED).
После его завершения источник можно удалить даже при нерассмотренных правках: они
уже сохранены и от источника не зависят, а исчезнут только при следующем анализе.

Просмотр — текст файла через реестр парсеров. Скачивание исходного файла идёт через
SourceFileService: байты отдаются авторизованным API, публичный адрес хранилища не нужен.
Для url-источника открывается сам адрес.

Проверка источника загружает и разбирает его тем же коннектором, что и воркер, и
ничего не сохраняет: пользователь узнаёт об отказе сразу после добавления ссылки,
а не после запуска анализа.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain.exceptions import (
    DocumentNotFoundError,
    DomainError,
    FileTooLargeError,
    InvalidSourceScopeError,
    SourceLockError,
    SourceNotFoundError,
    UnsupportedFormatError,
)
from app.domain.interfaces.document_parser import IDocumentParserRegistry
from app.domain.interfaces.file_storage import FileStorage, UploadContent
from app.domain.interfaces.source_connector import SourceConnectorResolver, SourceKind, SourceRef
from app.domain.interfaces.source_file_inspector import SourceFileInspector
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.lifecycle import DocumentLifecycle
from app.domain.policies import UploadLimits
from app.domain.source_files import SourceFileReport, ensure_source_extension
from app.domain.source_url import DEFAULT_SOURCE_URL_POLICY, SourceUrl, SourceUrlPolicy
from app.domain.storage_keys import source_storage_key
from app.domain.value_objects import SourceScopeVO, SourceTypeVO

if TYPE_CHECKING:
    from app.infrastructure.db.models.document import Document
    from app.infrastructure.db.models.project import Project
    from app.infrastructure.db.models.source import Source

logger = logging.getLogger("syncscribe.sources")

_SOURCE_KINDS = {SourceTypeVO.FILE.value: SourceKind.FILE, SourceTypeVO.URL.value: SourceKind.URL}


@dataclass(frozen=True, slots=True)
class SourceCheckResult:
    """Итог пробной загрузки: ok и длина текста либо понятная причина отказа."""

    ok: bool
    chars: int
    error: str | None = None


class SourceService:
    def __init__(
        self,
        uow: IUnitOfWork,
        file_storage: FileStorage,
        upload_limits: UploadLimits,
        parser_registry: IDocumentParserRegistry | None = None,
        url_policy: SourceUrlPolicy = DEFAULT_SOURCE_URL_POLICY,
        connector_for: SourceConnectorResolver | None = None,
        max_source_chars: int | None = None,
        file_inspector: SourceFileInspector | None = None,
    ) -> None:
        self._uow = uow
        self._storage = file_storage
        self._upload_limits = upload_limits
        self._parser_registry = parser_registry
        self._url_policy = url_policy
        self._connector_for = connector_for
        self._max_source_chars = max_source_chars
        self._file_inspector = file_inspector

    @staticmethod
    def _assert_sources_mutable(document: Document) -> None:
        if not DocumentLifecycle.can_edit_sources(document.status):
            raise SourceLockError(
                f"Нельзя изменить источники документа в статусе '{document.status.value}'. "
                "Дождитесь завершения анализа."
            )

    async def _target_document(
        self, project_id: uuid.UUID, scope: SourceScopeVO, document_id: uuid.UUID | None
    ) -> Document | None:
        """Документ, к которому будет прикреплён новый источник (None для scope=project).

        Вызывается внутри открытого UoW. Документ должен принадлежать проекту
        и допускать изменение источников.
        """
        if scope is not SourceScopeVO.DOCUMENT:
            return None
        if document_id is None:
            raise InvalidSourceScopeError("Для источника со scope=document нужен document_id")
        document = await self._uow.documents.get_by_id(document_id)
        if document is None or document.project_id != project_id:
            raise DocumentNotFoundError(f"Документ {document_id} не найден в проекте {project_id}")
        self._assert_sources_mutable(document)
        return document

    async def attach_sources_to_document(
        self, document: Document, sources: list[Source]
    ) -> list[Source]:
        """Прикрепить источники проекта к документу (повтор безопасен).

        Источники должны быть уже проверены на принадлежность проекту документа
        (см. get_sources_for_project). Бросает SourceLockError, если документ
        сейчас анализируется.
        """
        self._assert_sources_mutable(document)
        async with self._uow:
            for source in sources:
                await self._uow.sources.attach_to_document(source.id, document.id)
            await self._uow.commit()
        return sources

    async def create_url_source(
        self,
        project: Project,
        name: str,
        url: str,
        scope: SourceScopeVO = SourceScopeVO.PROJECT,
        document_id: uuid.UUID | None = None,
    ) -> Source:
        source_url = SourceUrl.parse(url, self._url_policy)
        async with self._uow:
            document = await self._target_document(project.id, scope, document_id)
            source = await self._uow.sources.create_url(
                project_id=project.id, name=name, url=source_url.value, scope=scope
            )
            if document is not None:
                await self._uow.sources.attach_to_document(source.id, document.id)
            await self._uow.commit()
        return source

    async def create_note_source(
        self,
        project: Project,
        name: str,
        text_content: str,
        scope: SourceScopeVO = SourceScopeVO.PROJECT,
        document_id: uuid.UUID | None = None,
    ) -> Source:
        """Текстовая заметка сохраняется как .txt-файл, тип источника — file."""
        return await self._create_stored_source(
            project,
            name=name,
            filename="note.txt",
            content=UploadContent.from_bytes(
                text_content.encode("utf-8"), "text/plain; charset=utf-8"
            ),
            scope=scope,
            document_id=document_id,
            too_large_message=f"Текст превышает лимит {self._upload_limits.max_size_mb} МБ",
        )

    async def create_file_source(
        self,
        project: Project,
        name: str,
        filename: str,
        content: UploadContent,
        scope: SourceScopeVO = SourceScopeVO.PROJECT,
        document_id: uuid.UUID | None = None,
    ) -> tuple[Source, SourceFileReport]:
        """Файл проверяется до записи: отказ не оставляет в хранилище ничего."""
        ensure_source_extension(filename)
        if self._upload_limits.exceeded_by(content.size):
            raise FileTooLargeError(f"Файл превышает лимит {self._upload_limits.max_size_mb} МБ")
        report = SourceFileReport()
        if self._file_inspector is not None:
            report = await self._file_inspector.inspect(filename, content)
        source = await self._create_stored_source(
            project,
            name=name,
            filename=filename,
            content=content,
            scope=scope,
            document_id=document_id,
            too_large_message=f"Файл превышает лимит {self._upload_limits.max_size_mb} МБ",
        )
        return source, report

    async def _create_stored_source(
        self,
        project: Project,
        *,
        name: str,
        filename: str,
        content: UploadContent,
        scope: SourceScopeVO,
        document_id: uuid.UUID | None,
        too_large_message: str,
    ) -> Source:
        """Загрузить содержимое в хранилище и записать источник в одной транзакции.

        Если запись в БД не удалась, загруженный объект удаляется, чтобы в хранилище
        не оставалось файлов без владельца.
        """
        if self._upload_limits.exceeded_by(content.size):
            raise FileTooLargeError(too_large_message)

        source_id = uuid.uuid4()
        storage_key = source_storage_key(project.id, source_id, filename)
        uploaded = False
        try:
            async with self._uow:
                document = await self._target_document(project.id, scope, document_id)
                await self._storage.upload(storage_key, content)
                uploaded = True
                source = await self._uow.sources.create_with_id(
                    source_id=source_id,
                    project_id=project.id,
                    name=name,
                    source_type=SourceTypeVO.FILE,
                    storage_key=storage_key,
                    scope=scope,
                )
                if document is not None:
                    await self._uow.sources.attach_to_document(source.id, document.id)
                await self._uow.commit()
        except Exception:
            if uploaded:
                await self._storage.delete(storage_key)
            raise
        return source

    async def list_sources(
        self,
        project_id: uuid.UUID,
        limit: int,
        offset: int,
        scope: SourceScopeVO | None = None,
    ) -> tuple[list[Source], int]:
        async with self._uow:
            items = await self._uow.sources.list_by_project(
                project_id, limit=limit, offset=offset, scope=scope
            )
            total = await self._uow.sources.count_by_project(project_id, scope=scope)
        return items, total

    async def get_source(self, project_id: uuid.UUID, source_id: uuid.UUID) -> Source:
        async with self._uow:
            source = await self._uow.sources.get_by_id(source_id)
        if source is None or source.project_id != project_id:
            raise SourceNotFoundError(f"Источник {source_id} не найден в проекте {project_id}")
        return source

    async def get_sources_for_project(
        self, project_id: uuid.UUID, source_ids: list[uuid.UUID]
    ) -> list[Source]:
        async with self._uow:
            sources = await self._uow.sources.get_many_by_ids(source_ids)

        found_ids = {s.id for s in sources}
        missing = set(source_ids) - found_ids
        if missing:
            raise SourceNotFoundError(f"Источники не найдены: {missing}")

        foreign = [s.id for s in sources if s.project_id != project_id]
        if foreign:
            raise SourceNotFoundError(f"Источники не принадлежат проекту {project_id}: {foreign}")
        return sources

    async def list_sources_for_documents(
        self,
        project_id: uuid.UUID,
        document_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[Source]]:
        """Документные источники для набора документов: {document_id: [Source, ...]}."""
        if not document_ids:
            return {}
        async with self._uow:
            pairs = await self._uow.sources.list_by_document_ids(project_id, document_ids)
        result: dict[uuid.UUID, list[Source]] = defaultdict(list)
        for source, doc_id in pairs:
            result[doc_id].append(source)
        return dict(result)

    async def delete_source(self, project_id: uuid.UUID, source_id: uuid.UUID) -> None:
        """Удалить источник проекта.

        Бросает SourceNotFoundError, если источник не найден или чужой,
        и SourceLockError, если он прикреплён к документу, который сейчас
        анализируется. Уже созданные по этому источнику правки не трогаем:
        они остаются до следующего анализа. Файл в хранилище удаляется
        после фиксации транзакции; сбой удаления только логируется.
        """
        async with self._uow:
            source = await self._uow.sources.get_by_id(source_id)
            if source is None or source.project_id != project_id:
                raise SourceNotFoundError(f"Источник {source_id} не найден в проекте {project_id}")

            document_ids = await self._uow.sources.list_attached_document_ids(source_id)
            for document in await self._uow.documents.get_many_by_ids(document_ids):
                self._assert_sources_mutable(document)

            storage_key = source.storage_key
            await self._uow.sources.delete(source)
            await self._uow.commit()

        if storage_key:
            try:
                await self._storage.delete(storage_key)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Файл источника не удалён из хранилища",
                    exc_info=True,
                    extra={"source_id": str(source_id), "storage_key": storage_key},
                )

    # ------------------------------------------------------------------
    # Просмотр
    # ------------------------------------------------------------------

    async def get_source_text(self, source: Source) -> str:
        """Текст файлового источника для просмотра.

        UnsupportedFormatError — формат не разбирается (например, .doc); для url-источников
        текста нет — просмотр идёт по ссылке, см. роутер.
        """
        if not source.storage_key:
            raise UnsupportedFormatError("Источник-ссылка открывается по адресу, текста у него нет")
        if self._parser_registry is None:
            raise UnsupportedFormatError("Просмотр источников не настроен")
        raw_bytes = await self._storage.download(source.storage_key)
        return self._parser_registry.extract_text(source.storage_key, raw_bytes)

    # ------------------------------------------------------------------
    # Проверка
    # ------------------------------------------------------------------

    async def check_source(self, project_id: uuid.UUID, source_id: uuid.UUID) -> SourceCheckResult:
        """Загружает и разбирает источник, как это сделает воркер, ничего не сохраняя.

        Ожидаемые отказы (адрес вне списка, вход, размер, формат) возвращаются в error.
        Непредвиденная ошибка коннектора тоже даёт ok=False: ручка служит подсказкой,
        а подробности остаются в логе.
        """
        source = await self.get_source(project_id, source_id)
        if self._connector_for is None:
            raise UnsupportedFormatError("Проверка источников не настроена")
        kind = _SOURCE_KINDS.get(source.type.value)
        if kind is None:
            raise UnsupportedFormatError(f"Неизвестный тип источника: {source.type.value}")
        ref = SourceRef(
            id=source.id,
            name=source.name,
            type=kind,
            storage_key=source.storage_key,
            url=source.url,
            uploaded_at=source.created_at,
        )
        try:
            text = await self._connector_for(kind).fetch_structured(ref)
        except DomainError as exc:
            return SourceCheckResult(ok=False, chars=0, error=str(exc))
        except Exception:
            logger.exception(
                "Проверка источника завершилась ошибкой", extra={"source_id": str(source_id)}
            )
            return SourceCheckResult(ok=False, chars=0, error="Источник не удалось загрузить")
        chars = len(text)
        if self._max_source_chars is not None and chars > self._max_source_chars:
            return SourceCheckResult(
                ok=False,
                chars=chars,
                error=f"Источник больше лимита: {chars} символов при допустимых {self._max_source_chars}",
            )
        if not text.strip():
            return SourceCheckResult(ok=False, chars=0, error="В источнике не найден текст")
        return SourceCheckResult(ok=True, chars=chars)
