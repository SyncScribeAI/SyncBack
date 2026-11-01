"""Project DELETE contract: authorization and repeated deletion."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.contract.conftest import register_and_login

pytestmark = pytest.mark.asyncio


async def test_delete_foreign_project_is_hidden_and_keeps_it(client: AsyncClient) -> None:
    owner = await register_and_login(client, "delete-owner@example.com")
    created = await client.post("/api/v1/projects", json={"name": "Keep me"}, headers=owner)
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    stranger = await register_and_login(client, "delete-stranger@example.com")

    response = await client.delete(f"/api/v1/projects/{project_id}", headers=stranger)
    assert response.status_code == 404, response.text
    response = await client.get(f"/api/v1/projects/{project_id}", headers=owner)
    assert response.status_code == 200, response.text


async def test_delete_requires_auth_and_repeated_delete_returns_404(client: AsyncClient) -> None:
    owner = await register_and_login(client, "delete-repeat@example.com")
    created = await client.post("/api/v1/projects", json={"name": "Delete once"}, headers=owner)
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    assert (await client.delete(f"/api/v1/projects/{project_id}")).status_code == 401
    response = await client.delete(f"/api/v1/projects/{project_id}", headers=owner)
    assert response.status_code == 204
    response = await client.delete(f"/api/v1/projects/{project_id}", headers=owner)
    assert response.status_code == 404
    response = await client.delete(f"/api/v1/projects/{uuid.uuid4()}", headers=owner)
    assert response.status_code == 404
