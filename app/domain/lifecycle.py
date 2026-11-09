"""Жизненные циклы документа и задачи анализа.

Единственное место, где описано, какие переходы статусов допустимы и что
можно делать с документом в каждом статусе. Сервисы, воркер и роутеры
читают правила отсюда и не держат собственных наборов статусов.

Документ (по UI-спецификации):
  draft → in_progress               запуск анализа
  in_progress → awaiting_approval   анализ завершён, есть правки
  in_progress → ready               анализ завершён, правок нет
  in_progress → draft               анализ упал или отменён
  awaiting_approval → ready         все правки рассмотрены
  awaiting_approval → draft         повторный анализ
  ready → draft                     повторный анализ (требует подтверждения)
  ready → awaiting_approval         сброс решений по правкам
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.domain.exceptions import InvalidDocumentStatusError
from app.domain.interfaces.document_analyzer import (
    AnalysisErrorCode,
    AnalysisResult,
    AnalysisStatus,
)
from app.domain.value_objects import AnalysisJobStatusVO, DocumentStatusVO

_DOCUMENT_TRANSITIONS: dict[DocumentStatusVO, frozenset[DocumentStatusVO]] = {
    DocumentStatusVO.DRAFT: frozenset({DocumentStatusVO.DRAFT, DocumentStatusVO.IN_PROGRESS}),
    DocumentStatusVO.IN_PROGRESS: frozenset(
        {DocumentStatusVO.AWAITING_APPROVAL, DocumentStatusVO.READY, DocumentStatusVO.DRAFT}
    ),
    DocumentStatusVO.AWAITING_APPROVAL: frozenset(
        {DocumentStatusVO.READY, DocumentStatusVO.DRAFT, DocumentStatusVO.AWAITING_APPROVAL}
    ),
    DocumentStatusVO.READY: frozenset({DocumentStatusVO.DRAFT, DocumentStatusVO.AWAITING_APPROVAL}),
}


def _raw_value(status: object) -> str:
    value = status.value if isinstance(status, Enum) else status
    if not isinstance(value, str):
        raise TypeError(f"Ожидался статус (enum или строка), получено {status!r}")
    return value


def _as_document_status(status: object) -> DocumentStatusVO:
    """Статус из ORM-enum или строки — домен работает только с VO."""
    if isinstance(status, DocumentStatusVO):
        return status
    return DocumentStatusVO(_raw_value(status))


def _as_job_status(status: object) -> AnalysisJobStatusVO:
    if isinstance(status, AnalysisJobStatusVO):
        return status
    return AnalysisJobStatusVO(_raw_value(status))


class DocumentLifecycle:
    """Переходы и допустимые действия документа."""

    # Анализ можно запустить из любого «спокойного» статуса; из ready — только
    # с подтверждением, потому что результаты предыдущего ревью будут сброшены.
    ANALYZABLE = frozenset(
        {DocumentStatusVO.DRAFT, DocumentStatusVO.AWAITING_APPROVAL, DocumentStatusVO.READY}
    )
    ANALYSIS_NEEDS_CONFIRMATION = frozenset({DocumentStatusVO.READY})
    # Источники заморожены только пока идёт анализ: воркер читает их во время задачи.
    # На ревью и после него источники можно менять; уже созданные правки от этого не исчезают
    # и живут до следующего анализа.
    SOURCES_LOCKED = frozenset({DocumentStatusVO.IN_PROGRESS})
    REVIEW_RESETTABLE = frozenset({DocumentStatusVO.AWAITING_APPROVAL, DocumentStatusVO.READY})
    # В этих статусах содержимое документа отличается от исходника правками анализа.
    WITH_REVIEW_RESULTS = frozenset({DocumentStatusVO.AWAITING_APPROVAL, DocumentStatusVO.READY})

    @staticmethod
    def transition(current: object, target: DocumentStatusVO) -> DocumentStatusVO:
        """Проверить переход и вернуть целевой статус; иначе InvalidDocumentStatusError."""
        current_vo = _as_document_status(current)
        if target not in _DOCUMENT_TRANSITIONS[current_vo]:
            raise InvalidDocumentStatusError(
                f"Переход документа {current_vo.value} → {target.value} недопустим"
            )
        return target

    @classmethod
    def can_start_analysis(cls, status: object) -> bool:
        return _as_document_status(status) in cls.ANALYZABLE

    @classmethod
    def analysis_needs_confirmation(cls, status: object) -> bool:
        return _as_document_status(status) in cls.ANALYSIS_NEEDS_CONFIRMATION

    @classmethod
    def can_edit_sources(cls, status: object) -> bool:
        return _as_document_status(status) not in cls.SOURCES_LOCKED

    @staticmethod
    def can_review(status: object) -> bool:
        return _as_document_status(status) is DocumentStatusVO.AWAITING_APPROVAL

    @staticmethod
    def can_export(status: object) -> bool:
        return _as_document_status(status) is DocumentStatusVO.READY

    @staticmethod
    def can_delete(status: object) -> bool:
        return _as_document_status(status) is not DocumentStatusVO.IN_PROGRESS

    @classmethod
    def can_reset_review(cls, status: object) -> bool:
        return _as_document_status(status) in cls.REVIEW_RESETTABLE

    @classmethod
    def has_review_results(cls, status: object) -> bool:
        return _as_document_status(status) in cls.WITH_REVIEW_RESULTS


_JOB_TRANSITIONS: dict[AnalysisJobStatusVO, frozenset[AnalysisJobStatusVO]] = {
    AnalysisJobStatusVO.PENDING: frozenset(
        {AnalysisJobStatusVO.DISPATCHED, AnalysisJobStatusVO.FAILED, AnalysisJobStatusVO.CANCELLED}
    ),
    # Из очереди задача может сразу завершиться (например, без источников).
    AnalysisJobStatusVO.DISPATCHED: frozenset(
        {
            AnalysisJobStatusVO.PROCESSING,
            AnalysisJobStatusVO.SUCCESS,
            AnalysisJobStatusVO.PARTIAL_SUCCESS,
            AnalysisJobStatusVO.FAILED,
            AnalysisJobStatusVO.CANCELLED,
        }
    ),
    AnalysisJobStatusVO.PROCESSING: frozenset(
        {
            AnalysisJobStatusVO.SUCCESS,
            AnalysisJobStatusVO.PARTIAL_SUCCESS,
            AnalysisJobStatusVO.FAILED,
            AnalysisJobStatusVO.CANCELLED,
        }
    ),
    AnalysisJobStatusVO.SUCCESS: frozenset(),
    AnalysisJobStatusVO.PARTIAL_SUCCESS: frozenset(),
    AnalysisJobStatusVO.FAILED: frozenset(),
    AnalysisJobStatusVO.CANCELLED: frozenset(),
}


# Пределы совпадают с соглашением по логам и error_message задачи.
_ERROR_MESSAGE_MAX = 500


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Исход задачи по результату конвейера.

    retryable — сбой временный, задачу можно повторить; сколько раз — решает воркер.
    """

    status: AnalysisJobStatusVO
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


