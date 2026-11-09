"""GET /editor — агрегат экрана редактора на реальном стеке."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.enums import (
    AnalysisJobStatus,
    ChangeType,
    DocumentStatus,
    SuggestionStatus,
)
from app.infrastructure.db.models.suggestion import Suggestion
from tests.contract.conftest import create_project, register_and_login, upload_document

pytestmark = pytest.mark.asyncio


def _editor_url(project_id: str, document_id: str) -> str:
    return f"/api/v1/projects/{project_id}/documents/{document_id}/editor"


async def _finish_analysis(
    sessionmaker: async_sessionmaker[AsyncSession],
    document_id: str,
    statuses: list[SuggestionStatus],
) -> None:
    doc_id = uuid.UUID(document_id)
    async with sessionmaker() as session:
        job = AnalysisJob(document_id=doc_id, status=AnalysisJobStatus.SUCCESS)
        session.add(job)
        await session.flush()
        session.add_all(
            Suggestion(
                analysis_job_id=job.id,
                document_id=doc_id,
                section_ref=f"p{i}",
                change_type=ChangeType.MODIFY,
                status=status,
                original_text=f"old {i}",
                suggested_text=f"new {i}",
            )
            for i, status in enumerate(statuses)
        )
        await session.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(status=DocumentStatus.AWAITING_APPROVAL, current_analysis_job_id=job.id)
        )
        await session.commit()


async def test_editor_for_draft_document(client: AsyncClient) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = await upload_document(client, headers, project_id, content=b"Alpha\nBeta\n")

    response = await client.get(_editor_url(project_id, document["id"]), headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document"]["title"] == "spec.txt"
    assert body["document"]["status"] == "draft"
    assert body["document"]["view_mode"] == "original"
    assert "Alpha" in body["content"]["plain_text"]
    # Текста в блоке нет: фронт берёт его из plain_text по смещениям.
    assert body["content"]["blocks"] == [
        {
            "ref": "p-0",
            "type": "paragraph",
            "start_offset": 0,
            "end_offset": 5,
            "editable": True,
            "heading_level": None,
        },
        {
            "ref": "p-1",
            "type": "paragraph",
            "start_offset": 6,
            "end_offset": 10,
            "editable": True,
            "heading_level": None,
        },
    ]
    # Для черновика оригинал совпадает с содержимым: отдельного файла нет.
    assert body["original_content"] == body["content"]
    assert body["suggestions"] == []
    assert body["counters"] == {"total": 0, "pending": 0, "accepted": 0, "rejected": 0}
    assert body["permissions"] == {
        "can_analyze": True,
        "can_review": False,
        "can_export": False,
        "can_delete": True,
        "sources_is_editable": True,
    }


async def test_editor_with_review_results_and_pagination(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = await upload_document(client, headers, project_id)
    await _finish_analysis(
        sessionmaker,
        document["id"],
        [SuggestionStatus.PENDING, SuggestionStatus.PENDING, SuggestionStatus.ACCEPTED],
    )

    response = await client.get(
        _editor_url(project_id, document["id"]) + "?suggestions_limit=2", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document"]["status"] == "awaiting_approval"
    assert body["document"]["view_mode"] == "suggested"
    assert len(body["suggestions"]) == 2
    assert body["suggestions_total"] == 3
    assert body["counters"] == {"total": 3, "pending": 2, "accepted": 1, "rejected": 0}
    assert body["original_content"]["plain_text"] == body["content"]["plain_text"]
    assert body["permissions"]["can_review"] is True
    # Источники заблокированы только во время анализа; на ревью их можно менять.
    assert body["permissions"]["sources_is_editable"] is True


async def test_editor_not_found_and_foreign_project(client: AsyncClient) -> None:
    headers = await register_and_login(client, "owner@example.com")
    project_id = await create_project(client, headers)
    document = await upload_document(client, headers, project_id)

    response = await client.get(_editor_url(project_id, str(uuid.uuid4())), headers=headers)
    assert response.status_code == 404

    stranger = await register_and_login(client, "stranger@example.com")
    response = await client.get(_editor_url(project_id, document["id"]), headers=stranger)
    assert response.status_code == 404
