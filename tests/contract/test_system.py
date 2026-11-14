"""Служебные эндпоинты: диагностика LLM и анализа только для администратора."""

import io
from pathlib import Path

import pytest

from docx import Document as DocxDocument
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.dependencies import get_analysis_queue_instance, get_pipeline_health_check
from app.domain.analysis_health import QueueProbe
from app.infrastructure.db.models.enums import UserRole
from app.infrastructure.db.models.user import User
from app.main import app
from tests.contract.conftest import register_and_login


async def _make_admin(sessionmaker: async_sessionmaker[AsyncSession], email: str) -> None:
    async with sessionmaker() as session:
        await session.execute(update(User).where(User.email == email).values(role=UserRole.ADMIN))
        await session.commit()
        assert (
            await session.scalar(select(User.role).where(User.email == email))
        ) == UserRole.ADMIN


async def test_llm_health_requires_admin(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    assert (await client.get("/api/v1/system/llm-health")).status_code == 401

    headers = await register_and_login(client, "plain@example.com")
    response = await client.get("/api/v1/system/llm-health", headers=headers)
    assert response.status_code == 403, response.text

    headers = await register_and_login(client, "admin@example.com")
    await _make_admin(sessionmaker, "admin@example.com")
    response = await client.get("/api/v1/system/llm-health", headers=headers)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"provider", "healthy"}


class _Queue:
    def __init__(self, consumers: int | None) -> None:
        self.consumers = consumers

    async def probe(self) -> QueueProbe:
        return QueueProbe(queue="analysis_v2", consumers=self.consumers)


def _override(healthy: bool | None, consumers: int | None) -> None:
    async def check() -> bool | None:
        return healthy

    app.dependency_overrides[get_pipeline_health_check] = lambda: check
    app.dependency_overrides[get_analysis_queue_instance] = lambda: _Queue(consumers)


async def test_analysis_health_requires_admin(
    client: AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _override(True, 1)
    assert (await client.get("/api/v1/system/analysis-health")).status_code == 401
    headers = await register_and_login(client, "plain-health@example.com")
    response = await client.get("/api/v1/system/analysis-health", headers=headers)
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    ("healthy", "consumers", "status"),
    [
        (True, 1, "ok"),
        (False, 1, "down"),
        (True, 0, "down"),
        (None, 1, "unknown"),
        (True, None, "unknown"),
        (None, 0, "down"),
    ],
)
async def test_analysis_health_status(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    healthy: bool | None,
    consumers: int | None,
    status: str,
) -> None:
    _override(healthy, consumers)
    headers = await register_and_login(client, "admin-health@example.com")
    await _make_admin(sessionmaker, "admin-health@example.com")
    response = await client.get("/api/v1/system/analysis-health", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == status
    assert body["healthy"] is healthy
    assert body["queue"] == {"name": "analysis_v2", "consumers": consumers}
    # Провайдер по умолчанию в тестах — legacy_llm: он работает без контракта 2.0.
    assert body["provider"] == "legacy_llm"
    assert body["contract_version"] is None


async def test_capabilities_is_public(client: AsyncClient) -> None:
    response = await client.get("/api/v1/system/capabilities")
    assert response.status_code == 200
    assert set(response.json()) == {"upload", "analysis", "review", "export"}


async def test_capabilities_match_real_behaviour(client: AsyncClient) -> None:
    """Значения capabilities должны совпадать с тем, что API на самом деле принимает."""
    caps = (await client.get("/api/v1/system/capabilities")).json()
    upload = caps["upload"]
    assert upload["document_extensions"] == [".docx", ".markdown", ".md", ".pdf", ".txt"]
    assert upload["document_formats"] == ["docx", "markdown", "pdf", "txt"]
    assert upload["unsupported_extensions"] == [".doc"]
    assert upload["max_size_bytes"] == upload["max_size_mb"] * 1024 * 1024
    assert caps["export"]["same_format_only"] is True
    assert caps["export"]["requires_status"] == "ready"
    assert caps["analysis"]["contract_version"] is None

    # Объявленные пути существуют в приложении с тем же методом.
    paths = app.openapi()["paths"]
    for declared in (caps["review"]["atomic_save_endpoint"], caps["export"]["endpoint"]):
        method, path = declared.split(" ", 1)
        assert method.lower() in paths.get(path, {}), declared

    # Каждое объявленное расширение действительно принимается, .doc — нет.
    headers = await register_and_login(client, "caps@example.com")
    project = await client.post("/api/v1/projects", json={"name": "caps"}, headers=headers)
    project_id = project.json()["id"]
    for extension in upload["document_extensions"]:
        response = await client.post(
            f"/api/v1/projects/{project_id}/documents",
            files={"file": (f"file{extension}", _sample(extension), "application/octet-stream")},
            headers=headers,
        )
        assert response.status_code == 201, (extension, response.text)
        assert response.json()["format"] in upload["document_formats"]
    response = await client.post(
        f"/api/v1/projects/{project_id}/documents",
        files={"file": ("old.doc", b"\xd0\xcf\x11\xe0", "application/msword")},
        headers=headers,
    )
    assert response.status_code == 415


def _sample(extension: str) -> bytes:
    if extension == ".pdf":
        return (Path(__file__).parents[1] / "fixtures" / "pdf" / "headings_lists.pdf").read_bytes()
    if extension != ".docx":
        return b"text"
    buffer = io.BytesIO()
    DocxDocument().save(buffer)
    return buffer.getvalue()
