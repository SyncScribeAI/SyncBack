"""Бизнес-логика задач анализа документов.

Сервис зависит от IUnitOfWork и порта очереди AnalysisQueue; каждая операция —
одна транзакция с одним commit. Переходы статуса документа применяются только
если задача остаётся текущей для документа (current_analysis_job_id).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn

from app.domain.analysis_reuse import AnalysisStrategy
from app.domain.exceptions import (
    AnalysisAlreadyRunningError,
    AnalysisConfirmationRequiredError,
    AnalysisJobAlreadyExistsError,
    AnalysisJobConflictError,
    AnalysisJobNotCancellableError,
    AnalysisJobNotFoundError,
    DocumentNotFoundError,
    InvalidDocumentStatusError,
)
from app.domain.interfaces.analysis_queue import AnalysisQueue
from app.domain.interfaces.entities import DocumentProtocol
from app.domain.interfaces.event_publisher import IEventPublisher
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.lifecycle import AnalysisJobLifecycle, DocumentLifecycle
from app.domain.services.document_events import DocumentEventOutbox
from app.domain.value_objects import AnalysisJobStatusVO, DocumentStatusVO

if TYPE_CHECKING:
    from app.infrastructure.db.models.analysis_job import AnalysisJob
    from app.infrastructure.db.models.document import Document

logger = logging.getLogger("syncscribe.services.analysis_job")


class BulkSkipReason(StrEnum):
    """Почему документ не попал в массовый запуск."""

    NOT_FOUND = "not_found"
    CONFIRMATION_REQUIRED = "confirmation_required"
    ANALYSIS_RUNNING = "analysis_running"
    INVALID_STATUS = "invalid_status"


@dataclass(frozen=True)
class BulkJobOutcome:
    """Итог массового запуска по одному документу: задача или причина пропуска."""

    document_id: uuid.UUID
    job: AnalysisJob | None = None
    job_id: uuid.UUID | None = None
    skip_reason: BulkSkipReason | None = None
    message: str | None = None


@dataclass
class ResetResult:
    """Результат сброса ревью: сколько правок и флагов вернулось в PENDING и актуальный документ."""

    reset_count: int
    document: DocumentProtocol
    flags_reset_count: int = 0


class AnalysisJobService:
    def __init__(
        self,
        uow: IUnitOfWork,
        queue: AnalysisQueue | None = None,
        *,
        events: IEventPublisher | None = None,
    ) -> None:
        self._uow = uow
        self._queue = queue
        self._outbox = DocumentEventOutbox(events)

    def _require_queue(self) -> AnalysisQueue:
        if self._queue is None:
            raise RuntimeError("AnalysisJobService создан без очереди анализа")
        return self._queue

    # ------------------------------------------------------------------
    # Idempotency helpers
    # ------------------------------------------------------------------

    async def find_job_by_idempotency_key(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        idempotency_key: str,
    ) -> AnalysisJob | None:
        async with self._uow:
            document = await self._uow.documents.get_by_id(document_id)
            if document is None or document.project_id != project_id:
                return None
            return await self._uow.jobs.get_by_idempotency_key(document_id, idempotency_key)

    # ------------------------------------------------------------------
    # Core job lifecycle
    # ------------------------------------------------------------------

    async def create_job(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        idempotency_key: str | None = None,
        *,
        force: bool = False,
        strategy: AnalysisStrategy = AnalysisStrategy.AUTO,
    ) -> AnalysisJob:
        """Создать задачу анализа и перевести документ в draft до постановки в очередь.

        Повторный анализ готового документа без force → AnalysisConfirmationRequiredError:
        проверка идёт в той же транзакции, что и создание задачи.

        Если задача с таким Idempotency-Key уже есть (в том числе создана
        параллельным запросом), бросается AnalysisJobAlreadyExistsError с этой
        задачей: роутер вернёт 200 и не поставит её в очередь повторно.

        Предварительные проверки не атомарны, поэтому окончательным арбитром
        служат уникальные индексы БД. Проигравший гонку запрос получает
        AnalysisAlreadyRunningError (конкурирующий запуск) или
        AnalysisJobAlreadyExistsError (тот же Idempotency-Key), а не 500.
        """
        try:
            job, created = await self._create_job_once(
                project_id, document_id, idempotency_key, force=force, strategy=strategy
            )
        except AnalysisJobConflictError as exc:
            # Транзакция уже откачена; событие о переводе документа в draft
            # не должно уйти подписчикам.
            self._outbox.discard()
            await self._raise_create_conflict(document_id, idempotency_key, exc)
        if not created:
            raise AnalysisJobAlreadyExistsError(job)
        return job

    async def _create_job_once(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        idempotency_key: str | None,
        *,
        force: bool,
        strategy: AnalysisStrategy,
    ) -> tuple[AnalysisJob, bool]:
        """Вернуть (задача, создана_ли_сейчас)."""
        async with self._uow:
            document = await self._uow.documents.get_by_id(document_id)
            if document is None or document.project_id != project_id:
                raise DocumentNotFoundError(
                    f"Документ {document_id} не найден в проекте {project_id}"
                )

            if idempotency_key is not None:
                existing = await self._uow.jobs.get_by_idempotency_key(document_id, idempotency_key)
                if existing is not None:
                    return existing, False

            if not DocumentLifecycle.can_start_analysis(document.status):
                raise InvalidDocumentStatusError(
                    f"Анализ нельзя запустить для документа в статусе {document.status.value}"
                )

            active = await self._uow.jobs.get_active_by_document_id(document.id)
            if active is not None:
                # Параллельный запрос с тем же ключом мог закоммитить задачу уже после
                # поиска по ключу выше: для клиента это та же задача, а не конфликт.
                if idempotency_key is not None and active.idempotency_key == idempotency_key:
                    return active, False
                raise AnalysisAlreadyRunningError("Для документа уже выполняется анализ")

            if DocumentLifecycle.analysis_needs_confirmation(document.status) and not force:
                raise AnalysisConfirmationRequiredError(
                    "Документ уже проходил анализ: повторный анализ заменит результаты ревью. "
                    "Передайте force=true."
                )

            # Пока задача не поставлена в очередь, документ — черновик: прежние
            # результаты ревью считаются сброшенными.
            if document.status != DocumentStatusVO.DRAFT:
                document = await self._set_document_status(document, DocumentStatusVO.DRAFT)

            job = await self._uow.jobs.create_for_document(
                document,
                status=AnalysisJobStatusVO.PENDING,
                idempotency_key=idempotency_key,
                strategy=strategy,
            )
            await self._uow.documents.set_current_job(document, job.id)
            await self._commit()
        return job, True

    async def _raise_create_conflict(
        self,
        document_id: uuid.UUID,
        idempotency_key: str | None,
        cause: AnalysisJobConflictError,
    ) -> NoReturn:
        """Разобрать проигранную гонку в новой транзакции и бросить доменную ошибку."""
        existing: AnalysisJob | None = None
        if idempotency_key is not None:
            async with self._uow:
                existing = await self._uow.jobs.get_by_idempotency_key(document_id, idempotency_key)
        if existing is not None:
            raise AnalysisJobAlreadyExistsError(existing) from cause
        raise AnalysisAlreadyRunningError("Для документа уже выполняется анализ") from cause

    async def dispatch_job(self, job: AnalysisJob) -> AnalysisJob:
        """Отправить задачу в очередь и вернуть её актуальное состояние.

        Статус dispatched выставляется до отправки, чтобы быстрый воркер не был
        перезаписан более поздней записью. Если очередь недоступна, задача
        помечается FAILED (QUEUE_UNAVAILABLE), документ возвращается в DRAFT,
        и такая задача возвращается без исключения.
        """
        if not AnalysisJobLifecycle.can_dispatch(job.status):
            raise InvalidDocumentStatusError(
                f"Задачу анализа в статусе {job.status.value} нельзя отправить в очередь"
            )
        queue = self._require_queue()

        async with self._uow:
            document = await self._uow.documents.get_by_id(job.document_id)
            if document is None:
                raise DocumentNotFoundError(f"Документ {job.document_id} не найден")
            sources = await self._uow.sources.list_for_analysis(document.project_id, document.id)
            source_ids = [source.id for source in sources]
            job = await self._set_job_status(job, document, AnalysisJobStatusVO.DISPATCHED)
            await self._commit()

        try:
            task_id = await queue.enqueue(job.id, source_ids)
        except Exception as exc:  # noqa: BLE001 — любой сбой брокера фиксируем в задаче
            logger.warning(
                "Очередь анализа недоступна, задача помечена FAILED",
                exc_info=True,
                extra={"job_id": str(job.id), "document_id": str(job.document_id)},
            )
            return await self._mark_queue_unavailable(job, str(exc))

        async with self._uow:
            job = await self._uow.jobs.set_celery_task_id(job, task_id)
            await self._commit()
        return job

    async def _mark_queue_unavailable(self, job: AnalysisJob, error_message: str) -> AnalysisJob:
        async with self._uow:
            document = await self._uow.documents.get_by_id(job.document_id)
            if document is None:
                raise DocumentNotFoundError(f"Документ {job.document_id} не найден")
            job = await self._set_job_status(
                job,
                document,
                AnalysisJobStatusVO.FAILED,
                error_code="QUEUE_UNAVAILABLE",
                error_message=error_message,
            )
            await self._commit()
        return job

    async def cancel_job(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> AnalysisJob:
        async with self._uow:
            job = await self._get_job(project_id, document_id, job_id)
            if job.status == AnalysisJobStatusVO.CANCELLED:
                return job
            if not AnalysisJobLifecycle.can_cancel(job.status):
                raise AnalysisJobNotCancellableError("Завершённую задачу анализа отменить нельзя")
            document = await self._uow.documents.get_by_id(document_id)
            if document is None:
                raise DocumentNotFoundError(f"Документ {document_id} не найден")
            job = await self._set_job_status(
                job,
                document,
                AnalysisJobStatusVO.CANCELLED,
                error_code="ANALYSIS_CANCELLED",
                error_message="Анализ отменён",
            )
            await self._commit()
        return job

    async def revoke_celery_task(self, celery_task_id: str) -> None:
        await self._require_queue().revoke(celery_task_id)

    async def get_active_for_document(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> AnalysisJob | None:
        async with self._uow:
            document = await self._uow.documents.get_by_id(document_id)
            if document is None or document.project_id != project_id:
                return None
            return await self._uow.jobs.get_active_by_document_id(document_id)

    async def get_job(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> AnalysisJob:
        async with self._uow:
            return await self._get_job(project_id, document_id, job_id)

    async def reset_analysis(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> ResetResult:
        """Вернуть правки и флаги текущего анализа в PENDING, документ — в AWAITING_APPROVAL."""
        async with self._uow:
            document = await self._uow.documents.get_by_id(document_id)
            if document is None or document.project_id != project_id:
                raise DocumentNotFoundError(
                    f"Документ {document_id} не найден в проекте {project_id}"
                )
            if not DocumentLifecycle.can_reset_review(document.status):
                raise InvalidDocumentStatusError(
                    f"Сброс невозможен для документа в статусе {document.status.value}"
                )
            if document.current_analysis_job_id is None:
                raise AnalysisJobNotFoundError("У документа нет активного анализа для сброса")

            reset_ids = await self._uow.suggestions.reset_to_pending(
                document.current_analysis_job_id
            )
            # «Сбросить все изменения» возвращает и отметки флагов: после сброса документ
            # проверяют заново, и прежняя отметка «просмотрено» уже ничего не значит.
            flags_reset = await self._uow.flags.reset_to_pending(document.current_analysis_job_id)
            document = await self._set_document_status(document, DocumentStatusVO.AWAITING_APPROVAL)
            await self._commit()

        return ResetResult(
            reset_count=len(reset_ids), document=document, flags_reset_count=flags_reset
        )

    async def bulk_create_jobs_for_project(
        self,
        project_id: uuid.UUID,
        document_ids: list[uuid.UUID] | None = None,
        *,
        force: bool = False,
        strategy: AnalysisStrategy = AnalysisStrategy.AUTO,
    ) -> list[BulkJobOutcome]:
        """Создать и поставить в очередь задачи для документов проекта, вернуть итог по каждому.

        Без document_ids берутся все документы, из которых анализ в принципе
        запускается (draft, awaiting_approval, ready). Готовые документы без force
        пропускаются с причиной confirmation_required, чтобы клиент спросил
        подтверждение и повторил запрос. Явно переданные документы, которые
        запустить нельзя, тоже попадают в ответ с причиной, а не теряются.
        """
        if document_ids is None:
            async with self._uow:
                target_ids = await self._uow.documents.list_ids_by_statuses(
                    project_id, DocumentLifecycle.ANALYZABLE
                )
        else:
            target_ids = list(dict.fromkeys(document_ids))

        outcomes: list[BulkJobOutcome] = []
        for document_id in target_ids:
            try:
                job = await self.create_job(project_id, document_id, force=force, strategy=strategy)
            except DocumentNotFoundError as exc:
                outcomes.append(_skipped(document_id, BulkSkipReason.NOT_FOUND, exc))
            except AnalysisConfirmationRequiredError as exc:
                outcomes.append(_skipped(document_id, BulkSkipReason.CONFIRMATION_REQUIRED, exc))
            except AnalysisAlreadyRunningError as exc:
                outcomes.append(_skipped(document_id, BulkSkipReason.ANALYSIS_RUNNING, exc))
            except InvalidDocumentStatusError as exc:
                outcomes.append(_skipped(document_id, BulkSkipReason.INVALID_STATUS, exc))
            else:
                # Ставим в очередь сразу, а не после цикла: задача не должна
                # висеть в pending, пока создаются задачи для остальных документов.
                job = await self.dispatch_job(job)
                outcomes.append(BulkJobOutcome(document_id=document_id, job=job, job_id=job.id))
        return await self._reload_jobs(outcomes)

    async def _reload_jobs(self, outcomes: list[BulkJobOutcome]) -> list[BulkJobOutcome]:
        # Откат транзакции на пропущенном документе помечает все объекты сессии
        # устаревшими, включая уже созданные задачи; даже чтение job.id тогда
        # полезло бы в БД вне async-контекста. Поэтому id запоминается сразу.
        job_ids = [o.job_id for o in outcomes if o.job_id is not None]
        if not job_ids:
            return outcomes
        async with self._uow:
            jobs = {job.id: job for job in await self._uow.jobs.list_by_ids(job_ids)}
        return [replace(o, job=jobs[o.job_id]) if o.job_id in jobs else o for o in outcomes]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _set_document_status(self, document: Document, target: DocumentStatusVO) -> Document:
        document = await self._uow.documents.update_status(
            document, DocumentLifecycle.transition(document.status, target)
        )
        self._outbox.record(document)
        return document

    async def _commit(self) -> None:
        try:
            await self._uow.commit()
        except BaseException:
            self._outbox.discard()
            raise
        await self._outbox.flush(self._uow)

    async def _set_job_status(
        self,
        job: AnalysisJob,
        document: Document,
        target: AnalysisJobStatusVO,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> AnalysisJob:
        """Сменить статус задачи и, если она текущая для документа, статус документа."""
        job = await self._uow.jobs.update_status(
            job,
            AnalysisJobLifecycle.transition(job.status, target),
            error_code=error_code,
            error_message=error_message,
        )
        if document.current_analysis_job_id == job.id:
            await self._set_document_status(
                document, AnalysisJobLifecycle.document_status_for(target)
            )
        return job

    async def _get_job(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> AnalysisJob:
        document = await self._uow.documents.get_by_id(document_id)
        if document is None or document.project_id != project_id:
            raise DocumentNotFoundError(f"Документ {document_id} не найден в проекте {project_id}")
        job = await self._uow.jobs.get_by_id(job_id)
        if job is None or job.document_id != document_id:
            raise AnalysisJobNotFoundError(
                f"Задача {job_id} не найдена для документа {document_id}"
            )
        return job


def _skipped(document_id: uuid.UUID, reason: BulkSkipReason, exc: Exception) -> BulkJobOutcome:
    return BulkJobOutcome(document_id=document_id, skip_reason=reason, message=str(exc))
