"""Перечисления уровня БД (типы PostgreSQL ENUM).

Значения совпадают с доменными VO из app.domain.value_objects. Где у типа БД
нет значений сверх домена (формат и статус документа, статус правки), используется сам VO —
так ORM-атрибуты удовлетворяют доменным протоколам без преобразований.
"""

import enum

from app.domain.value_objects import DocumentFormatVO as DocumentFormat  # noqa: F401
from app.domain.value_objects import DocumentStatusVO as DocumentStatus  # noqa: F401
from app.domain.value_objects import SuggestionStatusVO as SuggestionStatus  # noqa: F401


class UserRole(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"


class SourceType(enum.StrEnum):
    FILE = "file"
    URL = "url"


class AnalysisJobStatus(enum.StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    PROCESSING = "processing"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChangeType(enum.StrEnum):
    ADD = "add"
    MODIFY = "modify"
    DELETE = "delete"


class AuditAction(enum.StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    BULK_ACCEPT = "bulk_accept"
    BULK_REJECT = "bulk_reject"  # синхронизировано с AuditActionVO
    FINALIZE = "finalize"
    FINALIZE_REVIEW = "finalize_review"  # синхронизировано с AuditActionVO
    REOPEN = "reopen"
    RESET = "reset"  # синхронизировано с AuditActionVO
    DOWNLOAD = "download"
    FLAG_REVIEW = "flag_review"  # синхронизировано с AuditActionVO
    FLAG_REOPEN = "flag_reopen"
