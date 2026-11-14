"""Флаги «проверьте вручную»: от ответа конвейера до отметки в редакторе.

Задача анализа v2 вызывается напрямую, конвейер — управляемая заглушка (см.
test_worker_pipeline_v2). Проверяется то, что видит фронтенд: агрегат редактора,
список флагов, PATCH отметки, сброс и аудит.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.workers.tasks.analysis_v2_tasks as v2
from app.domain.interfaces.document_analyzer import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    FlagQuote,
    ProposedChange,
    ReviewFlag,
)
from app.infrastructure.db.models.analysis_flag import AnalysisFlag
from app.infrastructure.db.models.audit_log import AuditLog
from tests.contract.conftest import register_and_login
from tests.contract.test_analysis_jobs import FakeAnalysisQueue, queue  # noqa: F401
from tests.contract.test_worker_pipeline_v2 import Worker, _block_ref, _start, worker  # noqa: F401

pytestmark = pytest.mark.asyncio


def _flags_only(request: AnalysisRequest) -> AnalysisResult:
    payment = _block_ref(request, "картой")
    return AnalysisResult(
        status=AnalysisStatus.COMPLETED,
        flags=(
            ReviewFlag(
                reason="sources_conflict",
                block_ref=payment,
                block_text="копия от конвейера",
                rationale="Источники расходятся",
                quotes=(FlagQuote("A", "только картой"), FlagQuote("B", "и наличными")),
            ),
            ReviewFlag(reason="", block_ref=_block_ref(request, "10 рабочих"), block_text=""),
            # Блока нет в документе — флаг отбрасывается, а не роняет ответ.
            ReviewFlag(reason="x", block_ref="p-404", block_text=""),
        ),
    )


def _flag_and_change(request: AnalysisRequest) -> AnalysisResult:
    result = _flags_only(request)
    change = ProposedChange(
        kind="modify",
        block_ref=_block_ref(request, "10 рабочих"),
        old_text="10 рабочих дней",
        new_text="5 рабочих дней",
    )
    return AnalysisResult(status=result.status, suggestions=(change,), flags=result.flags)


async def _analyze(
    client: AsyncClient, queue: FakeAnalysisQueue, worker: Worker
) -> tuple[dict[str, str], str, str]:
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, ["A"])
    await v2.analyze_document(job_id, source_ids, 0)
    return headers, f"/api/v1/projects/{project_id}/documents/{document_id}", document_id


async def _audit(sessionmaker: async_sessionmaker[AsyncSession]) -> list[tuple[str, object]]:
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars()
        return [
            (row.action.value, row.details) for row in rows if row.action.value.startswith("flag_")
        ]


async def test_flags_are_stored_in_pipeline_order_and_shown_in_editor(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
) -> None:
    worker.analyzer.respond = _flags_only
    headers, base, document_id = await _analyze(client, queue, worker)

    editor = await client.get(f"{base}/editor", headers=headers)
    assert editor.status_code == 200, editor.text
    body = editor.json()
    # Флаги не держат документ в review: правок нет — документ готов.
    assert body["document"]["status"] == "ready"
    assert body["counters"]["flags_pending"] == 2
    blocks = {block["ref"]: block for block in body["content"]["blocks"]}
    first, second = body["flags"]
    assert (first["reason"], first["section_ref"], first["position"]) == (
        "sources_conflict",
        "Оплата",
        0,
    )
    assert first["original_text"] == "Оплата принимается картой."
    assert first["original_text"] in body["content"]["plain_text"]
    assert first["block_id"] in blocks
    # Поля основания необязательные; здесь проверяется прежняя часть цитаты.
    assert [{"source_name": q["source_name"], "text": q["text"]} for q in first["quotes"]] == [
        {"source_name": "A", "text": "только картой"},
        {"source_name": "B", "text": "и наличными"},
    ]
    assert (first["status"], first["decided_by"]) == ("pending", None)
    assert (second["reason"], second["position"]) == ("unknown", 1)

    listed = await client.get(f"{base}/flags", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == body["flags"]


async def test_review_and_reopen_write_one_audit_event_each(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    worker.analyzer.respond = _flag_and_change
    headers, base, _ = await _analyze(client, queue, worker)
    flag_id = (await client.get(f"{base}/flags", headers=headers)).json()[0]["id"]

    for _ in range(2):
        reviewed = await client.patch(
            f"{base}/flags/{flag_id}", json={"status": "reviewed"}, headers=headers
        )
        assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"
    assert reviewed.json()["decided_by"] is not None
    assert reviewed.json()["decided_at"] is not None

    editor = (await client.get(f"{base}/editor", headers=headers)).json()
    # Отметка флага не решает правку и не меняет статус документа.
    assert editor["document"]["status"] == "awaiting_approval"
    assert (editor["counters"]["flags_pending"], editor["counters"]["pending"]) == (1, 1)

    reopened = await client.patch(
        f"{base}/flags/{flag_id}", json={"status": "pending"}, headers=headers
    )
    assert reopened.status_code == 200, reopened.text
    assert (reopened.json()["status"], reopened.json()["decided_by"]) == ("pending", None)

    assert await _audit(sessionmaker) == [
        ("flag_review", {"flag_id": flag_id}),
        ("flag_reopen", {"flag_id": flag_id}),
    ]


async def test_reset_returns_flags_to_pending(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
) -> None:
    worker.analyzer.respond = _flag_and_change
    headers, base, _ = await _analyze(client, queue, worker)
    flags = (await client.get(f"{base}/flags", headers=headers)).json()
    for flag in flags:
        response = await client.patch(
            f"{base}/flags/{flag['id']}", json={"status": "reviewed"}, headers=headers
        )
        assert response.status_code == 200, response.text

    reset = await client.post(f"{base}/editor/reset", headers=headers)
    assert reset.status_code == 200, reset.text
    assert reset.json()["flags_reset_count"] == 2

    after = (await client.get(f"{base}/flags", headers=headers)).json()
    assert {(f["status"], f["decided_by"]) for f in after} == {("pending", None)}


async def test_flag_errors(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    worker.analyzer.respond = _flags_only
    headers, base, _ = await _analyze(client, queue, worker)
    flag_id = (await client.get(f"{base}/flags", headers=headers)).json()[0]["id"]

    missing = await client.patch(
        f"{base}/flags/{uuid.uuid4()}", json={"status": "reviewed"}, headers=headers
    )
    assert missing.status_code == 404

    invalid = await client.patch(
        f"{base}/flags/{flag_id}", json={"status": "accepted"}, headers=headers
    )
    assert invalid.status_code == 422

    # Чужой проект неотличим от несуществующего.
    other = await register_and_login(client, email="other@example.com")
    assert (await client.get(f"{base}/flags", headers=other)).status_code == 404
    foreign = await client.patch(
        f"{base}/flags/{flag_id}", json={"status": "reviewed"}, headers=other
    )
    assert foreign.status_code == 404

    async with sessionmaker() as session:
        stored = await session.get(AnalysisFlag, uuid.UUID(flag_id))
        assert stored is not None and stored.status == "pending"
