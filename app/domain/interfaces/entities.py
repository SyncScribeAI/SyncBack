"""
Доменные Protocol-интерфейсы для сущностей.

Зачем:
  Domain-сервисы должны работать с абстракциями (портами), а не с ORM-моделями.
  Этот файл определяет минимальные Protocol-контракты, которыми
  должны удовлетворять объекты, передаваемые в сервисы.

Правило:
  - В Protocol входят только атрибуты/методы, которые domain-сервис реально читает.
  - Не нужно дублировать весь ORM-контракт — только минимальную поверхность.
  - SQLAlchemy-модели удовлетворяют Protocol-ам структурно — никаких
    изменений в конкретных реализациях не требуется.
  - @runtime_checkable позволяет использовать isinstance() в тестах.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.value_objects import DocumentStatusVO, SuggestionStatusVO


@runtime_checkable
class DocumentProtocol(Protocol):
    """Минимальный контракт документа, нужный domain-сервисам.

    Удовлетворяется любым объектом, у которого есть эти атрибуты —
    в том числе SQLAlchemy Document и test fakes.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    status: DocumentStatusVO
    current_analysis_job_id: uuid.UUID | None
    review_version: int


@runtime_checkable
class SuggestionProtocol(Protocol):
    """Минимальный контракт правки, нужный domain-сервисам.

    Покрывает атрибуты, читаемые в SuggestionService
    и DocumentExportService.
    """

    id: uuid.UUID
    analysis_job_id: uuid.UUID
    section_ref: str
    original_text: str | None
    suggested_text: str | None
    # Текст, которым пользователь заменил suggested_text при принятии.
    edited_text: str | None
    # Границы original_text в plain_text; None у правок прежнего конвейера.
    start_offset: int | None
    end_offset: int | None
    status: SuggestionStatusVO
    decided_by: uuid.UUID | None
    decided_at: datetime | None

    # Только чтение: у ORM-модели это инфраструктурный StrEnum, домену хватает строки.
    @property
    def change_type(self) -> str: ...


@runtime_checkable
class AnalysisFlagProtocol(Protocol):
    """Минимальный контракт флага ручной проверки, нужный AnalysisFlagService."""

    id: uuid.UUID
    analysis_job_id: uuid.UUID
    document_id: uuid.UUID
    # У ORM-модели строка с CHECK, а не enum PostgreSQL; домен сравнивает с FlagStatusVO.
    status: str
    decided_by: uuid.UUID | None
    decided_at: datetime | None


@runtime_checkable
class UserProtocol(Protocol):
    """Минимальный контракт пользователя, нужный AuthService.

    Нужен, чтобы AuthService не импортировал ORM-модель User.

    Атрибуты только для чтения: так протоколу удовлетворяет и ORM-модель,
    у которой role — StrEnum (подтип str). AuthService передаёт role
    в JWTHandler.create_access_token(role=...).
    """

    @property
    def id(self) -> uuid.UUID: ...

    @property
    def email(self) -> str: ...

    @property
    def password_hash(self) -> str: ...

    @property
    def role(self) -> str: ...
