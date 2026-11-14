"""Инкрементальный анализ: изменённый источник проверяется выборочно.

Конвейер заменён заглушкой, которая отвечает по тексту запроса: правка срока для блока
со сроком, если он редактируемый, и флаг для блока об оплате. Заглушка проверяет только
механику выбора и переноса; полноту выборочной проверки на реальной модели эти тесты не
измеряют.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.workers.tasks.analysis_v2_tasks as v2
from app.domain.evidence import Quote
from app.domain.interfaces.document_analyzer import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    ProposedChange,
    ReviewFlag,
)
from app.infrastructure.db.models.analysis_flag import AnalysisFlag
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.suggestion import Suggestion
from tests.contract.test_analysis_jobs import FakeAnalysisQueue, queue  # noqa: F401
from tests.contract.test_analysis_reuse import _job, _rerun
from tests.contract.test_worker_pipeline_v2 import Worker, _start, worker  # noqa: F401

pytestmark = pytest.mark.asyncio

DOCUMENT = "\n\n".join(
    [
        "# Регламент",
        "Срок ответа составляет 10 рабочих дней.",
        "Оплата принимается картой.",
        "Заявки регистрируются в журнале учёта.",
        "Отчёт по обращениям готовит бухгалтерия ежемесячно.",
        "Пропуск для гостей выдаёт охрана на входе.",
        "Совещание отдела проходит по понедельникам.",
        "Парковка доступна сотрудникам с утра.",
        "Обед в столовой с часу до двух.",
        "Связь с дежурным по внутреннему телефону.",
        "Почта для писем лежит на ресепшене.",
    ]
).encode()

DEADLINE = "Срок ответа на обращения 5 рабочих дней."
SOURCE = "\n\n".join(
    [
        "# Решения",
        DEADLINE,
        "Оплата принимается картой и переводом.",
        "Заявки регистрируются в журнале учёта.",
        "Отчёт по обращениям готовит бухгалтерия ежемесячно.",
        "Пропуск для гостей выдаёт охрана.",
        "Совещание отдела проходит по понедельникам.",
        "Парковка доступна сотрудникам с утра.",
        "Обед в столовой с часу до двух.",
        "Связь с дежурным по внутреннему телефону.",
        "Почта для писем лежит на ресепшене.",
    ]
)
_DAYS = re.compile(r"[^\n]*?(\d+) рабочих дн[^\n]*")


def _editable(request: AnalysisRequest, text: str) -> str | None:
    for block in request.document.blocks:
        if text in block.text and block.editable:
            return block.ref
    return None


def respond(request: AnalysisRequest) -> AnalysisResult:
    """Правка срока по числу дней из источника и флаг об оплате, если блоки проверяются."""
    changes: list[ProposedChange] = []
    flags: list[ReviewFlag] = []
    deadline = _editable(request, "10 рабочих")
    found = next((m for s in request.sources if (m := _DAYS.search(s.text))), None)
    if deadline is not None and found is not None:
        changes.append(
            ProposedChange(
                kind="modify",
                block_ref=deadline,
                old_text="10 рабочих дней",
                new_text=f"{found.group(1)} рабочих дней",
                quotes=(Quote(source_name="Решения", text=found.group(0)),),
            )
        )
    payment = _editable(request, "картой")
    if payment is not None:
        flags.append(
            ReviewFlag(
                reason="sources_conflict",
                block_ref=payment,
                block_text="Оплата принимается картой.",
            )
        )
    checked = sum(1 for block in request.document.blocks if block.editable)
    return AnalysisResult(
        status=AnalysisStatus.COMPLETED,
        suggestions=tuple(changes),
        flags=tuple(flags),
        meta={"checked_blocks": checked, "errors": 0},
    )


async def _first(
    client: AsyncClient, queue: FakeAnalysisQueue, worker: Worker
) -> tuple[dict[str, str], str, str]:
    worker.analyzer.respond = respond
    worker.connector.texts["Решения"] = SOURCE
    headers, project_id, document_id, job_id, source_ids = await _start(
        client, queue, ["Решения"], content=DOCUMENT
    )
    await v2.analyze_document(job_id, source_ids, 0)
    return headers, f"/api/v1/projects/{project_id}/documents/{document_id}", job_id


async def _rows(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: str
) -> tuple[list[Suggestion], list[AnalysisFlag]]:
    async with sessionmaker() as session:
        job = uuid.UUID(job_id)
        suggestions = (
            (await session.execute(select(Suggestion).where(Suggestion.analysis_job_id == job)))
            .scalars()
            .all()
        )
        flags = (
            (await session.execute(select(AnalysisFlag).where(AnalysisFlag.analysis_job_id == job)))
            .scalars()
            .all()
        )
    return list(suggestions), list(flags)


async def _age(sessionmaker: async_sessionmaker[AsyncSession], *job_ids: str) -> None:
    """SQLite хранит created_at с точностью до секунды; прошлые задачи сдвигаются в прошлое,
    чтобы последняя задача документа определялась однозначно."""
    async with sessionmaker() as session:
        await session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id.in_([uuid.UUID(job_id) for job_id in job_ids]))
            .values(created_at=AnalysisJob.created_at - timedelta(minutes=1))
        )
        await session.commit()


async def _decide(client: AsyncClient, headers: dict[str, str], base: str, decision: str) -> None:
    items = (await client.get(f"{base}/suggestions", headers=headers)).json()["items"]
    response = await client.put(
        f"{base}/suggestions/review",
        json={
            "review_version": 0,
            "decisions": [{"suggestion_id": i["id"], "decision": decision} for i in items],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text


async def test_changed_paragraph_is_sent_with_context_to_few_blocks(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, base, first_id = await _first(client, queue, worker)
    [full_request] = worker.analyzer.requests
    checkable = sum(1 for block in full_request.document.blocks if block.editable)

    worker.connector.texts["Решения"] = SOURCE.replace("5 рабочих дней", "3 рабочих дней")
    second_id, _ = await _rerun(client, queue, headers, base)

    request = worker.analyzer.requests[-1]
    [source] = request.sources
    assert "Срок ответа на обращения 3 рабочих дней." in source.text
    # Заголовок раздела и сосед изменённого абзаца.
    assert source.text.startswith("# Решения\n\n")
    assert "Оплата принимается картой и переводом." in source.text
    assert len(source.text) < len(full_request.sources[0].text)
    editable = [block.text for block in request.document.blocks if block.editable]
    assert "Срок ответа составляет 10 рабочих дней." in editable
    assert len(editable) <= checkable // 2
    # Документ уходит целиком: остальные блоки нужны модели как контекст.
    assert len(request.document.blocks) == len(full_request.document.blocks)

    second = await _job(sessionmaker, second_id)
    assert second.pipeline_meta is not None
    incremental = second.pipeline_meta["incremental"]
    assert (incremental["mode"], incremental["chain_length"]) == ("delta", 1)
    assert incremental["fallback_reason"] is None
    assert incremental["blocks_selected"] == len(editable)
    assert str(second.baseline_job_id) == first_id
    stats = second.pipeline_meta["run_stats"]
    assert stats["blocks_checked"] == len(editable)
    assert stats["chars_sent"] == len(source.text) < stats["chars_total"]

    suggestions, flags = await _rows(sessionmaker, second_id)
    assert [(s.suggested_text, s.status.value) for s in suggestions] == [
        ("3 рабочих дней", "pending")
    ]
    assert [flag.reason for flag in flags] == ["sources_conflict"]

    job = (await client.get(f"{base}/analysis-jobs/{second_id}", headers=headers)).json()
    assert job["incremental"]["mode"] == "delta"
    assert job["incremental"]["blocks_selected"] == len(editable)


async def test_rejected_suggestion_is_not_shown_again(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, base, _ = await _first(client, queue, worker)
    await _decide(client, headers, base, "rejected")

    # Абзац о сроке переписан, число то же: модель снова предложит ту же правку.
    worker.connector.texts["Решения"] = SOURCE.replace(DEADLINE, "Срок ответа: 5 рабочих дней.")
    second_id, _ = await _rerun(client, queue, headers, base)

    assert worker.analyzer.requests[-1].sources[0].text != SOURCE
    suggestions, _ = await _rows(sessionmaker, second_id)
    assert [(s.suggested_text, s.status.value) for s in suggestions] == [
        ("5 рабочих дней", "rejected")
    ]
    assert suggestions[0].carried_from_id is not None
    second = await _job(sessionmaker, second_id)
    assert second.pipeline_meta is not None
    assert second.pipeline_meta["incremental"]["inherited"] == 1


async def test_removed_evidence_marks_decision_stale_and_flags_block(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, base, first_id = await _first(client, queue, worker)
    [first] = (await _rows(sessionmaker, first_id))[0]
    assert first.evidence and first.evidence[0]["segment_index"] == 1
    await _decide(client, headers, base, "accepted")

    worker.connector.texts["Решения"] = SOURCE.replace(DEADLINE + "\n\n", "")
    second_id, _ = await _rerun(client, queue, headers, base)

    suggestions, flags = await _rows(sessionmaker, second_id)
    assert [(s.status.value, s.stale) for s in suggestions] == [("accepted", True)]
    assert "evidence_removed" in [flag.reason for flag in flags]
    items = (await client.get(f"{base}/suggestions", headers=headers)).json()["items"]
    assert [(item["stale"], item["carried_from_id"]) for item in items] == [(True, str(first.id))]


async def test_chain_limit_falls_back_to_full_analysis(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker.configure(monkeypatch, analysis_delta_max_chain=1)
    headers, base, first_id = await _first(client, queue, worker)
    worker.connector.texts["Решения"] = SOURCE.replace("5 рабочих", "4 рабочих")
    second_id, _ = await _rerun(client, queue, headers, base)
    await _age(sessionmaker, first_id)
    worker.connector.texts["Решения"] = SOURCE.replace("5 рабочих", "3 рабочих")
    third_id, _ = await _rerun(client, queue, headers, base)

    second, third = await _job(sessionmaker, second_id), await _job(sessionmaker, third_id)
    assert second.pipeline_meta is not None and third.pipeline_meta is not None
    assert second.pipeline_meta["incremental"]["mode"] == "delta"
    assert (
        third.pipeline_meta["incremental"]["mode"],
        third.pipeline_meta["incremental"]["fallback_reason"],
        third.pipeline_meta["incremental"]["chain_length"],
    ) == ("full", "chain_limit", 0)
    assert third.baseline_job_id is None
    assert worker.analyzer.requests[-1].sources[0].text == worker.connector.texts["Решения"]


async def test_large_change_falls_back_to_full_analysis(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, base, _ = await _first(client, queue, worker)
    worker.connector.texts["Решения"] = SOURCE.replace("а", "о")
    second_id, _ = await _rerun(client, queue, headers, base)
    second = await _job(sessionmaker, second_id)
    assert second.pipeline_meta is not None
    assert second.pipeline_meta["incremental"]["fallback_reason"] == "changed_share"


async def test_audit_runs_full_analysis_and_records_recall(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker.configure(monkeypatch, analysis_delta_audit_rate=1.0)
    headers, base, _ = await _first(client, queue, worker)
    worker.connector.texts["Решения"] = SOURCE.replace("5 рабочих", "3 рабочих")
    second_id, _ = await _rerun(client, queue, headers, base)

    # Полный анализ, выборочный и контрольный полный.
    assert len(worker.analyzer.requests) == 3
    audit_request = worker.analyzer.requests[-1]
    assert all(
        block.editable == (block.type.value != "heading") for block in audit_request.document.blocks
    )
    second = await _job(sessionmaker, second_id)
    assert second.pipeline_meta is not None
    audit = second.pipeline_meta["incremental"]["audit"]
    assert (audit["recall"], audit["missed"]) == (1.0, 0)
    suggestions, _ = await _rows(sessionmaker, second_id)
    assert len(suggestions) == 1
