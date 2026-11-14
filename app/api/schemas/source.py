"""Схемы источников.

Два типа источников: file (загружается через multipart /sources/file) и url.
Текстовые заметки (/sources/note) сохраняются как файл и возвращаются с type=file.

В ответах есть url и created_at: фронтенд показывает дату загрузки и открывает url-источник
по адресу без отдельного запроса; файловые источники скачиваются через /file и читаются
через /content.
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class _ScopedSourceRequest(BaseModel):
    scope: Literal["project", "document"] = "project"
    document_id: uuid.UUID | None = Field(
        default=None, description="Документ, к которому прикрепляется источник (при scope=document)"
    )

    @model_validator(mode="after")
    def _document_scope_requires_document(self) -> Self:
        if self.scope == "document" and self.document_id is None:
            raise ValueError("Для источника со scope=document нужен document_id")
        return self


class SourceCreateRequest(_ScopedSourceRequest):
    name: str = Field(min_length=1, max_length=255)
    type: Literal["url"]
    url: str = Field(min_length=1, max_length=2048)


class NoteCreateRequest(_ScopedSourceRequest):
    name: str = Field(min_length=1, max_length=255)
    text_content: str = Field(min_length=1, max_length=200_000)


def _enum_value(v: object) -> str:
    if isinstance(v, Enum):
        return str(v.value)
    return str(v)


class SourceResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    type: Literal["url", "file"]
    scope: Literal["project", "document"]
    url: str | None = None
    created_at: datetime
    pages_without_text: list[int] | None = Field(
        default=None,
        description=(
            "Только в ответе на загрузку PDF: страницы без текстового слоя (с единицы). "
            "Файл принят, но текст этих страниц в анализ не попадёт"
        ),
    )

    model_config = {"from_attributes": True}

    _normalise_type = field_validator("type", "scope", mode="before")(_enum_value)


class SourceBadge(BaseModel):
    """Минимум для строки источника на карточке документа: название, тип, адрес, дата."""

    id: uuid.UUID
    name: str
    type: Literal["url", "file"]
    url: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}

    _normalise_type = field_validator("type", mode="before")(_enum_value)


class SourceContentResponse(BaseModel):
    """Содержимое источника для просмотра.

    preview_available=False — текста нет: url-источник открывается по адресу, файл
    неподдерживаемого формата можно только скачать; причина — в reason.
    """

    id: uuid.UUID
    name: str
    type: Literal["url", "file"]
    url: str | None = None
    created_at: datetime
    plain_text: str | None = None
    preview_available: bool
    reason: str | None = None

    _normalise_type = field_validator("type", mode="before")(_enum_value)


class SourceCheckResponse(BaseModel):
    """Итог пробной загрузки источника: ничего не сохраняется."""

    ok: bool
    chars: int
    error: str | None = None
