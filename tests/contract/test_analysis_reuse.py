"""Повторный анализ без изменений входа: результат переносится без вызова ML.

Задача вызывается напрямую через analyze_document, конвейер заменён управляемой заглушкой.
Проверяется приёмка D1: второй прогон не вызывает конвейер (нет записи «вызов
конвейера» и запроса в заглушке), а правки, флаги и решения пользователя на месте.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

import app.workers.tasks.analysis_v2_tasks as v2
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
from app.infrastructure.db.repositories.source_snapshot_repository import (
    SourceSnapshotRepository,
)
from tests.contract.test_analysis_jobs import FakeAnalysisQueue, queue  # noqa: F401
from tests.contract.test_worker_pipeline_v2 import Worker, _block_ref, _start, worker  # noqa: F401

pytestmark = pytest.mark.asyncio

_CALL_LOG = "Анализ v2: вызов конвейера"


def _change_and_flag(request: AnalysisRequest) -> AnalysisResult:
    return AnalysisResult(
        status=AnalysisStatus.COMPLETED,
        suggestions=(
            ProposedChange(
                kind="modify",
                block_ref=_block_ref(request, "10 рабочих"),
                old_text="10 рабочих дней",
                new_text="5 рабочих дней",
            ),
        ),
        flags=(
            ReviewFlag(
                reason="sources_conflict",
                block_ref=_block_ref(request, "картой"),
                block_text="Оплата принимается картой.",
            ),
        ),
        meta={"checked_blocks": 2, "errors": 0},
    )


async def _first_run(
    client: AsyncClient, queue: FakeAnalysisQueue, worker: Worker, sources: list[str]
) -> tuple[dict[str, str], str, str, list[str]]:
    worker.analyzer.respond = _change_and_flag
    headers, project_id, document_id, job_id, source_ids = await _start(client, queue, sources)
    await v2.analyze_document(job_id, source_ids, 0)
    base = f"/api/v1/projects/{project_id}/documents/{document_id}"
    return headers, base, job_id, source_ids


async def _decide_all(client: AsyncClient, headers: dict[str, str], base: str) -> None:
    items = (await client.get(f"{base}/suggestions", headers=headers)).json()["items"]
    reviewed = await client.put(
        f"{base}/suggestions/review",
        json={
            "review_version": 0,
            "decisions": [{"suggestion_id": i["id"], "decision": "accepted"} for i in items],
        },
        headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    [flag] = (await client.get(f"{base}/flags", headers=headers)).json()
    marked = await client.patch(
        f"{base}/flags/{flag['id']}", json={"status": "reviewed"}, headers=headers
    )
    assert marked.status_code == 200, marked.text


async def _rerun(
    client: AsyncClient,
    queue: FakeAnalysisQueue,
    headers: dict[str, str],
    base: str,
    **body: object,
) -> tuple[str, list[str]]:
    response = await client.post(
        f"{base}/analysis-jobs", json={"force": True, **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    job_id, source_ids = queue.enqueued[-1]
    await v2.analyze_document(str(job_id), [str(s) for s in source_ids], 0)
    return str(job_id), [str(s) for s in source_ids]


async def _job(sessionmaker: async_sessionmaker[AsyncSession], job_id: str) -> AnalysisJob:
    async with sessionmaker() as session:
        job = await session.get(AnalysisJob, uuid.UUID(job_id))
        assert job is not None
        return job


async def test_unchanged_input_reuses_result_with_decisions(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers, base, first_id, _ = await _first_run(client, queue, worker, ["A", "B"])
    await _decide_all(client, headers, base)
    first = await _job(sessionmaker, first_id)
    assert first.input_hash is not None and len(first.input_hash) == 64

    caplog.set_level(logging.INFO, logger="syncscribe.workers.analysis_v2")
    caplog.clear()
    second_id, _ = await _rerun(client, queue, headers, base)

    assert len(worker.analyzer.requests) == 1
    assert _CALL_LOG not in [record.getMessage() for record in caplog.records]
    second = await _job(sessionmaker, second_id)
    assert (second.status.value, second.input_hash) == ("success", first.input_hash)
    assert second.request_id is None
    assert second.checked_blocks == first.checked_blocks
    assert second.pipeline_meta is not None
    assert _meta(second.pipeline_meta["incremental"]) == {
        "mode": "reused",
        "reason": "reused",
        "baseline_job_id": first_id,
        "chain_length": 0,
        "fallback_reason": None,
    }
    stats = second.pipeline_meta["run_stats"]
    assert (stats["segments_sent"], stats["chars_sent"], stats["blocks_checked"]) == (0, 0, 0)
    assert stats["segments_total"] == 2

    async with sessionmaker() as session:
        suggestions = (
            (
                await session.execute(
                    select(Suggestion)
                    .where(Suggestion.analysis_job_id == uuid.UUID(second_id))
                    .options(selectinload(Suggestion.sources))
                )
            )
            .scalars()
            .all()
        )
        flags = (
            (
                await session.execute(
                    select(AnalysisFlag).where(AnalysisFlag.analysis_job_id == uuid.UUID(second_id))
                )
            )
            .scalars()
            .all()
        )
        originals = (
            (
                await session.execute(
                    select(Suggestion).where(Suggestion.analysis_job_id == uuid.UUID(first_id))
                )
            )
            .scalars()
            .all()
        )
    [suggestion] = suggestions
    [original] = originals
    assert suggestion.id != original.id
    assert suggestion.carried_from_id == original.id
    assert (suggestion.status.value, suggestion.decided_by) == ("accepted", original.decided_by)
    assert suggestion.evidence == original.evidence
    assert sorted(source.name for source in suggestion.sources) == ["A", "B"]
    [flag] = flags
    assert (flag.status, flag.carried_from_id is not None) == ("reviewed", True)

    # Документ текущей задачи показывает перенесённый результат: решений не осталось.
    editor = (await client.get(f"{base}/editor", headers=headers)).json()
    assert editor["document"]["status"] == "ready"
    job = (await client.get(f"{base}/analysis-jobs/{second_id}", headers=headers)).json()
    assert job["incremental"]["mode"] == "reused"
    assert job["strategy"] == "auto"
    assert job["run_stats"]["segments_sent"] == 0
    # Своя запись входа у новой задачи: просмотр источника читает снимки этой задачи.
    async with sessionmaker() as session:
        job_sources = await SourceSnapshotRepository(session).list_job_sources(uuid.UUID(second_id))
    assert sorted(s.name for s in job_sources) == ["A", "B"]
    assert all(s.snapshot is not None for s in job_sources)


async def test_strategy_full_calls_pipeline_and_measures_delta(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, base, first_id, _ = await _first_run(client, queue, worker, ["A"])

    second_id, _ = await _rerun(client, queue, headers, base, strategy="full")

    assert len(worker.analyzer.requests) == 2
    second = await _job(sessionmaker, second_id)
    assert second.strategy == "full"
    assert second.pipeline_meta is not None
    assert _meta(second.pipeline_meta["incremental"]) == {
        "mode": "full",
        "reason": "strategy_full",
        "baseline_job_id": first_id,
        "chain_length": 0,
        "fallback_reason": "strategy_full",
        "changed_share": 0.0,
        "segments_added": 0,
        "segments_removed": 0,
    }


async def test_mode_off_always_calls_pipeline(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, base, _, _ = await _first_run(client, queue, worker, ["A"])
    worker.configure(monkeypatch, analysis_incremental_mode="off")

    second_id, _ = await _rerun(client, queue, headers, base)

    assert len(worker.analyzer.requests) == 2
    second = await _job(sessionmaker, second_id)
    assert second.pipeline_meta is not None
    assert second.pipeline_meta["incremental"]["reason"] == "mode_off"


async def test_changed_source_calls_pipeline_with_changed_share(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    worker.connector.texts["A"] = "Первый абзац.\n\nСрок ответа 5 дней."
    headers, base, first_id, _ = await _first_run(client, queue, worker, ["A"])
    worker.connector.texts["A"] = "Первый абзац.\n\nСрок ответа 3 дня."

    second_id, _ = await _rerun(client, queue, headers, base)

    assert len(worker.analyzer.requests) == 2
    second = await _job(sessionmaker, second_id)
    assert second.pipeline_meta is not None
    incremental = second.pipeline_meta["incremental"]
    assert (incremental["reason"], incremental["baseline_job_id"]) == ("input_changed", first_id)
    assert (incremental["segments_added"], incremental["segments_removed"]) == (1, 1)
    # «Срок ответа 3 дня.» занимает 18 из 31 символа входа.
    assert incremental["changed_share"] == round(18 / 31, 4)


async def test_failed_source_disables_reuse(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers, base, _, _ = await _first_run(client, queue, worker, ["A", "B"])
    worker.connector.broken.add("B")

    second_id, _ = await _rerun(client, queue, headers, base)

    assert len(worker.analyzer.requests) == 2
    second = await _job(sessionmaker, second_id)
    assert second.pipeline_meta is not None
    assert second.pipeline_meta["incremental"]["reason"] == "sources_failed"


async def test_partial_job_is_not_a_baseline(
    client: AsyncClient,
    queue: FakeAnalysisQueue,  # noqa: F811
    worker: Worker,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    worker.connector.broken.add("B")
    headers, base, _, _ = await _first_run(client, queue, worker, ["A", "B"])
    worker.connector.broken.clear()

    second_id, _ = await _rerun(client, queue, headers, base)
    third_id, _ = await _rerun(client, queue, headers, base)

    assert len(worker.analyzer.requests) == 2
    second = await _job(sessionmaker, second_id)
    third = await _job(sessionmaker, third_id)
    assert second.pipeline_meta is not None and third.pipeline_meta is not None
    assert second.pipeline_meta["incremental"]["reason"] == "no_baseline"
    assert _meta(third.pipeline_meta["incremental"]) == {
        "mode": "reused",
        "reason": "reused",
        "baseline_job_id": second_id,
        "chain_length": 0,
        "fallback_reason": None,
    }


def _meta(incremental: dict[str, Any]) -> dict[str, Any]:
    """meta без хэша документа: он зависит от байтов тестового файла."""
    assert len(incremental["document_hash"]) == 64
    return {key: value for key, value in incremental.items() if key != "document_hash"}
