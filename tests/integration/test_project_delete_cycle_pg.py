"""Regression: deleting a project whose documents reference a current analysis job.

Before the fix, session.delete(project) + flush raised CircularDependencyError because
Document.current_analysis_job_id and AnalysisJob.document_id point at each other.
Runs on migrated PostgreSQL (integration suite); file storage is an in-memory recorder.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.services.project_service import ProjectService
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.enums import AnalysisJobStatus, ChangeType, DocumentFormat
from app.infrastructure.db.models.project import Project
from app.infrastructure.db.models.suggestion import Suggestion
from app.infrastructure.db.models.user import User
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork


class RecordingStorage:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _seed_project(
    sessionmaker: async_sessionmaker[AsyncSession], last_status: AnalysisJobStatus
) -> tuple[uuid.UUID, list[uuid.UUID], list[str]]:
    """Project with two documents; each has a failed job, a final job and current_analysis_job."""
    async with sessionmaker() as session:
        user = User(email=f"it-{uuid.uuid4().hex}@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        project = Project(name="delete-cycle", owner_id=user.id)
        session.add(project)
        await session.flush()
        document_ids: list[uuid.UUID] = []
        keys: list[str] = []
        for number in range(2):
            prefix = f"it/{project.id}/{number}"
            document = Document(
                project_id=project.id,
                name=f"doc-{number}.txt",
                format=DocumentFormat.TXT,
                storage_key=f"{prefix}.txt",
                original_storage_key=f"{prefix}-original.docx",
                size_bytes=1,
                uploaded_at=datetime.now(UTC),
            )
            session.add(document)
            await session.flush()
            document_ids.append(document.id)
            keys.extend([document.storage_key, document.original_storage_key])
            first = AnalysisJob(document_id=document.id, status=AnalysisJobStatus.FAILED)
            last = AnalysisJob(document_id=document.id, status=last_status)
            session.add_all([first, last])
            await session.flush()
            document.current_analysis_job_id = last.id
            for job in (first, last):
                session.add(
                    Suggestion(
                        analysis_job_id=job.id,
                        document_id=document.id,
                        section_ref="p1",
                        change_type=ChangeType.MODIFY,
                        original_text="old",
                        suggested_text="new",
                    )
                )
        await session.commit()
        return project.id, document_ids, keys


async def _count(
    sessionmaker: async_sessionmaker[AsyncSession], model: type, column, ids: list[uuid.UUID]
) -> int:
    async with sessionmaker() as session:
        stmt = select(func.count()).select_from(model).where(column.in_(ids))
        return (await session.execute(stmt)).scalar_one()


@pytest.mark.parametrize(
    "last_status",
    [AnalysisJobStatus.SUCCESS, AnalysisJobStatus.FAILED, AnalysisJobStatus.CANCELLED],
)
async def test_delete_project_with_current_analysis_job(
    pg_sessionmaker: async_sessionmaker[AsyncSession], last_status: AnalysisJobStatus
) -> None:
    project_id, document_ids, keys = await _seed_project(pg_sessionmaker, last_status)
    other_id, other_documents, other_keys = await _seed_project(
        pg_sessionmaker, AnalysisJobStatus.SUCCESS
    )
    storage = RecordingStorage()

    async with pg_sessionmaker() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        await ProjectService(SqlAlchemyUnitOfWork(session), storage).delete_project(project)

    async with pg_sessionmaker() as session:
        assert await session.get(Project, project_id) is None
        assert await session.get(Project, other_id) is not None
    assert await _count(pg_sessionmaker, Document, Document.id, document_ids) == 0
    assert await _count(pg_sessionmaker, AnalysisJob, AnalysisJob.document_id, document_ids) == 0
    assert await _count(pg_sessionmaker, Suggestion, Suggestion.document_id, document_ids) == 0

    assert await _count(pg_sessionmaker, Document, Document.id, other_documents) == 2
    assert await _count(pg_sessionmaker, AnalysisJob, AnalysisJob.document_id, other_documents) == 4
    assert await _count(pg_sessionmaker, Suggestion, Suggestion.document_id, other_documents) == 4

    assert sorted(storage.deleted) == sorted(keys)
    assert not set(storage.deleted) & set(other_keys)
