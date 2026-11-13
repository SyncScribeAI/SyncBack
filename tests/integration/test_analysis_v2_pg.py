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
    FlagQuote,
    ProposedChange,
    ReviewFlag,
)
from app.domain.interfaces.file_storage import UploadContent
from app.domain.interfaces.source_connector import SourceRef
from app.domain.source_text import SourceLayout
from app.infrastructure.db.models.analysis_flag import AnalysisFlag
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.audit_log import AuditLog
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.enums import (
    AnalysisJobStatus,
    AuditAction,
    DocumentFormat,
    SourceType,
)
from app.infrastructure.db.models.project import Project
from app.infrastructure.db.models.source import Source
from app.infrastructure.db.models.suggestion import Suggestion
from app.infrastructure.db.models.user import User
from app.domain.value_objects import FlagStatusVO
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
    """Документ отдаётся по любому ключу, снимки источников пишутся в общий словарь."""

    files: dict[str, bytes] = {}

    async def download(self, key: str) -> bytes:
        return self.files.get(key, DOCUMENT)

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def upload(self, key: str, content: UploadContent) -> None:
        self.files[key] = content.stream.read()


class FakeConnector:
    async def fetch_structured(self, source: SourceRef) -> str:
        return "Текст источника."

    async def fetch_layout(self, source: SourceRef) -> SourceLayout:
        return SourceLayout(await self.fetch_structured(source))


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


class ReversedOrderAnalyzer:
    """Правки в обратном порядке документа: выдача должна следовать ответу конвейера."""

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        first, second = request.document.blocks[:2]
        return AnalysisResult(
            status=AnalysisStatus.COMPLETED,
            suggestions=(
                ProposedChange("modify", second.ref, "Второй абзац", "Второй абзац обновлён"),
                ProposedChange("delete", first.ref, "документа", None),
                ProposedChange("modify", first.ref, "Первый", "Начальный"),
            ),
            meta={"checked_blocks": 2, "errors": 0, "model": "модель-1"},
        )

    async def health_check(self) -> bool:
        return True


@pytest.mark.usefixtures("worker_env")
async def test_order_offsets_and_pipeline_meta_on_postgres(
    pg_sessionmaker: async_sessionmaker[AsyncSession],
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v2, "_get_analyzer", ReversedOrderAnalyzer)

    await v2.analyze_document(str(scenario.job_id), scenario.source_ids, 0)

    text = DOCUMENT.decode()
    async with pg_sessionmaker() as session:
        uow = SqlAlchemyUnitOfWork(session)
        items = await uow.suggestions.list_by_analysis_job(scenario.job_id)
        assert [(s.position, s.original_text) for s in items] == [
            (0, "Второй абзац"),
            (1, "документа"),
            (2, "Первый"),
        ]
        for item in items:
            assert item.start_offset is not None and item.end_offset is not None
            assert text[item.start_offset : item.end_offset] == item.original_text
        assert [s.block_id for s in items] == ["p-1", "p-0", "p-0"]

        job = await session.get(AnalysisJob, scenario.job_id)
        assert job is not None
        assert (job.request_id, job.checked_blocks, job.failed_blocks) == (
            f"{scenario.job_id}:0",
            2,
            0,
        )
        assert job.pipeline_meta is not None
        assert job.pipeline_meta["meta"] == {"checked_blocks": 2, "errors": 0, "model": "модель-1"}
        # JSONB, а не текст: по ключам meta можно фильтровать в SQL.
        model = await session.scalar(
            select(AnalysisJob.pipeline_meta["meta"]["model"].astext).where(
                AnalysisJob.id == scenario.job_id
            )
        )
        assert model == "модель-1"


class RepeatedPhraseAnalyzer:
    """Одна и та же правка фразы в двух абзацах — одна группа."""

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        return AnalysisResult(
            status=AnalysisStatus.COMPLETED,
            suggestions=tuple(
                ProposedChange("modify", block.ref, "документа", "текста")
                for block in request.document.blocks[:2]
            ),
        )

    async def health_check(self) -> bool:
        return True