class AnalysisJobLifecycle:
    """Переходы задачи анализа и их отражение на статусе документа.

    pending — создана, ещё не в очереди; dispatched — в очереди;
    processing — воркер начал обработку; остальные статусы терминальные.
    """

    ACTIVE = frozenset(
        {
            AnalysisJobStatusVO.PENDING,
            AnalysisJobStatusVO.DISPATCHED,
            AnalysisJobStatusVO.PROCESSING,
        }
    )
    TERMINAL = frozenset(
        {
            AnalysisJobStatusVO.SUCCESS,
            AnalysisJobStatusVO.PARTIAL_SUCCESS,
            AnalysisJobStatusVO.FAILED,
            AnalysisJobStatusVO.CANCELLED,
        }
    )

    @staticmethod
    def transition(current: object, target: AnalysisJobStatusVO) -> AnalysisJobStatusVO:
        current_vo = _as_job_status(current)
        if target not in _JOB_TRANSITIONS[current_vo]:
            raise InvalidDocumentStatusError(
                f"Переход задачи анализа {current_vo.value} → {target.value} недопустим"
            )
        return target

    @classmethod
    def is_active(cls, status: object) -> bool:
        return _as_job_status(status) in cls.ACTIVE

    @classmethod
    def is_terminal(cls, status: object) -> bool:
        return _as_job_status(status) in cls.TERMINAL

    @classmethod
    def can_cancel(cls, status: object) -> bool:
        return cls.is_active(status)

    @staticmethod
    def can_dispatch(status: object) -> bool:
        return _as_job_status(status) is AnalysisJobStatusVO.PENDING

    @staticmethod
    def document_status_for(
        job_status: AnalysisJobStatusVO, *, has_pending_suggestions: bool = False
    ) -> DocumentStatusVO:
        """Статус документа, соответствующий статусу его текущей задачи анализа."""
        if job_status in (AnalysisJobStatusVO.DISPATCHED, AnalysisJobStatusVO.PROCESSING):
            return DocumentStatusVO.IN_PROGRESS
        if job_status in (AnalysisJobStatusVO.SUCCESS, AnalysisJobStatusVO.PARTIAL_SUCCESS):
            return (
                DocumentStatusVO.AWAITING_APPROVAL
                if has_pending_suggestions
                else DocumentStatusVO.READY
            )
        return DocumentStatusVO.DRAFT

    @staticmethod
    def outcome_for(result: AnalysisResult, failed_sources: Sequence[str] = ()) -> JobOutcome:
        """Исход задачи анализа v2.

        failed — по коду ошибки: invalid_request означает ошибку нашего запроса, повтор не
        поможет; остальное — временный сбой конвейера. completed — успех, если проверены все
        блоки и все источники дошли до запроса; иначе partial_success: правки сохраняются,
        но пользователь должен знать, что часть документа или источников не учтена.
        """
        if result.status is AnalysisStatus.FAILED:
            message = _truncate(result.error.message if result.error else "")
            if result.error and result.error.code is AnalysisErrorCode.INVALID_REQUEST:
                return JobOutcome(AnalysisJobStatusVO.FAILED, "ML_INVALID_REQUEST", message)
            return JobOutcome(AnalysisJobStatusVO.FAILED, "LLM_UNAVAILABLE", message, True)
        problems: list[str] = []
        failed_blocks = _failed_block_count(result.meta)
        if failed_blocks:
            problems.append(f"{failed_blocks} блоков не удалось проверить")
        if failed_sources:
            problems.append("недоступны источники: " + ", ".join(failed_sources))
        if problems:
            return JobOutcome(
                AnalysisJobStatusVO.PARTIAL_SUCCESS,
                "PARTIAL_FAILURE",
                _truncate("; ".join(problems)),
            )
        return JobOutcome(AnalysisJobStatusVO.SUCCESS)


def _failed_block_count(meta: Mapping[str, Any]) -> int:
    """meta.errors читается мягко: состав meta не зафиксирован контрактом."""
    value = meta.get("errors")
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        return 0
    return max(int(value), 0)


def _truncate(message: str) -> str | None:
    if not message:
        return None
    return (
        message if len(message) <= _ERROR_MESSAGE_MAX else message[: _ERROR_MESSAGE_MAX - 1] + "…"
    )
