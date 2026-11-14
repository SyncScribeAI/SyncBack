"""Курсорная выдача задач анализа при совпадающем created_at."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.value_objects import KeysetPage
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.enums import AnalysisJobStatus
from app.infrastructure.db.repositories.analysis_job_repository import AnalysisJobRepository
from tests.contract.conftest import create_project, register_and_login, upload_document

pytestmark = pytest.mark.asyncio


async def test_keyset_pages_cover_jobs_with_equal_created_at_once(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document_id = uuid.UUID((await upload_document(client, headers, project_id))["id"])

    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    # SQLite обходит индекс (document_id, created_at) с конца, и без id в ORDER BY
    # совпадения выходят в обратном порядке вставки. Вставка по убыванию id даёт
    # возрастающий порядок, на котором курсор по id теряет и повторяет задачи.
    job_ids = sorted((uuid.uuid4() for _ in range(7)), reverse=True)
    async with sessionmaker() as session:
        session.add_all(
            AnalysisJob(
                id=job_id,
                document_id=document_id,
                status=AnalysisJobStatus.SUCCESS,
                created_at=created_at,
            )
            for job_id in job_ids
        )
        await session.commit()

    seen: list[uuid.UUID] = []
    page = KeysetPage(limit=3)
    async with sessionmaker() as session:
        repo = AnalysisJobRepository(session)
        for _ in range(len(job_ids)):
            rows = await repo.list_by_document(document_id, page)
            seen.extend(row.id for row in rows)
            if len(rows) < page.limit:
                break
            page = KeysetPage(limit=3, before_created_at=rows[-1].created_at, before_id=rows[-1].id)

    assert seen == job_ids
