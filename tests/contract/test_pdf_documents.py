"""PDF как документ: загрузка, редактор с размерами страниц, кэш разбора, геометрия правок,
экспорт аннотациями."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.workers.tasks.analysis_v2_tasks as v2
from app.domain.interfaces.document_analyzer import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    ProposedChange,
    ReviewFlag,
)
from app.domain.storage_keys import document_layout_key
from app.domain.value_objects import DocumentFormatVO
from app.infrastructure.db.models.analysis_flag import AnalysisFlag
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.suggestion import Suggestion
from tests.contract.conftest import InMemoryFileStorage, create_project, register_and_login
from tests.contract.test_analysis_jobs import FakeAnalysisQueue, queue  # noqa: F401
from tests.contract.test_worker_pipeline_v2 import Worker, worker  # noqa: F401

pytestmark = pytest.mark.asyncio

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pdf"


def _pdf(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


async def _upload(client: AsyncClient, headers: dict[str, str], project_id: str, name: str):
    return await client.post(
        f"/api/v1/projects/{project_id}/documents",
        files={"file": (name, _pdf(name), "application/pdf")},
        headers=headers,
    )


async def _storage_key(sessionmaker: async_sessionmaker[AsyncSession], document_id: str) -> str:
    async with sessionmaker() as session:
        document = await session.get(Document, uuid.UUID(document_id))
        assert document is not None
        return document.storage_key


async def test_pdf_document_upload_and_editor_with_pages(
    client: AsyncClient,
    file_storage: InMemoryFileStorage,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)

    response = await _upload(client, headers, project_id, "headings_lists.pdf")

    assert response.status_code == 201, response.text
    document = response.json()
    assert document["format"] == "pdf"
    assert document["pages_without_text"] is None

    editor = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document['id']}/editor", headers=headers
    )
    assert editor.status_code == 200, editor.text
    content = editor.json()["content"]
    assert content["plain_text"].startswith("Руководство администратора")
    assert content["blocks"][0]["type"] == "heading"
    assert content["pages"][0] == {"number": 1, "width": 612.0, "height": 792.0}

    key = await _storage_key(sessionmaker, document["id"])
    assert document_layout_key(key) in file_storage.files

    plain = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document['id']}/content", headers=headers
    )
    assert plain.status_code == 200, plain.text
    assert plain.json()["plain_text"] == content["plain_text"]


async def test_pdf_document_reports_pages_without_text(client: AsyncClient) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)

    response = await _upload(client, headers, project_id, "text_then_image.pdf")

    assert response.status_code == 201, response.text
    assert response.json()["pages_without_text"] == [2]


@pytest.mark.parametrize("name", ["image_only.pdf", "encrypted.pdf"])
async def test_unreadable_pdf_document_is_rejected_before_storage(
    client: AsyncClient, file_storage: InMemoryFileStorage, name: str
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)

    response = await _upload(client, headers, project_id, name)

    assert response.status_code == 422, response.text
    assert file_storage.files == {}


async def test_deleting_pdf_document_removes_layout_cache(
    client: AsyncClient,
    file_storage: InMemoryFileStorage,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = (await _upload(client, headers, project_id, "headings_lists.pdf")).json()
    base = f"/api/v1/projects/{project_id}/documents/{document['id']}"
    await client.get(f"{base}/editor", headers=headers)
    key = await _storage_key(sessionmaker, document["id"])
    assert document_layout_key(key) in file_storage.files

    response = await client.delete(base, headers=headers)

    assert response.status_code == 204, response.text
    assert key not in file_storage.files
    assert document_layout_key(key) not in file_storage.files


def _change_and_flag(request: AnalysisRequest) -> AnalysisResult:
    paragraph = next(b for b in request.document.blocks if "тридцати дней" in b.text)
    heading = request.document.blocks[0]
    return AnalysisResult(
        status=AnalysisStatus.COMPLETED,
        suggestions=(
            ProposedChange(
                kind="modify",
                block_ref=paragraph.ref,
                old_text="тридцати дней",
                new_text="четырнадцати дней",
            ),
            ProposedChange(
                kind="add",
                block_ref=paragraph.ref,
                old_text=None,
                anchor_text="50 МБ",
                new_text=" (по умолчанию)",
            ),
        ),
        flags=(
            ReviewFlag(reason="source_conflict", block_ref=heading.ref, block_text=heading.text),
        ),
    )


async def test_worker_stores_geometry_of_pdf_suggestions_and_flags(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    worker.analyzer.respond = _change_and_flag
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = (await _upload(client, headers, project_id, "headings_lists.pdf")).json()
    await client.post(
        f"/api/v1/projects/{project_id}/sources/note",
        json={"name": "A", "text_content": "t", "scope": "project"},
        headers=headers,
    )
    started = await client.post(
        f"/api/v1/projects/{project_id}/documents/{document['id']}/analysis-jobs",
        headers=headers,
    )
    assert started.status_code == 201, started.text
    job_id, source_ids = queue.enqueued[-1]

    assert await v2.analyze_document(str(job_id), [str(s) for s in source_ids], 0) is None

    async with sessionmaker() as session:
        suggestions = list((await session.execute(select(Suggestion))).scalars())
        [flag] = list((await session.execute(select(AnalysisFlag))).scalars())
    assert len(suggestions) == 2
    for suggestion in suggestions:
        assert suggestion.geometry is not None
        rects = suggestion.geometry["rects"]
        assert rects and all(r[0] == 1 and r[1] < r[3] and r[2] < r[4] for r in rects)
    assert flag.geometry is not None and flag.geometry["rects"][0][0] == 1

    listed = await client.get(
        f"/api/v1/projects/{project_id}/documents/{document['id']}/suggestions", headers=headers
    )
    assert all(item["geometry"]["rects"] for item in listed.json()["items"])


async def test_geometry_failure_keeps_analysis_result(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker.analyzer.respond = _change_and_flag
    reader = v2.get_document_layout_reader()

    async def broken_locate(*args: object, **kwargs: object) -> None:
        raise RuntimeError("PDFium недоступен")

    monkeypatch.setattr(reader, "locate", broken_locate)
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = (await _upload(client, headers, project_id, "headings_lists.pdf")).json()
    await client.post(
        f"/api/v1/projects/{project_id}/sources/note",
        json={"name": "A", "text_content": "t", "scope": "project"},
        headers=headers,
    )
    await client.post(
        f"/api/v1/projects/{project_id}/documents/{document['id']}/analysis-jobs",
        headers=headers,
    )
    job_id, source_ids = queue.enqueued[-1]

    await v2.analyze_document(str(job_id), [str(s) for s in source_ids], 0)

    async with sessionmaker() as session:
        suggestions = list((await session.execute(select(Suggestion))).scalars())
    assert len(suggestions) == 2
    assert all(s.geometry is None for s in suggestions)


async def test_accepted_pdf_suggestions_are_exported_as_annotations(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
) -> None:
    worker.analyzer.respond = _change_and_flag
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = (await _upload(client, headers, project_id, "headings_lists.pdf")).json()
    base = f"/api/v1/projects/{project_id}/documents/{document['id']}"
    await client.post(
        f"/api/v1/projects/{project_id}/sources/note",
        json={"name": "A", "text_content": "t", "scope": "project"},
        headers=headers,
    )
    await client.post(f"{base}/analysis-jobs", headers=headers)
    job_id, source_ids = queue.enqueued[-1]
    await v2.analyze_document(str(job_id), [str(s) for s in source_ids], 0)
    items = (await client.get(f"{base}/suggestions", headers=headers)).json()["items"]

    finalized = await client.put(
        f"{base}/suggestions/review",
        json={
            "review_version": 0,
            "decisions": [{"suggestion_id": i["id"], "decision": "accepted"} for i in items],
            "finalize": True,
        },
        headers=headers,
    )
    assert finalized.status_code == 200, finalized.text
    exported = await client.get(f"{base}/export", params={"export_format": "pdf"}, headers=headers)

    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"] == "application/pdf"
    assert exported.content.startswith(_pdf("headings_lists.pdf"))
    reader = PdfReader(io.BytesIO(exported.content))
    annotations = [a.get_object() for a in reader.pages[0]["/Annots"]]
    assert sorted(a["/Subtype"] for a in annotations) == ["/Caret", "/StrikeOut"]
    assert {a["/Contents"] for a in annotations} == {"четырнадцати дней", " (по умолчанию)"}
