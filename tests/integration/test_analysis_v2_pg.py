"""Задача анализа v2 на PostgreSQL: повторная доставка и отмена во время ожидания конвейера.

S3, Redis и ML-конвейер подменяются заглушками; блокировка строки задачи
(SELECT … FOR UPDATE) проверяется на настоящей БД — SQLite её не поддерживает.
Заглушка конвейера держит каждый вызов до сигнала теста, поэтому обе доставки
гарантированно оказываются внутри вызова одновременно.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.interfaces.document_analyzer import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    ProposedChange,
)
from app.domain.interfaces.source_connector import SourceRef
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.enums import AnalysisJobStatus, DocumentFormat, SourceType
from app.infrastructure.db.models.project import Project
from app.infrastructure.db.models.source import Source
from app.infrastructure.db.models.suggestion import Suggestion
from app.infrastructure.db.models.user import User
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.workers.tasks import analysis_v2_tasks as v2

DOCUMENT = "Первый абзац документа.\n\nВторой абзац документа.\n".encode()
WAIT_SECONDS = 10


@dataclass
class Scenario:
    job_id: uuid.UUID
    source_ids: list[str]


class GatedAnalyzer:
    """Каждый вызов сообщает о входе и ждёт, пока тест его отпустит."""

    def __init__(self, calls: int) -> None:
        self.started = [asyncio.Event() for _ in range(calls)]
        self.gates = [asyncio.Event() for _ in range(calls)]
        self.calls = 0

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        index = self.calls
        self.calls += 1
        self.started[index].set()
        await self.gates[index].wait()
        block = next(b for b in request.document.blocks if "Первый" in b.text)
        return AnalysisResult(
            status=AnalysisStatus.COMPLETED,
            suggestions=(
                ProposedChange("modify", block.ref, "Первый абзац", "Первый абзац обновлён"),
            ),
        )

    async def health_check(self) -> bool:
        return True


class FakeStorage:
    async def download(self, key: str) -> bytes:
        return DOCUMENT


class FakeConnector:
    async def fetch_structured(self, source: SourceRef) -> str:
        return "Текст источника."


class Silent:
    async def publish(self, event: object) -> None:
        return None


@pytest_asyncio.fixture
async def scenario(pg_sessionmaker: async_sessionmaker[AsyncSession]) -> Scenario:
    async with pg_sessionmaker() as session:
        user = User(email=f"it-{uuid.uuid4().hex}@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        project = Project(name="analysis-v2", owner_id=user.id)
        session.add(project)
        await session.flush()
        document = Document(
            project_id=project.id,
            name="doc.txt",
            format=DocumentFormat.TXT,
            storage_key=f"it/{uuid.uuid4()}.txt",
            size_bytes=len(DOCUMENT),
            uploaded_at=datetime.now(UTC),
        )
        session.add(document)
        await session.flush()
        source = Source(
            project_id=project.id,
            name="source.txt",
            type=SourceType.FILE,
            storage_key=f"it/{uuid.uuid4()}.source.txt",
        )
        session.add(source)
        job = AnalysisJob(document_id=document.id, status=AnalysisJobStatus.DISPATCHED)
        session.add(job)
        await session.commit()
        return Scenario(job_id=job.id, source_ids=[str(source.id)])


@pytest.fixture
def worker_env(
    monkeypatch: pytest.MonkeyPatch, pg_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    @asynccontextmanager
    async def _uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
        async with pg_sessionmaker() as session:
            yield SqlAlchemyUnitOfWork(session)

    monkeypatch.setattr(v2, "uow_factory", _uow)
    monkeypatch.setattr(v2, "get_storage", FakeStorage)
    monkeypatch.setattr(v2, "get_connector_for", lambda kind: FakeConnector())
    monkeypatch.setattr(v2, "event_publisher", Silent)


async def _suggestions_count(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> int:
    async with sessionmaker() as session:
        query = select(func.count()).select_from(Suggestion)
        return (
            await session.execute(query.where(Suggestion.analysis_job_id == job_id))
        ).scalar_one()


async def _status(sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> str:
    async with sessionmaker() as session:
        job = await session.get(AnalysisJob, job_id)
        assert job is not None
        return job.status.value


@pytest.mark.usefixtures("worker_env")
async def test_concurrent_deliveries_write_suggestions_once(
    pg_sessionmaker: async_sessionmaker[AsyncSession],
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = GatedAnalyzer(calls=2)
    monkeypatch.setattr(v2, "_get_analyzer", lambda: analyzer)
    job_id = str(scenario.job_id)

    first = asyncio.create_task(v2.analyze_document(job_id, scenario.source_ids, 0))
    second = asyncio.create_task(v2.analyze_document(job_id, scenario.source_ids, 0))
    for started in analyzer.started:
        await asyncio.wait_for(started.wait(), WAIT_SECONDS)

    for gate in analyzer.gates:
        gate.set()
    assert await asyncio.wait_for(asyncio.gather(first, second), WAIT_SECONDS) == [None, None]

    assert await _suggestions_count(pg_sessionmaker, scenario.job_id) == 1
    assert await _status(pg_sessionmaker, scenario.job_id) == "success"


@pytest.mark.usefixtures("worker_env")
async def test_cancel_while_waiting_for_pipeline_drops_result(
    pg_sessionmaker: async_sessionmaker[AsyncSession],
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = GatedAnalyzer(calls=1)
    monkeypatch.setattr(v2, "_get_analyzer", lambda: analyzer)

    running = asyncio.create_task(v2.analyze_document(str(scenario.job_id), scenario.source_ids, 0))
    await asyncio.wait_for(analyzer.started[0].wait(), WAIT_SECONDS)
    async with pg_sessionmaker() as session:
        await session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == scenario.job_id)
            .values(status=AnalysisJobStatus.CANCELLED)
        )
        await session.commit()

    analyzer.gates[0].set()
    assert await asyncio.wait_for(running, WAIT_SECONDS) is None

    assert await _status(pg_sessionmaker, scenario.job_id) == "cancelled"
    assert await _suggestions_count(pg_sessionmaker, scenario.job_id) == 0


@pytest.mark.usefixtures("worker_env")
async def test_finish_waits_for_row_lock_of_concurrent_writer(
    pg_sessionmaker: async_sessionmaker[AsyncSession],
    scenario: Scenario,
) -> None:
    # Строка задачи заблокирована «другим воркером»: finish ждёт, а после его
    # завершения видит терминальный статус и ничего не пишет.
    async with pg_sessionmaker() as holder:
        locked = await SqlAlchemyUnitOfWork(holder).jobs.lock_if_active(scenario.job_id)
        assert locked is not None
        pending = asyncio.create_task(
            v2.finish(scenario.job_id, v2.AnalysisJobStatusVO.FAILED, "LLM_UNAVAILABLE", "x")
        )
        await asyncio.sleep(0.3)
        assert not pending.done()
        locked.status = AnalysisJobStatus.SUCCESS
        await holder.commit()

    assert await asyncio.wait_for(pending, WAIT_SECONDS) is False
    assert await _status(pg_sessionmaker, scenario.job_id) == "success"
