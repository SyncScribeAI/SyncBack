"""Запуск, просмотр и отмена задач анализа одного документа.

Повторный запуск с тем же Idempotency-Key возвращает уже созданную задачу.
Повторный анализ готового документа требует force=true, иначе 409 с
confirmation_required=true, чтобы клиент показал диалог подтверждения.
Параллельные запросы не приводят к 500: проигравший гонку получает 409
(конкурирующий запуск) или 200 с уже созданной задачей (тот же Idempotency-Key).
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.deps import get_allowed_project
from app.api.schemas.analysis_job import (
    AnalysisJobConflictResponse,
    AnalysisJobCreateRequest,
    AnalysisJobResponse,
)
from app.core.dependencies import get_analysis_job_service
from app.domain.analysis_reuse import AnalysisStrategy
from app.domain.exceptions import (
    AnalysisAlreadyRunningError,
    AnalysisConfirmationRequiredError,
    AnalysisJobAlreadyExistsError,
    AnalysisJobNotCancellableError,
    DocumentNotFoundError,
    InvalidDocumentStatusError,
)
from app.domain.services.analysis_job_service import AnalysisJobService
from app.infrastructure.db.models.project import Project

logger = logging.getLogger("syncscribe.api.analysis_jobs")

router = APIRouter(
    prefix="/projects/{project_id}/documents/{document_id}/analysis-jobs",
    tags=["analysis-jobs"],
)


def _job_response(job: Any, http_status: int = status.HTTP_201_CREATED) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=jsonable_encoder(AnalysisJobResponse.model_validate(job)),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"model": AnalysisJobResponse, "description": "Задача создана"},
        200: {
            "model": AnalysisJobResponse,
            "description": "Идемпотентный запрос — задача уже существует",
        },
        409: {
            "model": AnalysisJobConflictResponse,
            "description": "Анализ уже запущен или документ ready без force=true",
        },
    },
)
async def start_analysis_job(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    service: AnalysisJobService = Depends(get_analysis_job_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    body: AnalysisJobCreateRequest = AnalysisJobCreateRequest(),
) -> JSONResponse:
    if idempotency_key:
        existing = await service.find_job_by_idempotency_key(
            project.id, document_id, idempotency_key
        )
        if existing is not None:
            if existing.document_id != document_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Idempotency-Key уже использован для другого документа "
                        f"({existing.document_id}). Используйте уникальный ключ."
                    ),
                )
            return _job_response(existing, status.HTTP_200_OK)

    try:
        job = await service.create_job(
            project.id,
            document_id,
            idempotency_key=idempotency_key,
            force=body.force,
            strategy=AnalysisStrategy(body.strategy),
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AnalysisJobAlreadyExistsError as exc:
        # Параллельный запрос с тем же Idempotency-Key успел раньше: возвращаем
        # его задачу и не ставим в очередь второй раз — это делает победитель.
        return _job_response(exc.job, status.HTTP_200_OK)
    except AnalysisConfirmationRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=AnalysisJobConflictResponse(
                detail=str(exc), confirmation_required=True
            ).model_dump(),
        ) from exc
    except (AnalysisAlreadyRunningError, InvalidDocumentStatusError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Сбой очереди не является ошибкой запроса: сервис вернёт задачу в статусе
    # failed с кодом QUEUE_UNAVAILABLE, и клиент увидит причину в ответе.
    job = await service.dispatch_job(job)
    return _job_response(job)


@router.get("/{job_id}", response_model=AnalysisJobResponse)
async def get_analysis_job(
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    service: AnalysisJobService = Depends(get_analysis_job_service),
) -> AnalysisJobResponse:
    try:
        job = await service.get_job(project.id, document_id, job_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AnalysisJobResponse.model_validate(job)


@router.delete(
    "/{job_id}",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"model": AnalysisJobResponse, "description": "Задача отменена"},
        404: {"description": "Задача или документ не найдены"},
        409: {"description": "Задача не может быть отменена в текущем статусе"},
    },
    summary="Отменить задачу анализа",
)
async def cancel_analysis_job(
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    service: AnalysisJobService = Depends(get_analysis_job_service),
) -> AnalysisJobResponse:
    """Отменить задачу анализа.

    Возвращает 200 + AnalysisJobResponse с status=cancelled.
    """
    try:
        job = await service.cancel_job(project.id, document_id, job_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AnalysisJobNotCancellableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if job.celery_task_id:
        try:
            await service.revoke_celery_task(job.celery_task_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Не удалось отозвать Celery-задачу при отмене job",
                exc_info=True,
                extra={"celery_task_id": job.celery_task_id, "job_id": str(job_id)},
            )

    return AnalysisJobResponse.model_validate(job)
