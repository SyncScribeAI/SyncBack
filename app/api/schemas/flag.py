"""Схемы флагов «проверьте вручную»: ответ редактору и PATCH отметки."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FlagQuoteResponse(BaseModel):
    """Цитата источника, из-за которой место нужно проверить."""

    source_name: str = Field(description="Имя источника, как его вернул конвейер")
    text: str


class AnalysisFlagResponse(BaseModel):
    """Место документа, которое конвейер просит проверить человеку: правки нет."""

    id: uuid.UUID
    document_id: uuid.UUID
    analysis_job_id: uuid.UUID
    block_id: str = Field(description="ref блока из content.blocks — по нему подсвечивается место")
    section_ref: str
    reason: str = Field(
        description=(
            "Код причины от конвейера (например, source_conflict); неизвестный код"
            " показывается как «проверьте вручную»"
        )
    )
    original_text: str = Field(description="Текст блока на момент анализа")
    rationale: str | None = None
    quotes: list[FlagQuoteResponse] = []
    position: int = Field(description="Порядок флага в ответе конвейера")
    status: Literal["pending", "reviewed"]
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PatchFlagRequest(BaseModel):
    status: Literal["pending", "reviewed"] = Field(
        description="reviewed — место просмотрено; pending — снять отметку"
    )
