"""Схемы правок (suggestions): ответ редактору и единый PATCH статусов."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SuggestionSourceResponse(BaseModel):
    """Краткие данные источника, на котором основана правка."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    type: Literal["url", "file"]
    url: str | None = None

    model_config = {"from_attributes": True}


class SuggestionResponse(BaseModel):
    """Правка анализа в том виде, в каком её показывает редактор."""

    id: uuid.UUID
    document_id: uuid.UUID
    analysis_job_id: uuid.UUID
    section_ref: str | None = None
    change_type: Literal["add", "modify", "delete"]
    # У add нет исходного текста, у delete — предлагаемого.
    original_text: str | None = None
    suggested_text: str | None = None
    edited_text: str | None = Field(
        default=None,
        description=(
            "Текст, которым пользователь заменил suggested_text при принятии; None — без правки"
        ),
    )
    rationale: str | None = Field(default=None, description="Обоснование правки от модели")
    confidence_score: float | None = Field(default=None, description="Уверенность модели, 0–1")
    sources: list[SuggestionSourceResponse] = Field(
        default_factory=list,
        description="Источники истины, на основании которых создана правка",
    )
    block_id: str | None = Field(
        default=None, description="ref блока документа (как в editor.blocks), если известен"
    )
    start_offset: int | None = Field(
        default=None,
        description=(
            "Начало original_text в plain_text документа, в символах Unicode (как смещения"
            " блоков редактора); None — правка не привязана к блоку"
        ),
    )
    end_offset: int | None = Field(default=None, description="Конец original_text, не включая")
    position: int = Field(
        default=0, description="Номер правки в ответе конвейера внутри одного анализа"
    )
    status: Literal["pending", "accepted", "rejected"]
    # None — решение ещё не принято или сброшено.
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("position", mode="before")
    @classmethod
    def _position_before_insert(cls, value: object) -> object:
        # default и server_default срабатывают только при INSERT: у правки до записи в БД здесь None.
        return 0 if value is None else value


# ---------------------------------------------------------------------------
# PATCH /suggestions — единый эндпоинт обновления статуса правок
# ---------------------------------------------------------------------------


class PatchSuggestionsRequest(BaseModel):
    """Тело запроса для PATCH /suggestions.

    Ровно одно из полей ids / filter обязательно.

    ids    — список UUID: точечное обновление конкретных правок.
    filter — предустановленный фильтр:
               "pending"  — все правки в статусе pending
               "decided"  — все accepted + rejected (для массового reset)
               "all"      — все правки документа
    status — целевой статус: "accepted" | "rejected" | "pending"
             "pending" = reset (сброс решения, аналог POST /{id}/reset).
    """

    ids: list[uuid.UUID] | None = None
    filter: Literal["pending", "decided", "all"] | None = None
    status: Literal["accepted", "rejected", "pending"]

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> "PatchSuggestionsRequest":
        has_ids = bool(self.ids)
        has_filter = self.filter is not None
        if has_ids == has_filter:  # оба True или оба False
            raise ValueError(
                "Укажите ровно одно из полей: 'ids' (список UUID) "
                'или \'filter\' ("pending"|"decided"|"all").'
            )
        return self


class PatchSuggestionsResponse(BaseModel):
    """Ответ PATCH /suggestions.

    updated_count   — количество фактически изменённых правок.
    document_status — новый статус документа (может измениться при finalize).
    review_version  — актуальная версия ревью для optimistic locking.
    """

    updated_count: int
    document_status: str | None = None
    review_version: int | None = None
