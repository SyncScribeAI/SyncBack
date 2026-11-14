"""PDF как источник: проверка при загрузке, белый список форматов, просмотр текста."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.domain.interfaces.source_connector import SourceKind, SourceRef
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.source_connectors.manual_upload_connector import ManualUploadConnector
from tests.contract.conftest import InMemoryFileStorage, create_project, register_and_login

pytestmark = pytest.mark.asyncio

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pdf"


async def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    project_id: str,
    name: str,
    payload: bytes,
    content_type: str = "application/pdf",
):
    return await client.post(
        f"/api/v1/projects/{project_id}/sources/file",
        data={"name": name},
        files={"file": (name, payload, content_type)},
        headers=headers,
    )


async def test_pdf_source_is_accepted_and_previewed(
    client: AsyncClient, file_storage: InMemoryFileStorage
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)

    response = await _upload(
        client, headers, project_id, "table.pdf", (_FIXTURES / "table.pdf").read_bytes()
    )

    assert response.status_code == 201, response.text
    assert response.json()["pages_without_text"] is None
    content = await client.get(
        f"/api/v1/projects/{project_id}/sources/{response.json()['id']}/content", headers=headers
    )
    assert content.json()["preview_available"] is True
    assert "| timeout 1 | 50 | Время ожидания ответа |" in content.json()["plain_text"]


async def test_pages_without_text_are_reported(
    client: AsyncClient, file_storage: InMemoryFileStorage
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)

    response = await _upload(
        client, headers, project_id, "mixed.pdf", (_FIXTURES / "text_then_image.pdf").read_bytes()
    )

    assert response.status_code == 201, response.text
    assert response.json()["pages_without_text"] == [2]
    assert len(file_storage.files) == 1


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [("image_only.pdf", "текстового слоя"), ("encrypted.pdf", "паролем")],
)
async def test_unreadable_pdf_is_rejected_before_storage(
    client: AsyncClient, file_storage: InMemoryFileStorage, fixture: str, reason: str
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)

    response = await _upload(
        client, headers, project_id, fixture, (_FIXTURES / fixture).read_bytes()
    )

    assert response.status_code == 422, response.text
    assert reason in response.json()["detail"]
    assert "DOCX" in response.json()["detail"]
    assert file_storage.files == {}


async def test_unknown_binary_source_is_rejected(
    client: AsyncClient, file_storage: InMemoryFileStorage
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)

    response = await _upload(
        client, headers, project_id, "tool.exe", b"MZ\x00\x01", "application/octet-stream"
    )

    assert response.status_code == 415, response.text
    assert ".pdf" in response.json()["detail"]
    assert file_storage.files == {}


async def test_capabilities_list_source_extensions(client: AsyncClient) -> None:
    upload = (await client.get("/api/v1/system/capabilities")).json()["upload"]
    assert upload["source_extensions"] == [
        ".docx",
        ".htm",
        ".html",
        ".markdown",
        ".md",
        ".pdf",
        ".txt",
    ]


async def test_pdf_source_reaches_analysis_as_text_with_rects() -> None:
    storage = InMemoryFileStorage()
    key = "projects/p/sources/s/table.pdf"
    storage.files[key] = (_FIXTURES / "table.pdf").read_bytes()
    connector = ManualUploadConnector(storage, DocumentParserRegistry())
    ref = SourceRef(
        id=uuid.uuid4(),
        name="table.pdf",
        type=SourceKind.FILE,
        storage_key=key,
        url=None,
        uploaded_at=datetime.now(UTC),
    )

    layout = await connector.fetch_layout(ref)

    assert "| Параметр | Значение | Комментарий |" in layout.text
    assert {span.locator["kind"] for span in layout.spans} == {"pdf_rects"}
