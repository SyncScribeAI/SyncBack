"""Источники: создание в обеих областях, изоляция по проекту, блокировка на анализе, удаление."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.enums import DocumentStatus
from tests.contract.conftest import (
    InMemoryFileStorage,
    create_project,
    register_and_login,
    upload_document,
)

pytestmark = pytest.mark.asyncio


async def _set_status(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: str, status: DocumentStatus
) -> None:
    async with sessionmaker() as session:
        await session.execute(
            update(Document).where(Document.id == uuid.UUID(document_id)).values(status=status)
        )
        await session.commit()


async def _create_url(
    client: AsyncClient, headers: dict[str, str], project_id: str, **extra: object
) -> dict:
    body: dict[str, object] = {"name": "Док", "type": "url", "url": "https://example.com/spec"}
    body.update(extra)
    response = await client.post(
        f"/api/v1/projects/{project_id}/sources", json=body, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_sources_of_all_kinds_and_list(
    client: AsyncClient, file_storage: InMemoryFileStorage
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    base = f"/api/v1/projects/{project_id}/sources"

    url_source = await _create_url(client, headers, project_id)
    assert url_source["type"] == "url"
    assert url_source["scope"] == "project"

    response = await client.post(
        f"{base}/note", json={"name": "Заметка", "text_content": "важно"}, headers=headers
    )
    assert response.status_code == 201, response.text
    note = response.json()
    assert note["type"] == "file"
    assert file_storage.files[f"projects/{project_id}/sources/{note['id']}.txt"] == "важно".encode()

    response = await client.post(
        f"{base}/file",
        data={"name": "Файл"},
        files={"file": ("../../etc/passwd.txt", b"root", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    file_source = response.json()
    assert file_storage.files[f"projects/{project_id}/sources/{file_source['id']}.txt"] == b"root"

    response = await client.get(base, headers=headers)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 3
    # Порядок по created_at в SQLite с точностью до секунды не проверить, сравниваем множества
    all_ids = {file_source["id"], note["id"], url_source["id"]}
    assert {s["id"] for s in page["items"]} == all_ids

    response = await client.get(base, params={"limit": 1, "offset": 1}, headers=headers)
    assert response.json()["total"] == 3
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["id"] in all_ids

    # Источники не видны из другого проекта
    other_project = await create_project(client, headers, name="Другой")
    response = await client.get(f"/api/v1/projects/{other_project}/sources", headers=headers)
    assert response.json()["total"] == 0


async def test_document_scope_attaches_to_own_document_only(
    client: AsyncClient, file_storage: InMemoryFileStorage
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = await upload_document(client, headers, project_id)
    base = f"/api/v1/projects/{project_id}/sources"

    doc_source = await _create_url(
        client, headers, project_id, scope="document", document_id=document["id"]
    )
    assert doc_source["scope"] == "document"

    response = await client.post(
        f"{base}/file",
        data={"name": "Файл", "scope": "document", "document_id": document["id"]},
        files={"file": ("notes.txt", b"x", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    doc_file = response.json()

    # Документные источники видны в карточке документа, проектные — в списке проекта
    response = await client.get(
        f"/api/v1/projects/{project_id}",
        params={"include": ["documents", "sources"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sources"] == []
    assert {s["id"] for s in body["documents"][0]["sources"]} == {doc_source["id"], doc_file["id"]}

    response = await client.get(base, params={"scope": "document"}, headers=headers)
    assert response.json()["total"] == 2
    response = await client.get(base, params={"scope": "project"}, headers=headers)
    assert response.json()["total"] == 0

    # Чужой документ (другой проект того же пользователя) — 404, запись не создаётся
    other_project = await create_project(client, headers, name="Другой")
    foreign_document = await upload_document(client, headers, other_project)
    response = await client.post(
        base,
        json={
            "name": "x",
            "type": "url",
            "url": "https://example.com",
            "scope": "document",
            "document_id": foreign_document["id"],
        },
        headers=headers,
    )
    assert response.status_code == 404, response.text
    response = await client.post(
        f"{base}/file",
        data={"name": "Файл", "scope": "document", "document_id": foreign_document["id"]},
        files={"file": ("notes.txt", b"orphan", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert b"orphan" not in file_storage.files.values()
    assert (await client.get(base, headers=headers)).json()["total"] == 2

    # scope=document без document_id и невалидный UUID в форме — 422
    response = await client.post(
        base,
        json={"name": "x", "type": "url", "url": "https://example.com", "scope": "document"},
        headers=headers,
    )
    assert response.status_code == 422
    response = await client.post(
        f"{base}/file",
        data={"name": "Файл", "scope": "document", "document_id": "not-a-uuid"},
        files={"file": ("notes.txt", b"x", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 422
    response = await client.post(
        f"{base}/file",
        data={"name": "Файл", "scope": "document"},
        files={"file": ("notes.txt", b"x", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 422


async def test_sources_are_locked_while_document_is_analysed(
    client: AsyncClient,
    file_storage: InMemoryFileStorage,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    document = await upload_document(client, headers, project_id)
    base = f"/api/v1/projects/{project_id}/sources"
    attached = await _create_url(
        client, headers, project_id, scope="document", document_id=document["id"]
    )
    project_source = await _create_url(client, headers, project_id)

    await _set_status(sessionmaker, document["id"], DocumentStatus.IN_PROGRESS)

    response = await client.post(
        base,
        json={
            "name": "x",
            "type": "url",
            "url": "https://example.com",
            "scope": "document",
            "document_id": document["id"],
        },
        headers=headers,
    )
    assert response.status_code == 423, response.text
    response = await client.post(
        f"{base}/note",
        json={"name": "n", "text_content": "t", "scope": "document", "document_id": document["id"]},
        headers=headers,
    )
    assert response.status_code == 423, response.text
    # Заметка для заблокированного документа не попала в хранилище: там только сам документ
    assert list(file_storage.files) == [f"projects/{project_id}/documents/{document['id']}.txt"]

    assert (await client.delete(f"{base}/{attached['id']}", headers=headers)).status_code == 423
    # Базовый источник проекта к документу не привязан — удаляется
    assert (
        await client.delete(f"{base}/{project_source['id']}", headers=headers)
    ).status_code == 204

    # Блокировка только на время анализа: на ревью источник удаляется, даже если правки
    # по нему ещё не рассмотрены — они остаются до следующего анализа.
    await _set_status(sessionmaker, document["id"], DocumentStatus.AWAITING_APPROVAL)
    assert (await client.delete(f"{base}/{attached['id']}", headers=headers)).status_code == 204
    assert (await client.delete(f"{base}/{attached['id']}", headers=headers)).status_code == 404


async def test_delete_source_removes_file_and_is_scoped_to_project(
    client: AsyncClient, file_storage: InMemoryFileStorage
) -> None:
    headers = await register_and_login(client)
    project_id = await create_project(client, headers)
    base = f"/api/v1/projects/{project_id}/sources"
    response = await client.post(
        f"{base}/note", json={"name": "Заметка", "text_content": "t"}, headers=headers
    )
    note = response.json()
    key = f"projects/{project_id}/sources/{note['id']}.txt"
    assert key in file_storage.files

    other_project = await create_project(client, headers, name="Другой")
    response = await client.delete(
        f"/api/v1/projects/{other_project}/sources/{note['id']}", headers=headers
    )
    assert response.status_code == 404
    assert key in file_storage.files

    other_headers = await register_and_login(client, email="intruder@example.com")
    response = await client.delete(f"{base}/{note['id']}", headers=other_headers)
    assert response.status_code == 404
    assert key in file_storage.files

    assert (await client.delete(f"{base}/{note['id']}", headers=headers)).status_code == 204
    assert key not in file_storage.files
    assert (await client.delete(f"{base}/{note['id']}", headers=headers)).status_code == 404
    assert (await client.delete(f"{base}/{uuid.uuid4()}", headers=headers)).status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/",
        "ftp://example.com/file",
        "http://user:pass@example.com/",
    ],
)
async def test_unsafe_source_url_is_rejected(client: AsyncClient, url: str) -> None:
    headers = await register_and_login(client, f"ssrf-{abs(hash(url))}@example.com")
    project_id = await create_project(client, headers)

    response = await client.post(
        f"/api/v1/projects/{project_id}/sources",
        headers=headers,
        json={"name": "x", "type": "url", "url": url},
    )

    assert response.status_code == 422, response.text
