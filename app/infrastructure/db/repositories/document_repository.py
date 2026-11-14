from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, and_, delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.interfaces.repositories import DocumentRow, IDocumentRepository
from app.domain.value_objects import (
    DocumentFormatVO,
    DocumentStatusVO,
    KeysetPage,
    PaginationParams,
    SourceScopeVO,
)
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.document_source import document_sources
from app.infrastructure.db.models.enums import DocumentFormat, DocumentStatus, SuggestionStatus
from app.infrastructure.db.models.project import Project
from app.infrastructure.db.models.source import Source
from app.infrastructure.db.models.suggestion import Suggestion

_SORT_COLUMNS = frozenset({"created_at", "updated_at", "name"})


def _status_to_orm(vo: DocumentStatusVO) -> DocumentStatus:
    return DocumentStatus(vo.value)


class DocumentRepository(IDocumentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        id: uuid.UUID,
        project_id: uuid.UUID,
        name: str,
        format: DocumentFormatVO,
        storage_key: str,
        size_bytes: int,
    ) -> Document:
        doc = Document(
            id=id,
            project_id=project_id,
            name=name,
            format=DocumentFormat(format.value),
            storage_key=storage_key,
            size_bytes=size_bytes,
            uploaded_at=datetime.now(UTC),
            status=DocumentStatus.DRAFT,
        )
        self._session.add(doc)
        await self._session.flush()
        return doc

    async def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        return await self._session.get(Document, document_id)

    async def get_many_by_ids(self, document_ids: list[uuid.UUID]) -> list[Document]:
        if not document_ids:
            return []
        result = await self._session.execute(select(Document).where(Document.id.in_(document_ids)))
        return list(result.scalars().all())

    async def list_for_project(
        self,
        project_id: uuid.UUID,
        pagination: KeysetPage | PaginationParams,
        *,
        status: DocumentStatusVO | None = None,
    ) -> list[Document]:
        q = (
            select(Document)
            .where(Document.project_id == project_id)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(pagination.limit)
        )

        if status is not None:
            q = q.where(Document.status == _status_to_orm(status))

        if isinstance(pagination, KeysetPage) and pagination.has_cursor:
            q = q.where(
                tuple_(Document.created_at, Document.id)
                < tuple_(pagination.before_created_at, pagination.before_id)
            )
        elif isinstance(pagination, PaginationParams):
            q = q.offset(pagination.offset)

        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def count_for_project(
        self,
        project_id: uuid.UUID,
        *,
        status: DocumentStatusVO | None = None,
    ) -> int:
        q = select(func.count()).select_from(Document).where(Document.project_id == project_id)
        if status is not None:
            q = q.where(Document.status == _status_to_orm(status))
        result = await self._session.execute(q)
        return result.scalar_one()

    async def list_ids_by_statuses(
        self, project_id: uuid.UUID, statuses: frozenset[DocumentStatusVO]
    ) -> list[uuid.UUID]:
        if not statuses:
            return []
        result = await self._session.execute(
            select(Document.id)
            .where(
                Document.project_id == project_id,
                Document.status.in_(tuple(_status_to_orm(s) for s in statuses)),
            )
            .order_by(Document.created_at.asc(), Document.id.asc())
        )
        return list(result.scalars().all())

    async def get_stats_for_project(
        self,
        project_id: uuid.UUID,
    ) -> dict[str, int]:
        stat_cols = [
            func.count().filter(Document.status == _status_to_orm(vo)).label(vo.value)
            for vo in DocumentStatusVO
        ]
        stat_cols.append(func.count().label("total"))

        rows = await self._session.execute(
            select(*stat_cols).where(Document.project_id == project_id)
        )
        row = rows.one()
        result: dict[str, int] = {vo.value: getattr(row, vo.value) for vo in DocumentStatusVO}
        result["total"] = row.total
        return result

    async def list_all_for_user(
        self,
        user_id: uuid.UUID,
        limit: int,
        offset: int,
        status: DocumentStatusVO | None = None,
        outdated: bool = False,
        search: str | None = None,
        sort_by: str = "updated_at",
        sort_dir: str = "desc",
    ) -> tuple[list[DocumentRow], int]:
        """Документы всех проектов пользователя со счётчиками правок текущего анализа.

        Один запрос: агрегаты правок считаются в подзапросе по (document_id,
        analysis_job_id) и присоединяются по текущей задаче документа, общее число
        строк берётся оконным COUNT() OVER ().
        """
        if sort_by not in _SORT_COLUMNS:
            sort_by = "updated_at"

        def _count_with_status(status_value: SuggestionStatus) -> ColumnElement[int]:
            return func.count(Suggestion.id).filter(Suggestion.status == status_value)

        counts = (
            select(
                Suggestion.document_id.label("document_id"),
                Suggestion.analysis_job_id.label("analysis_job_id"),
                func.count(Suggestion.id).label("total"),
                _count_with_status(SuggestionStatus.PENDING).label("pending"),
                _count_with_status(SuggestionStatus.ACCEPTED).label("accepted"),
                _count_with_status(SuggestionStatus.REJECTED).label("rejected"),
            )
            .group_by(Suggestion.document_id, Suggestion.analysis_job_id)
            .subquery("suggestion_counts")
        )

        suggestions_total = func.coalesce(counts.c.total, 0).label("suggestions_total")
        suggestions_pending = func.coalesce(counts.c.pending, 0).label("suggestions_pending")
        suggestions_accepted = func.coalesce(counts.c.accepted, 0).label("suggestions_accepted")
        suggestions_rejected = func.coalesce(counts.c.rejected, 0).label("suggestions_rejected")

        stmt = (
            select(
                Document,
                Project.name.label("project_name"),
                suggestions_total,
                suggestions_pending,
                suggestions_accepted,
                suggestions_rejected,
                func.count().over().label("total_count"),
            )
            .join(Project, Document.project_id == Project.id)
            .outerjoin(
                counts,
                and_(
                    counts.c.document_id == Document.id,
                    counts.c.analysis_job_id == Document.current_analysis_job_id,
                ),
            )
            .where(Project.owner_id == user_id)
        )

        if status is not None:
            stmt = stmt.where(Document.status == _status_to_orm(status))

        if outdated:
            stmt = stmt.where(func.coalesce(counts.c.pending, 0) > 0)

        if search:
            safe_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(Document.name.ilike(f"%{safe_search}%", escape="\\"))

        sort_column = getattr(Document, sort_by)
        page_stmt = (
            stmt.order_by(
                sort_column.asc() if sort_dir == "asc" else sort_column.desc(),
                Document.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )

        rows = (await self._session.execute(page_stmt)).all()
        if not rows:
            # Страница за пределами выборки: оконный счётчик недоступен, total
            # нужен отдельным запросом, иначе клиент решит, что документов нет.
            total = await self._session.scalar(
                select(func.count()).select_from(stmt.with_only_columns(Document.id).subquery())
            )
            return [], total or 0

        items: list[DocumentRow] = [
            {
                "document": row[0],
                "project_name": row.project_name,
                "suggestions_total": row.suggestions_total,
                "suggestions_pending": row.suggestions_pending,
                "suggestions_accepted": row.suggestions_accepted,
                "suggestions_rejected": row.suggestions_rejected,
            }
            for row in rows
        ]
        return items, int(rows[0].total_count)

    async def update_status(
        self,
        document: Document,
        status: DocumentStatusVO,
    ) -> Document:
        document.status = _status_to_orm(status)
        await self._session.flush()
        return document

    async def compare_and_set_status(
        self,
        document_id: uuid.UUID,
        analysis_job_id: uuid.UUID | None,
        expected: DocumentStatusVO,
        target: DocumentStatusVO,
    ) -> bool:
        stmt = (
            update(Document)
            .where(
                Document.id == document_id,
                Document.status == _status_to_orm(expected),
                Document.current_analysis_job_id == analysis_job_id,
            )
            .values(status=_status_to_orm(target))
            .returning(Document)
        )
        # ORM-UPDATE с RETURNING обновляет и объект документа в сессии.
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def set_current_job(self, document: Document, job_id: uuid.UUID | None) -> Document:
        document.current_analysis_job_id = job_id
        await self._session.flush()
        return document

    async def update_exported_key(
        self,
        document: Document,
        export_key: str,
    ) -> None:
        document.exported_storage_key = export_key
        await self._session.flush()

    async def compare_and_increment_review_version(
        self,
        document_id: uuid.UUID,
        expected_version: int,
    ) -> Document | None:
        stmt = (
            update(Document)
            .where(
                Document.id == document_id,
                Document.review_version == expected_version,
            )
            .values(review_version=Document.review_version + 1)
            .returning(Document)
        )
        result = await self._session.execute(stmt)
        updated = result.scalar_one_or_none()
        if updated is None:
            return None
        await self._session.flush()
        return updated

    async def delete_document_scoped_sources(
        self,
        document_id: uuid.UUID,
    ) -> list[str]:
        """Удалить источники scope=DOCUMENT, привязанные к данному документу.

        Выполняется одним DELETE с подзапросом:

            DELETE FROM sources
             WHERE scope = 'document'
               AND id IN (
                   SELECT source_id FROM document_sources
                    WHERE document_id = :document_id
               )
             RETURNING storage_key

        Источники scope=PROJECT намеренно не трогаются: они принадлежат
        проекту и переживают удаление документа.

        Возвращает ключи файлов удалённых источников: после commit сервис удаляет
        их из хранилища, иначе содержимое источника пережило бы его запись.
        Должен вызываться внутри той же транзакции, что и delete / delete_by_id,
        ДО flush/commit, чтобы FK-каскад по document_sources не успел
        удалить строки раньше подзапроса.
        """
        subq = (
            select(document_sources.c.source_id)
            .where(document_sources.c.document_id == document_id)
            .scalar_subquery()
        )
        stmt = (
            delete(Source)
            .where(
                Source.scope == SourceScopeVO.DOCUMENT,
                Source.id.in_(subq),
            )
            .returning(Source.storage_key)
        )
        result = await self._session.execute(stmt)
        keys = [key for key in result.scalars() if key]
        await self._session.flush()
        return keys

    async def delete(self, document: Document) -> None:
        await self._session.delete(document)
        await self._session.flush()

    async def delete_by_id(
        self,
        document_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> dict[str, str | None] | None:
        """M-BLOCK: удалить документ без предварительного SELECT.

        Выполняет:
            DELETE FROM documents
             WHERE id = :document_id AND project_id = :project_id
             RETURNING storage_key, original_storage_key, exported_storage_key

        Возвращает dict с ключами 'storage_key', 'original_storage_key' и
        'exported_storage_key', если строка удалена, или None, если документ не
        найден / не принадлежит проекту.

        Примечание: источники scope=DOCUMENT должны быть удалены
        через delete_document_scoped_sources() ДО вызова этого метода
        (в той же транзакции), иначе FK-каскад по document_sources
        удалит join-строки раньше, чем подзапрос их прочитает.
        """
        stmt = (
            delete(Document)
            .where(
                Document.id == document_id,
                Document.project_id == project_id,
            )
            .returning(
                Document.storage_key,
                Document.original_storage_key,
                Document.exported_storage_key,
            )
        )
        result = await self._session.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return None
        await self._session.flush()
        return {
            "storage_key": row.storage_key,
            "original_storage_key": row.original_storage_key,
            "exported_storage_key": row.exported_storage_key,
        }
