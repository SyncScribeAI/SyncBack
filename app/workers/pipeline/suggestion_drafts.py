"""Черновики правок ML 2.0 → ORM-модели Suggestion.

Проверки применимости уже сделаны доменным маппером (analysis_result_mapper); здесь только
перенос полей. Атрибуции правки к источнику в контракте нет, поэтому у каждой правки —
все источники, ушедшие в запрос.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from app.domain.services.analysis_result_mapper import SuggestionDraft
from app.infrastructure.db.models.enums import ChangeType
from app.infrastructure.db.models.source import Source
from app.infrastructure.db.models.suggestion import Suggestion


def to_suggestions(
    drafts: Iterable[SuggestionDraft],
    *,
    job_id: uuid.UUID,
    document_id: uuid.UUID,
    sources: Sequence[Source],
) -> list[Suggestion]:
    """O(S·K): у каждой правки свой список источников — общий список SQLAlchemy не разделяет."""
    return [
        Suggestion(
            analysis_job_id=job_id,
            document_id=document_id,
            section_ref=draft.section_ref,
            block_id=draft.block_ref,
            start_offset=draft.start_offset,
            end_offset=draft.end_offset,
            position=draft.position,
            change_type=ChangeType(draft.change_type.value),
            original_text=draft.original_text,
            suggested_text=draft.suggested_text,
            rationale=draft.rationale,
            confidence_score=draft.confidence_score,
            sources=list(sources),
        )
        for draft in drafts
    ]
