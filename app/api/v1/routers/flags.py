"""Флаги «проверьте вручную» текущего анализа документа.

GET   /projects/{project_id}/documents/{document_id}/flags
PATCH /projects/{project_id}/documents/{document_id}/flags/{flag_id}

Те же флаги приходят в GET /editor; отдельный список нужен, чтобы обновить панель
флагов без перечитывания всего агрегата. Аудит пишется после фиксации отметки и не
блокирует ответ, как у решений по правкам.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_allowed_project, get_current_user
from app.api.schemas.flag import AnalysisFlagResponse, PatchFlagRequest
from app.core.dependencies import get_analysis_flag_service, get_audit_log_service
from app.domain.exceptions import (
    AnalysisFlagNotFoundError,
    DocumentNotFoundError,
    InvalidDocumentStatusError,
)
from app.domain.services.analysis_flag_service import AnalysisFlagService
from app.domain.services.audit_log_service import AuditLogService
from app.domain.value_objects import AuditActionVO, FlagStatusVO
from app.infrastructure.db.models.project import Project
from app.infrastructure.db.models.user import User

logger = logging.getLogger("syncscribe.api.flags")

router = APIRouter(
    prefix="/projects/{project_id}/documents/{document_id}/flags",
    tags=["flags"],
)

_AUDIT_ACTIONS: dict[FlagStatusVO, AuditActionVO] = {
    FlagStatusVO.REVIEWED: AuditActionVO.FLAG_REVIEW,
    FlagStatusVO.PENDING: AuditActionVO.FLAG_REOPEN,
}


@router.get("", response_model=list[AnalysisFlagResponse])
async def list_flags(
    document_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    flag_service: AnalysisFlagService = Depends(get_analysis_flag_service),
) -> list[AnalysisFlagResponse]:
    """Флаги текущего анализа в порядке ответа конвейера; без анализа — пустой список."""
    try:
        flags = await flag_service.list_for_document(project.id, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return [AnalysisFlagResponse.model_validate(flag) for flag in flags]


@router.patch("/{flag_id}", response_model=AnalysisFlagResponse)
async def patch_flag(
    document_id: uuid.UUID,
    flag_id: uuid.UUID,
    payload: PatchFlagRequest,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    flag_service: AnalysisFlagService = Depends(get_analysis_flag_service),
    audit_log_service: AuditLogService = Depends(get_audit_log_service),
) -> AnalysisFlagResponse:
    """Отметить флаг просмотренным или снять отметку.

    Повтор того же статуса — 200 без нового события аудита. Флаг прежнего анализа — 404,
    как правка прежнего анализа. Документ без результатов анализа — 409. Отметка не
    меняет статус документа и не влияет на экспорт.
    """
    target = FlagStatusVO(payload.status)
    try:
        change = await flag_service.set_status(
            project.id, document_id, flag_id, target, current_user.id
        )
    except (DocumentNotFoundError, AnalysisFlagNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidDocumentStatusError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Ответ собирается до аудита: сбой аудита откатывает общую сессию запроса.
    response = AnalysisFlagResponse.model_validate(change.flag)
    if change.changed:
        try:
            await audit_log_service.log_flag_decision(
                current_user.id, document_id, flag_id, _AUDIT_ACTIONS[target]
            )
        except Exception:
            logger.warning(
                "Не удалось записать audit_log для отметки флага",
                exc_info=True,
                extra={"flag_id": str(flag_id), "user_id": str(current_user.id)},
            )
    return response
