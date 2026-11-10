"""Правки документа: список, решения (PATCH — single и bulk), сохранение ревью, сброс.

Запись в аудит идёт после фиксации решения и не блокирует ответ: сбой аудита
логируется, но пользователь получает успешный результат.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import TypeAdapter

from app.api.deps import get_allowed_project, get_current_user
from app.api.schemas.pagination import Page
from app.api.schemas.review import ReviewSaveRequest, ReviewSaveResponse
from app.api.schemas.suggestion import (
    PatchSuggestionsRequest,
    PatchSuggestionsResponse,
    SuggestionResponse,
)
from app.core.dependencies import get_audit_log_service, get_suggestion_service
from app.domain.exceptions import (
    DocumentNotFoundError,
    InvalidDocumentStatusError,
    OptimisticLockError,
    ReviewNotCompleteError,
    StaleSuggestionJobError,
    SuggestionAlreadyDecidedError,
    SuggestionNotFoundError,
    SuggestionResetNotAllowedError,
)
from app.domain.services.audit_log_service import AuditLogService
from app.domain.services.suggestion_service import SuggestionService
from app.domain.value_objects import (
    AuditActionVO,
    PaginationParams,
    SuggestionStatusVO,
)
from app.infrastructure.db.models.project import Project
from app.infrastructure.db.models.user import User

logger = logging.getLogger("syncscribe.api.suggestions")
_suggestion_list_adapter: TypeAdapter[list[SuggestionResponse]] = TypeAdapter(
    list[SuggestionResponse]
)

PageSuggestionResponse = Page[SuggestionResponse]

_SUGGESTION_STATUS_FILTER_MAP: dict[str, SuggestionStatusVO] = {
    vo.value: vo for vo in SuggestionStatusVO
}

router = APIRouter(
    prefix="/projects/{project_id}/documents/{document_id}/suggestions",
    tags=["suggestions"],
)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


async def _safe_bulk_log(
    audit_log_service: AuditLogService,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    decisions: list[tuple[uuid.UUID, AuditActionVO]],
) -> None:
    """Записать аудит после того, как решения уже зафиксированы.

    Ошибка аудита откатывает общую сессию запроса и просрочивает загруженные
    объекты, поэтому ответ нужно собрать до вызова.
    """
    if not decisions:
        return
    try:
        await audit_log_service.log_suggestion_decisions(user_id, document_id, decisions)
    except Exception:
        logger.warning(
            "Не удалось записать audit_log для решений по правкам",
            exc_info=True,
            extra={"user_id": str(user_id), "document_id": str(document_id)},
        )


async def _safe_single_log(
    audit_log_service: AuditLogService,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    action: AuditActionVO,
) -> None:
    try:
        await audit_log_service.log_suggestion_decision(user_id, document_id, suggestion_id, action)
    except Exception:
        logger.warning(
            "Не удалось записать audit_log для решения по правке",
            exc_info=True,
            extra={"suggestion_id": str(suggestion_id), "user_id": str(user_id)},
        )


_PATCH_AUDIT_ACTIONS: dict[SuggestionStatusVO, AuditActionVO] = {
    SuggestionStatusVO.ACCEPTED: AuditActionVO.ACCEPT,
    SuggestionStatusVO.REJECTED: AuditActionVO.REJECT,
    SuggestionStatusVO.PENDING: AuditActionVO.RESET,
}


def _parse_if_match(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    try:
        return int(if_match.strip('"').strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Заголовок If-Match должен содержать целочисленный "
                f"review_version, получено: {if_match!r}"
            ),
        ) from None


# ---------------------------------------------------------------------------
# GET /suggestions
# ---------------------------------------------------------------------------


@router.get("", response_model=PageSuggestionResponse)
async def list_suggestions(
    document_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=(
            "Фильтр по статусу правки. "
            f"Допустимые значения: {', '.join(_SUGGESTION_STATUS_FILTER_MAP)}"
        ),
    ),
    group_key: str | None = Query(
        default=None,
        max_length=64,
        description="Только правки одной группы (SuggestionResponse.group_key)",
    ),
    project: Project = Depends(get_allowed_project),
    suggestion_service: SuggestionService = Depends(get_suggestion_service),
) -> PageSuggestionResponse:
    """Список правок документа с опциональной фильтрацией по статусу и группе."""
    status_vo: SuggestionStatusVO | None = None
    if status_filter is not None:
        status_vo = _SUGGESTION_STATUS_FILTER_MAP.get(status_filter.lower())
        if status_vo is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Неподдерживаемый статус правки: {status_filter!r}. "
                    f"Допустимые значения: {', '.join(_SUGGESTION_STATUS_FILTER_MAP)}"
                ),
            )

    pagination = PaginationParams(limit=limit, offset=offset)
    try:
        suggestions, total = await suggestion_service.list_suggestions_for_document(
            project.id, document_id, pagination, status_filter=status_vo, group_key=group_key
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return PageSuggestionResponse(
        items=_suggestion_list_adapter.validate_python(suggestions, from_attributes=True),
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /suggestions/{suggestion_id}
# ---------------------------------------------------------------------------


@router.get("/{suggestion_id}", response_model=SuggestionResponse)
async def get_suggestion(
    document_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    suggestion_service: SuggestionService = Depends(get_suggestion_service),
) -> SuggestionResponse:
    """Получить одну правку по ID."""
    try:
        suggestion = await suggestion_service.get_suggestion_for_document(
            project.id, document_id, suggestion_id
        )
    except (DocumentNotFoundError, SuggestionNotFoundError, StaleSuggestionJobError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return SuggestionResponse.model_validate(suggestion)


# ---------------------------------------------------------------------------
# PATCH /suggestions — единственный endpoint изменения статуса (single + bulk)
# ---------------------------------------------------------------------------


@router.patch(
    "",
    response_model=PatchSuggestionsResponse,
    status_code=status.HTTP_200_OK,
    summary="Обновить статус правок (single или bulk)",
    responses={
        200: {"description": "Статус обновлён, возвращает кол-во изменённых правок"},
        400: {"description": "Невалидный запрос (оба или ни один selector)"},
        404: {"description": "Документ или правки не найдены"},
        409: {"description": "Конфликт статуса документа или правки"},
    },
)
async def patch_suggestions(
    document_id: uuid.UUID,
    payload: PatchSuggestionsRequest,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    suggestion_service: SuggestionService = Depends(get_suggestion_service),
    audit_log_service: AuditLogService = Depends(get_audit_log_service),
) -> PatchSuggestionsResponse:
    """Обновить статус правок одним запросом.

    Ровно одно из полей обязательно:
    - **ids** — список UUID для точечного обновления.
    - **filter** — предустановленный фильтр: `pending` / `decided` / `all`.

    Допустимые переходы:
    | Текущий | Целевой  | Семантика        |
    |---------|----------|------------------|
    | pending | accepted | принять          |
    | pending | rejected | отклонить        |
    | decided | pending  | сбросить решение |

    Принять или отклонить можно только в awaiting_approval. Сброс доступен и в
    ready: документ возвращается в awaiting_approval, новый статус — в document_status.
    """
    target_status = SuggestionStatusVO(payload.status)

    try:
        result = await suggestion_service.patch_suggestions(
            project_id=project.id,
            document_id=document_id,
            user_id=current_user.id,
            target_status=target_status,
            ids=payload.ids,
            filter=payload.filter,
        )
    except (DocumentNotFoundError, SuggestionNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        InvalidDocumentStatusError,
        SuggestionAlreadyDecidedError,
        SuggestionResetNotAllowedError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    response = PatchSuggestionsResponse(
        updated_count=result.updated_count,
        document_status=result.document.status.value,
        review_version=result.document.review_version,
    )
    audit_action = _PATCH_AUDIT_ACTIONS[target_status]
    await _safe_bulk_log(
        audit_log_service,
        current_user.id,
        document_id,
        [(sid, audit_action) for sid in result.updated_ids],
    )
    return response


# ---------------------------------------------------------------------------
# PUT /review — батч accept/reject с optimistic locking
# ---------------------------------------------------------------------------


@router.put("/review", response_model=ReviewSaveResponse)
async def review_save(
    document_id: uuid.UUID,
    payload: ReviewSaveRequest,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    suggestion_service: SuggestionService = Depends(get_suggestion_service),
    audit_log_service: AuditLogService = Depends(get_audit_log_service),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> ReviewSaveResponse:
    client_version = _parse_if_match(if_match)

    if (
        client_version is not None
        and payload.review_version is not None
        and client_version != payload.review_version
    ):
        logger.warning(
            "If-Match (%s) расходится с payload.review_version (%s) — используется If-Match",
            client_version,
            payload.review_version,
            extra={"document_id": str(document_id), "user_id": str(current_user.id)},
        )

    review_version = client_version if client_version is not None else payload.review_version

    accepted_ids: list[uuid.UUID] = []
    rejected_ids: list[uuid.UUID] = []
    edited_texts: dict[uuid.UUID, str] = {}
    audit_decisions: list[tuple[uuid.UUID, AuditActionVO]] = []
    for d in payload.decisions:
        if d.decision == "accepted":
            accepted_ids.append(d.suggestion_id)
            audit_decisions.append((d.suggestion_id, AuditActionVO.ACCEPT))
            if d.edited_text is not None:
                edited_texts[d.suggestion_id] = d.edited_text
        else:
            rejected_ids.append(d.suggestion_id)
            audit_decisions.append((d.suggestion_id, AuditActionVO.REJECT))

    try:
        result = await suggestion_service.atomic_review_save(
            project_id=project.id,
            document_id=document_id,
            user_id=current_user.id,
            review_version=review_version,
            accepted_ids=tuple(accepted_ids),
            rejected_ids=tuple(rejected_ids),
            finalize=payload.finalize,
            edited_texts=edited_texts,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except OptimisticLockError as exc:
        conflict_status = (
            status.HTTP_412_PRECONDITION_FAILED
            if client_version is not None
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=conflict_status, detail=str(exc)) from exc
    except SuggestionAlreadyDecidedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (InvalidDocumentStatusError, ReviewNotCompleteError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    doc = result.document
    response = ReviewSaveResponse(
        document_id=doc.id,
        document_status=doc.status.value,
        review_version=doc.review_version,
        accepted_count=result.accepted_count,
        rejected_count=result.rejected_count,
        pending_count=result.pending_count,
        finalized=result.finalized,
    )
    await _safe_bulk_log(audit_log_service, current_user.id, document_id, audit_decisions)
    return response


# ---------------------------------------------------------------------------
# POST /{suggestion_id}/reset
# ---------------------------------------------------------------------------


@router.post("/{suggestion_id}/reset", response_model=SuggestionResponse)
async def reset_suggestion(
    document_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    project: Project = Depends(get_allowed_project),
    current_user: User = Depends(get_current_user),
    suggestion_service: SuggestionService = Depends(get_suggestion_service),
    audit_log_service: AuditLogService = Depends(get_audit_log_service),
) -> SuggestionResponse:
    """Отменить ранее принятое/отклонённое решение — вернуть правку в PENDING.

    В готовом документе это возвращает его в awaiting_approval.

    Для массового сброса используйте PATCH /suggestions с {"ids": [...], "status": "pending"}.
    """
    try:
        suggestion = await suggestion_service.reset_suggestion(
            project.id, document_id, suggestion_id, current_user.id
        )
    except (DocumentNotFoundError, SuggestionNotFoundError, StaleSuggestionJobError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (InvalidDocumentStatusError, SuggestionResetNotAllowedError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    response = SuggestionResponse.model_validate(suggestion)
    await _safe_single_log(
        audit_log_service, current_user.id, document_id, suggestion.id, AuditActionVO.RESET
    )
    return response
