"""SQLAlchemy-реализация ISourceSnapshotRepository."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.interfaces.repositories import ISourceSnapshotRepository
from app.domain.source_snapshot import (
    JobSourceRecord,
    JobSourceSnapshot,
    JobSourceStatus,
    SnapshotDraft,
    SourceSnapshot,
)
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.source_snapshot import (
    AnalysisJobSourceSnapshot,
    SourceSnapshotModel,
)


def _to_domain(row: SourceSnapshotModel) -> SourceSnapshot:
    return SourceSnapshot(
        id=row.id,
        project_id=row.project_id,
        content_hash=row.content_hash,
        storage_key=row.storage_key,
        chars=row.chars,
        segments=row.segments,
        created_at=row.created_at,
    )


class SourceSnapshotRepository(ISourceSnapshotRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_snapshots(self, drafts: Sequence[SnapshotDraft]) -> dict[str, uuid.UUID]:
        if not drafts:
            return {}
        project_ids = {draft.project_id for draft in drafts}
        if len(project_ids) != 1:
            raise ValueError("Снимки одной задачи принадлежат одному проекту")
        (project_id,) = project_ids
        # Конкурентная задача могла записать тот же текст: ON CONFLICT оставляет её строку,
        # а id читается отдельным запросом для всех хэшей сразу.
        stmt = (
            pg_insert(SourceSnapshotModel)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "project_id": draft.project_id,
                        "content_hash": draft.content_hash,
                        "storage_key": draft.storage_key,
                        "chars": draft.chars,
                        "segments": draft.segments,
                    }
                    for draft in drafts
                ]
            )
            .on_conflict_do_nothing(index_elements=["project_id", "content_hash"])
        )
        await self._session.execute(stmt)
        hashes = [draft.content_hash for draft in drafts]
        rows = await self._session.execute(
            select(SourceSnapshotModel.content_hash, SourceSnapshotModel.id).where(
                SourceSnapshotModel.project_id == project_id,
                SourceSnapshotModel.content_hash.in_(hashes),
            )
        )
        return {row.content_hash: row.id for row in rows}

    async def add_job_sources(
        self, job_id: uuid.UUID, records: Sequence[JobSourceRecord], ids: dict[str, uuid.UUID]
    ) -> None:
        self._session.add_all(
            AnalysisJobSourceSnapshot(
                job_id=job_id,
                position=record.position,
                source_id=record.source_id,
                snapshot_id=ids[record.content_hash] if record.content_hash else None,
                name=record.name[:512],
                kind=record.kind,
                file_storage_key=record.file_storage_key,
                status=record.status.value,
            )
            for record in records
        )
        await self._session.flush()

    async def list_job_sources(self, job_id: uuid.UUID) -> list[JobSourceSnapshot]:
        rows = await self._session.scalars(
            select(AnalysisJobSourceSnapshot)
            .where(AnalysisJobSourceSnapshot.job_id == job_id)
            .order_by(AnalysisJobSourceSnapshot.position)
        )
        return [
            JobSourceSnapshot(
                position=row.position,
                source_id=row.source_id,
                name=row.name,
                kind=row.kind,
                status=JobSourceStatus(row.status),
                snapshot=_to_domain(row.snapshot) if row.snapshot else None,
                file_storage_key=row.file_storage_key,
            )
            for row in rows
        ]

    async def get_in_project(
        self, project_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> SourceSnapshot | None:
        row = await self._session.scalar(
            select(SourceSnapshotModel).where(
                SourceSnapshotModel.id == snapshot_id,
                SourceSnapshotModel.project_id == project_id,
            )
        )
        return _to_domain(row) if row else None

    async def delete_unreferenced(self, project_id: uuid.UUID) -> list[str]:
        # Ссылка считается живой только через существующие задачу и документ: так проверка
        # не зависит от того, успел ли каскад удалить строки задачи в этой транзакции.
        referenced = (
            exists()
            .where(AnalysisJobSourceSnapshot.snapshot_id == SourceSnapshotModel.id)
            .where(AnalysisJob.id == AnalysisJobSourceSnapshot.job_id)
            .where(Document.id == AnalysisJob.document_id)
        )
        result = await self._session.execute(
            delete(SourceSnapshotModel)
            .where(SourceSnapshotModel.project_id == project_id, ~referenced)
            .returning(SourceSnapshotModel.storage_key)
        )
        return list(result.scalars().all())

    async def list_storage_keys(self, project_id: uuid.UUID) -> list[str]:
        result = await self._session.scalars(
            select(SourceSnapshotModel.storage_key).where(
                SourceSnapshotModel.project_id == project_id
            )
        )
        return list(result.all())
