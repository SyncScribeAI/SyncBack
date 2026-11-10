"""Завершение задачи анализа: статус задачи, статус документа и событие для SSE.

Общий шаг прежнего chord (finalize_analysis_job) и задачи v2 (analyze_document_for_job),
чтобы правила перехода документа не расходились между конвейерами. Фиксация транзакции и
публикация событий — на вызывающем: событие уходит только после commit.
"""

from __future__ import annotations

from app.domain.interfaces.event_publisher import IEventPublisher
from app.domain.interfaces.unit_of_work import IUnitOfWork
from app.domain.lifecycle import AnalysisJobLifecycle, DocumentLifecycle
from app.domain.services.document_events import DocumentEventOutbox
from app.domain.value_objects import AnalysisJobStatusVO, SuggestionStatusVO
from app.infrastructure.db.models.analysis_job import AnalysisJob
from app.infrastructure.db.models.document import Document


async def complete_job(
    uow: IUnitOfWork,
    job: AnalysisJob,
    document: Document,
    status: AnalysisJobStatusVO,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    publisher: IEventPublisher | None = None,
) -> DocumentEventOutbox:
    """Записать терминальный статус задачи и, если она текущая, — статус документа.

    Документ переходит, только если задача всё ещё текущая: иначе поздний результат
    старого анализа перезаписал бы статус документа, у которого уже идёт новый.
    """
    pending = await uow.suggestions.count_by_analysis_job_and_status(
        job.id, SuggestionStatusVO.PENDING
    )
    job = await uow.jobs.update_status(
        job, AnalysisJobLifecycle.transition(job.status, status), error_code, error_message
    )
    job.partial_success = status is AnalysisJobStatusVO.PARTIAL_SUCCESS
    outbox = DocumentEventOutbox(publisher)
    if document.current_analysis_job_id == job.id:
        document_status = AnalysisJobLifecycle.document_status_for(
            status, has_pending_suggestions=pending > 0
        )
        document = await uow.documents.update_status(
            document, DocumentLifecycle.transition(document.status, document_status)
        )
        outbox.record(document)
    return outbox
