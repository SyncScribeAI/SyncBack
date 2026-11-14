"""SQLAlchemy-адаптер для AnalysisJob.

Решения о переходах принимает домен (AnalysisJobLifecycle), репозиторий
только записывает статус и отметки времени. Фиксация транзакции — на стороне UoW.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analysis_reuse import AnalysisStrategy, Baseline, RunInput
from app.domain.exceptions import AnalysisJobConflictError
from app.domain.interfaces.repositories import IAnalysisJobRepository
from app.domain.lifecycle import AnalysisJobLifecycle
from app.domain.pipeline_run import PipelineRunSummary
from app.domain.value_objects import AnalysisJobStatusVO, KeysetPage, PaginationParams
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.enums import AnalysisJobStatus


def _status_to_orm(vo: AnalysisJobStatusVO) -> AnalysisJobStatus:
    return AnalysisJobStatus(vo.value)


_ACTIVE_ORM_STATUSES = tuple(_status_to_orm(st) for st in AnalysisJobLifecycle.ACTIVE)

_IDEMPOTENCY_CONSTRAINT = "uq_analysis_jobs_doc_idem_key"


def _violates_idempotency_key(exc: IntegrityError) -> bool:
    """Какой уникальный ключ нарушен: идемпотентности или «одна активная задача».

    PostgreSQL называет ограничение в сообщении, SQLite перечисляет колонки.
    """
    message = str(exc.orig)
    return _IDEMPOTENCY_CONSTRAINT in message or "idempotency_key" in message


class AnalysisJobRepository(IAnalysisJobRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_by_id(self, job_id: uuid.UUID) -> AnalysisJob | None:
        return await self._session.get(AnalysisJob, job_id)

    async def list_by_ids(self, job_ids: Sequence[uuid.UUID]) -> list[AnalysisJob]:
        if not job_ids:
            return []
        result = await self._session.execute(
            select(AnalysisJob).where(AnalysisJob.id.in_(list(job_ids)))
        )
        return list(result.scalars().all())

    async def get_by_idempotency_key(
        self, document_id: uuid.UUID, idempotency_key: str
    ) -> AnalysisJob | None:
        result = await self._session.execute(
            select(AnalysisJob).where(
                AnalysisJob.document_id == document_id,
                AnalysisJob.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def get_active_by_document_id(self, document_id: uuid.UUID) -> AnalysisJob | None:
        result = await self._session.execute(
            select(AnalysisJob)
            .where(
                AnalysisJob.document_id == document_id,
                AnalysisJob.status.in_(_ACTIVE_ORM_STATUSES),
            )
            .order_by(AnalysisJob.created_at.desc(), AnalysisJob.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_by_document(
        self,
        document_id: uuid.UUID,
        pagination: PaginationParams,
    ) -> list[AnalysisJob]:
        stmt = (
            select(AnalysisJob)
            .where(AnalysisJob.document_id == document_id)
            # id замыкает порядок так же, как курсор keyset: при равном created_at
            # иначе страницы теряли бы или повторяли задачи.
            .order_by(AnalysisJob.created_at.desc(), AnalysisJob.id.desc())
        )
        if isinstance(pagination, KeysetPage):
            if pagination.has_cursor:
                stmt = stmt.where(
                    (AnalysisJob.created_at < pagination.before_created_at)
                    | (
                        (AnalysisJob.created_at == pagination.before_created_at)
                        & (AnalysisJob.id < pagination.before_id)
                    )
                )
            stmt = stmt.limit(pagination.limit)
        else:
            stmt = stmt.limit(pagination.limit).offset(pagination.offset)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def create_for_document(
        self,
        document: Document,
        *,
        job_id: uuid.UUID | None = None,
        status: AnalysisJobStatusVO = AnalysisJobStatusVO.PENDING,
        idempotency_key: str | None = None,
        strategy: AnalysisStrategy = AnalysisStrategy.AUTO,
    ) -> AnalysisJob:
        """Фабричный метод: создаёт ORM-объект AnalysisJob внутри репозитория.

        Репозиторий НЕ мутирует document.current_analysis_job_id.
        Вызывающий сервис обязан сам выполнить:
            document.current_analysis_job_id = job.id
        или использовать uow.documents.set_current_job(document, job.id).
        Это соблюдает SRP: jobs-репозиторий не знает про структуру Document.

        refresh не нужен, достаточно flush(); commit — ответственность вызывающего UoW.

        INSERT выполняется в SAVEPOINT. Нарушение уникального индекса
        («одна активная задача на документ» или ключ идемпотентности) при
        параллельном запуске превращается в AnalysisJobConflictError, а не в
        «сырой» IntegrityError, который клиент увидел бы как 500.
        """
        job = AnalysisJob(
            id=job_id or uuid.uuid4(),
            document_id=document.id,
            status=_status_to_orm(status),
            idempotency_key=idempotency_key,
            strategy=strategy.value,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(job)
                await self._session.flush()
        except IntegrityError as exc:
            by_key = idempotency_key is not None and _violates_idempotency_key(exc)
            raise AnalysisJobConflictError(
                "Задача анализа для документа уже создана параллельным запросом",
                by_idempotency_key=by_key,
            ) from exc
        return job

    async def set_celery_task_id(self, job: AnalysisJob, task_id: str) -> AnalysisJob:
        job.celery_task_id = task_id
        await self._session.flush()
        return job

    async def mark_processing_if_active(self, job_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.id == job_id,
                AnalysisJob.status.in_(_ACTIVE_ORM_STATUSES),
            )
            .values(
                status=AnalysisJobStatus.PROCESSING,
                started_at=func.coalesce(AnalysisJob.started_at, datetime.now(UTC)),
            )
            .returning(AnalysisJob.id)
        )
        await self._session.flush()
        return result.scalar_one_or_none() is not None

    async def lock_if_active(self, job_id: uuid.UUID) -> AnalysisJob | None:
        result = await self._session.execute(
            select(AnalysisJob)
            .where(AnalysisJob.id == job_id, AnalysisJob.status.in_(_ACTIVE_ORM_STATUSES))
            .with_for_update()
            # Объект мог быть загружен сессией раньше: статус берём из заблокированной строки.
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def record_pipeline_run(self, job: AnalysisJob, run: PipelineRunSummary) -> None:
        job.request_id = run.request_id
        job.pipeline_meta = {
            "contract_version": run.contract_version,
            "provider": run.provider,
            "meta": None if run.meta is None else dict(run.meta),
            "evidence": None if run.evidence is None else dict(run.evidence),
        }
        job.checked_blocks = run.checked_blocks
        job.failed_blocks = run.failed_blocks

    async def find_reuse_baseline(
        self, document_id: uuid.UUID, exclude_job_id: uuid.UUID
    ) -> Baseline | None:
        # partial_success и упавшие блоки исключены: их результат неполон, и перенос
        # закрепил бы пропуски. Поиск идёт по ix_analysis_jobs_doc_latest.
        row = (
            await self._session.execute(
                select(AnalysisJob.id, AnalysisJob.input_hash, AnalysisJob.pipeline_meta)
                .where(
                    AnalysisJob.document_id == document_id,
                    AnalysisJob.id != exclude_job_id,
                    AnalysisJob.status == AnalysisJobStatus.SUCCESS,
                    AnalysisJob.partial_success.is_(False),
                    or_(AnalysisJob.failed_blocks.is_(None), AnalysisJob.failed_blocks == 0),
                    AnalysisJob.input_hash.is_not(None),
                )
                .order_by(AnalysisJob.created_at.desc(), AnalysisJob.id.desc())
                .limit(1)
            )
        ).first()
        if row is None or row.input_hash is None:
            return None
        meta = row.pipeline_meta or {}
        incremental = meta.get("incremental") or {}
        chain = incremental.get("chain_length")
        document_hash = incremental.get("document_hash")
        return Baseline(
            job_id=row.id,
            input_hash=row.input_hash,
            document_hash=document_hash if isinstance(document_hash, str) else None,
            contract_version=meta.get("contract_version"),
            provider=meta.get("provider"),
            chain_length=chain if isinstance(chain, int) else 0,
        )

    async def record_input(self, job: AnalysisJob, run_input: RunInput) -> None:
        job.input_hash = run_input.input_hash
        job.baseline_job_id = run_input.baseline_job_id
        meta = dict(job.pipeline_meta or {})
        meta.setdefault("contract_version", run_input.contract_version)
        meta.setdefault("provider", run_input.provider)
        meta["incremental"] = run_input.incremental_meta()
        meta["run_stats"] = run_input.run_stats_meta(meta.get("meta"))
        job.pipeline_meta = meta

    async def record_audit(self, job_id: uuid.UUID, audit: dict[str, Any]) -> None:
        job = await self._session.get(AnalysisJob, job_id, with_for_update=True)
        if job is None:
            return
        meta = dict(job.pipeline_meta or {})
        incremental = dict(meta.get("incremental") or {})
        incremental["audit"] = audit
        meta["incremental"] = incremental
        job.pipeline_meta = meta

    async def record_reuse(self, job: AnalysisJob, baseline_job_id: uuid.UUID) -> None:
        baseline = await self._session.get(AnalysisJob, baseline_job_id)
        evidence = (baseline.pipeline_meta or {}).get("evidence") if baseline else None
        job.request_id = None
        job.pipeline_meta = {"meta": None, "evidence": evidence}
        job.checked_blocks = baseline.checked_blocks if baseline else None
        job.failed_blocks = baseline.failed_blocks if baseline else None

    async def update_status(
        self,
        job: AnalysisJob,
        status: AnalysisJobStatusVO,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> AnalysisJob:
        job.status = _status_to_orm(status)
        job.error_code = error_code
        job.error_message = error_message
        now = datetime.now(UTC)
        if status is AnalysisJobStatusVO.PROCESSING and job.started_at is None:
            job.started_at = now
        if AnalysisJobLifecycle.is_terminal(status):
            job.finished_at = now
        await self._session.flush()
        return job
