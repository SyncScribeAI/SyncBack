"""
Схемы ответов для editor-роутера.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.api.schemas.document import DocumentSectionResponse, SuggestionCounters
from app.api.schemas.flag import AnalysisFlagResponse
from app.api.schemas.suggestion import SuggestionResponse
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.value_objects import DocumentStatusVO


class EditorDocumentMeta(BaseModel):
    id: uuid.UUID
    title: str
    format: str
    status: DocumentStatusVO
    current_analysis_job_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    review_version: int = 0
    view_mode: str = "original"


class EditorBlockResponse(BaseModel):
    """Блок документа без текста: текст фронт берёт из plain_text по смещениям,
    чтобы ответ не дублировал документ целиком."""

    ref: str
    type: DocumentBlockType
    start_offset: int
    end_offset: int
    editable: bool
    heading_level: int | None = None


class PageSizeResponse(BaseModel):
    """Размер страницы в точках с учётом CropBox и поворота."""

    number: int
    width: float
    height: float


class EditorContent(BaseModel):
    plain_text: str
    sections: list[DocumentSectionResponse] = []
    blocks: list[EditorBlockResponse] = []
    pages: list[PageSizeResponse] | None = Field(
        default=None,
        description=(
            "Только у PDF: размеры страниц в порядке просмотра, чтобы клиент разметил "
            "страницы до отрисовки и наложил прямоугольники geometry"
        ),
    )


class EditorPermissions(BaseModel):
    can_analyze: bool
    can_review: bool
    can_export: bool
    can_delete: bool
    sources_is_editable: bool = True


class SuggestionGroupResponse(BaseModel):
    """Группа правок одной фразы: сколько в ней правок и сколько ещё не решено."""

    group_key: str
    size: int
    pending: int

    model_config = {"from_attributes": True}


class EditorCounters(SuggestionCounters):
    """Счётчики редактора: к счётчикам правок добавлены флаги.

    В списке документов флагов нет, поэтому поле не входит в SuggestionCounters.
    """

    flags_pending: int = Field(
        default=0,
        description=(
            "Флаги «проверьте вручную» без отметки; не влияют на переход документа в ready"
        ),
    )


class EditorAggregateResponse(BaseModel):
    document: EditorDocumentMeta
    content: EditorContent | None = None
    original_content: EditorContent | None = None
    suggestions: list[SuggestionResponse] = []
    suggestions_total: int = 0
    counters: EditorCounters
    permissions: EditorPermissions
    suggestion_groups: list[SuggestionGroupResponse] = Field(
        default_factory=list,
        description=(
            "Группы правок текущего анализа независимо от страницы suggestions —"
            " для «ещё N таких» без загрузки всех страниц"
        ),
    )
    flags: list[AnalysisFlagResponse] = Field(
        default_factory=list,
        description=(
            "Флаги «проверьте вручную» текущего анализа целиком, в порядке ответа конвейера;"
            " число ограничено ANALYSIS_MAX_FLAGS"
        ),
    )


class ResetResponse(BaseModel):
    document_id: uuid.UUID
    document_status: str
    review_version: int
    suggestions_reset_count: int
    flags_reset_count: int = 0


class CancelAllSuggestionsResponse(BaseModel):
    """Ответ DELETE /editor/suggestions.

    cancelled_count — число pending-правок, переведённых в rejected.
    """

    document_id: uuid.UUID
    cancelled_count: int