@pytest.mark.usefixtures("worker_env")
async def test_suggestion_groups_on_postgres(
    pg_sessionmaker: async_sessionmaker[AsyncSession],
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v2, "_get_analyzer", RepeatedPhraseAnalyzer)

    await v2.analyze_document(str(scenario.job_id), scenario.source_ids, 0)

    async with pg_sessionmaker() as session:
        uow = SqlAlchemyUnitOfWork(session)
        items = await uow.suggestions.list_by_analysis_job(scenario.job_id)
        assert [s.origin for s in items] == ["model", "model"]
        (key,) = {s.group_key for s in items}
        assert key is not None
        second_id = items[1].id
        await session.execute(
            update(Suggestion).where(Suggestion.id == items[0].id).values(status="rejected")
        )
        await session.flush()

        groups = await uow.suggestions.list_groups(scenario.job_id)
        assert [(g.group_key, g.size, g.pending) for g in groups] == [(key, 2, 1)]
        page, total = await uow.suggestions.list_with_total(
            scenario.job_id, limit=1, offset=0, group_key=key
        )
        assert (len(page), total) == (1, 2)
        assert await uow.suggestions.list_with_total(
            scenario.job_id, limit=10, offset=0, group_key="нет-такой"
        ) == ([], 0)

        index = await session.scalar(
            select(func.pg_get_indexdef(func.to_regclass("ix_suggestions_job_group")))
        )
        assert index is not None and "WHERE (group_key IS NOT NULL)" in index
        await session.rollback()

    async with pg_sessionmaker() as session:
        with pytest.raises(Exception, match="ck_suggestions_origin"):
            await session.execute(
                update(Suggestion).where(Suggestion.id == second_id).values(origin="manual")
            )


class FlagAnalyzer:
    """Флаг без правок: документ уходит в ready, флаг остаётся для ручной проверки."""

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        block = request.document.blocks[1]
        return AnalysisResult(
            status=AnalysisStatus.COMPLETED,
            flags=(
                ReviewFlag(
                    "sources_conflict",
                    block.ref,
                    "",
                    "Источники расходятся",
                    (FlagQuote("Регламент", "«срок — 5 дней»"),),
                ),
            ),
        )

    async def health_check(self) -> bool:
        return True


@pytest.mark.usefixtures("worker_env")
async def test_flags_on_postgres(
    pg_sessionmaker: async_sessionmaker[AsyncSession],
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v2, "_get_analyzer", FlagAnalyzer)

    await v2.analyze_document(str(scenario.job_id), scenario.source_ids, 0)

    async with pg_sessionmaker() as session:
        uow = SqlAlchemyUnitOfWork(session)
        [flag] = await uow.flags.list_by_analysis_job(scenario.job_id)
        assert (flag.block_id, flag.original_text, flag.status) == (
            "p-1",
            "Второй абзац документа.",
            "pending",
        )
        # JSONB с кириллицей без экранирования: цитата читается по ключу в SQL.
        quote = await session.scalar(
            select(AnalysisFlag.quotes[0]["text"].astext).where(AnalysisFlag.id == flag.id)
        )
        assert quote == "«срок — 5 дней»"
        user_id = await session.scalar(
            select(Project.owner_id)
            .join(Document, Document.project_id == Project.id)
            .where(Document.id == flag.document_id)
        )
        assert user_id is not None
        # Повторная отметка — тот же UPDATE без изменений: второе событие аудита не нужно.
        assert await uow.flags.set_status(flag, FlagStatusVO.REVIEWED, user_id) is True
        assert await uow.flags.set_status(flag, FlagStatusVO.REVIEWED, user_id) is False
        assert flag.status == "reviewed" and flag.decided_by == user_id
        assert await uow.flags.reset_to_pending(scenario.job_id) == 1
        await uow.audit.create(
            document_id=flag.document_id,
            user_id=user_id,
            action=AuditAction.FLAG_REVIEW.value,
            details={"flag_id": str(flag.id)},
        )
        await session.commit()
        flag_id, document_id = flag.id, flag.document_id

    async with pg_sessionmaker() as session:
        # Событие флага — действие над документом: ссылка на правку запрещена.
        session.add(
            AuditLog(
                document_id=document_id,
                suggestion_id=uuid.uuid4(),
                action=AuditAction.FLAG_REOPEN,
            )
        )
        with pytest.raises(Exception, match="ck_audit_logs_target"):
            await session.flush()

    async with pg_sessionmaker() as session:
        with pytest.raises(Exception, match="ck_analysis_flags_status"):
            await session.execute(
                update(AnalysisFlag).where(AnalysisFlag.id == flag_id).values(status="accepted")
            )

    async with pg_sessionmaker() as session:
        await session.execute(
            AnalysisJob.__table__.delete().where(AnalysisJob.id == scenario.job_id)
        )
        await session.commit()
        assert await session.get(AnalysisFlag, flag_id) is None
