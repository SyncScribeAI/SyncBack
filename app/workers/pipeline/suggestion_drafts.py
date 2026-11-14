"""Черновики правок ML 2.0 → ORM-модели Suggestion.

Проверки применимости уже сделаны доменным маппером (analysis_result_mapper); здесь только
перенос полей. Источники правки берутся из её оснований: правка ссылается на те источники,
цитаты из которых нашлись. Если ни одна цитата не нашлась, у правки все источники запроса,
как было до оснований: атрибуции в самом контракте нет.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence

from app.domain.evidence import EvidenceSource, evidence_to_json
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
    evidence_sources: Mapping[int, EvidenceSource] | None = None,
) -> list[Suggestion]:
    """O(S·K): у каждой правки свой список источников, общий список SQLAlchemy не разделяет."""
    rows = evidence_sources or {}
    by_id = {source.id: source for source in sources}
    return [
        Suggestion(
            analysis_job_id=job_id,
            document_id=document_id,
            section_ref=draft.section_ref,
            block_id=draft.block_ref,
            start_offset=draft.start_offset,
            end_offset=draft.end_offset,
            position=draft.position,
            group_key=draft.group_key,
            origin=draft.origin.value,
            change_type=ChangeType(draft.change_type.value),
            original_text=draft.original_text,
            suggested_text=draft.suggested_text,
            rationale=draft.rationale,
            confidence_score=draft.confidence_score,
            sources=_cited_sources(draft, rows, by_id) or list(sources),
            evidence=evidence_to_json(draft.evidence, rows) if draft.evidence else None,
        )
        for draft in drafts
    ]


def _cited_sources(
    draft: SuggestionDraft,
    rows: Mapping[int, EvidenceSource],
    by_id: Mapping[uuid.UUID, Source],
) -> list[Source]:
    cited: dict[uuid.UUID, Source] = {}
    for item in draft.evidence:
        if not item.found or item.source_position is None:
            continue
        row = rows.get(item.source_position)
        source = None if row is None or row.source_id is None else by_id.get(row.source_id)
        if source is not None:
            cited.setdefault(source.id, source)
    return list(cited.values())
