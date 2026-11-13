"""Схемы правок (suggestions): ответ редактору и единый PATCH статусов."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


class SuggestionSourceResponse(BaseModel):
    """Краткие данные источника, на котором основана правка."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    type: Literal["url", "file"]
    url: str | None = None

    model_config = {"from_attributes": True}


class EvidenceResponse(BaseModel):
    """Цитата источника, на которой основана правка, и её место в снимке источника."""

    source_id: uuid.UUID | None = Field(
        default=None, description="Источник цитаты; None, если источник не определён или удалён"
    )
    source_name: str = Field(description="Имя источника на момент анализа или из цитаты")
    snapshot_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Снимок источника, в котором искалась цитата:"
            " GET /api/v1/projects/{project_id}/source-snapshots/{snapshot_id}/content"
        ),
    )
    quote: str = Field(description="Цитата в том виде, в каком её вернул конвейер")
    match: Literal["exact", "approximate", "not_found"] = Field(
        description=(
            "exact — найден диапазон цитаты; approximate — цитата пересказана, диапазон"
            " покрывает сегмент; not_found — места в снимке нет"
        )
    )
    start_offset: int | None = Field(
        default=None, description="Начало в тексте снимка, в символах Unicode"
    )
    end_offset: int | None = Field(default=None, description="Конец в тексте снимка, не включая")
    segment_index: int | None = Field(default=None, description="Номер сегмента снимка")
    locator: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Место в исходном файле. docx_paragraphs: first и last, порядковые номера"
            " непустых абзацев файла, оба включены"
        ),
    )
    original_file_available: bool = Field(
        default=False,
        description=(
            "Файл источника тот же, что разбирался при анализе: подсветку можно показать"
            " в оригинале; иначе только в тексте снимка"
        ),
    )


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
    group_key: str | None = Field(
        default=None,
        description=(
            "Ключ группы правок одной фразы (тип, old_text и new_text без учёта регистра,"
            " пробелов и «ё/е»); None — у правки нет пары. Все правки группы:"
            " GET …/suggestions?group_key=…"
        ),
    )
    origin: Literal["model", "propagated"] = Field(
        default="model",
        description="model — правка конвейера; propagated — найдена бэкендом по точному повтору",
    )
    evidence: list[EvidenceResponse] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidence_view", "evidence"),
        description="Основания правки; пусто, если цитаты не извлеклись или анализ старый",
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

    @field_validator("origin", mode="before")
    @classmethod
    def _origin_before_insert(cls, value: object) -> object:
        return "model" if value is None else value


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
