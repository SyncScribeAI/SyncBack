"""Черновики флагов ML 2.0 → ORM-модели AnalysisFlag.

Проверки (блок существует, лимит числа флагов, длина причины) уже сделаны доменным
маппером; здесь только перенос полей. Цитата флага хранится вместе со своим основанием:
просмотр источника подсвечивает её так же, как основание правки.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from app.domain.evidence import EvidenceSource, evidence_to_json
from app.domain.services.analysis_result_mapper import FlagDraft
from app.infrastructure.db.models.analysis_flag import AnalysisFlag


def to_flags(
    drafts: Iterable[FlagDraft],
    *,
    job_id: uuid.UUID,
    document_id: uuid.UUID,
    evidence_sources: Mapping[int, EvidenceSource] | None = None,
) -> list[AnalysisFlag]:
    rows = evidence_sources or {}
    return [
        AnalysisFlag(
            analysis_job_id=job_id,
            document_id=document_id,
            block_id=draft.block_ref,
            section_ref=draft.section_ref,
            reason=draft.reason,
            original_text=draft.original_text,
            rationale=draft.rationale,
            quotes=_quotes(draft, rows),
            position=draft.position,
        )
        for draft in drafts
    ]


def _quotes(draft: FlagDraft, rows: Mapping[int, EvidenceSource]) -> list[dict[str, Any]]:
    """source_name и text как раньше; поля основания добавляются, когда локатор отработал."""
    if not draft.evidence:
        return [{"source_name": q.source_name, "text": q.text} for q in draft.quotes]
    quotes: list[dict[str, Any]] = []
    for quote, row in zip(draft.quotes, evidence_to_json(draft.evidence, rows), strict=True):
        row.pop("file_storage_key", None)
        row.pop("quote", None)
        quotes.append({**row, "source_name": quote.source_name, "text": quote.text})
    return quotes
