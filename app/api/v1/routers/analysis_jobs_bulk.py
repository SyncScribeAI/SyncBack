"""Массовый запуск анализа по документам проекта.

POST /projects/{project_id}/documents/analysis-jobs/bulk
"""

from fastapi import APIRouter, Body, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_allowed_project
from app.api.schemas.analysis_job import (
    AnalysisJobResponse,
    BulkAnalysisJobsResponse,
    BulkAnalysisRequest,
    BulkJobResult,
)
from app.core.dependencies import get_analysis_job_service
from app.domain.analysis_reuse import AnalysisStrategy
from app.domain.services.analysis_job_service import AnalysisJobService, BulkSkipReason
from app.infrastructure.db.models.project import Project

router = APIRouter(
    prefix="/projects/{project_id}/documents",
    tags=["analysis-jobs"],
)


@router.post(
    "/analysis-jobs/bulk",
    responses={
        201: {"model": BulkAnalysisJobsResponse, "description": "Запущена хотя бы одна задача"},
        200: {"model": BulkAnalysisJobsResponse, "description": "Ни одна задача не запущена"},
    },
)
async def bulk_start_analysis_jobs(
    payload: BulkAnalysisRequest = Body(default=BulkAnalysisRequest()),
    project: Project = Depends(get_allowed_project),
    service: AnalysisJobService = Depends(get_analysis_job_service),
) -> JSONResponse:
    """Запустить анализ для выбранных или всех подходящих документов проекта.

    По каждому документу в results либо задача, либо skip_reason. Готовые документы
    без force=true пропускаются с причиной confirmation_required: клиент показывает
    диалог и повторяет запрос с теми же document_ids и force=true.
    Сбой очереди не прерывает запуск остальных: задача приходит в статусе failed
    с кодом QUEUE_UNAVAILABLE. Ответ 201, если запущена хотя бы одна задача, иначе 200.
    """
    outcomes = await service.bulk_create_jobs_for_project(
        project.id,
        document_ids=payload.document_ids,
        force=payload.force,
        strategy=AnalysisStrategy(payload.strategy),
    )

    results = [
        BulkJobResult(
            document_id=outcome.document_id,
            job=AnalysisJobResponse.model_validate(outcome.job) if outcome.job else None,
            skip_reason=outcome.skip_reason.value if outcome.skip_reason else None,
            error=outcome.message,
        )
        for outcome in outcomes
    ]
    started = sum(1 for r in results if r.job is not None)
    body = BulkAnalysisJobsResponse(
        started=started,
        skipped=len(results) - started,
        confirmation_required=sum(
            1 for o in outcomes if o.skip_reason is BulkSkipReason.CONFIRMATION_REQUIRED
        ),
        results=results,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if started else status.HTTP_200_OK,
        content=body.model_dump(mode="json"),
    )
