"""Черновики флагов ML 2.0 → ORM-модели AnalysisFlag.

Проверки (блок существует, лимит числа флагов, длина причины) уже сделаны доменным
маппером; здесь только перенос полей.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from app.domain.services.analysis_result_mapper import FlagDraft
from app.infrastructure.db.models.analysis_flag import AnalysisFlag


def to_flags(
    drafts: Iterable[FlagDraft], *, job_id: uuid.UUID, document_id: uuid.UUID
) -> list[AnalysisFlag]:
    return [
        AnalysisFlag(
            analysis_job_id=job_id,
            document_id=document_id,
            block_id=draft.block_ref,
            section_ref=draft.section_ref,
            reason=draft.reason,
            original_text=draft.original_text,
            rationale=draft.rationale,
            quotes=[{"source_name": q.source_name, "text": q.text} for q in draft.quotes],
            position=draft.position,
        )
        for draft in drafts
    ]
