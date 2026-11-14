"""Выборочный запрос инкрементального анализа и проверка правок по его ответу.

Чистые функции: отпечатки прошлого входа, сводки прошлого результата и тексты
источников уже загружены воркером. Решения о пределах принимает app.domain.analysis_reuse,
выбор блоков и сегментов делает app.domain.services.delta_selection.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import chain

from app.domain.analysis_reuse import (
    Baseline,
    DeltaFallback,
    DeltaPolicy,
    DeltaSummary,
    RunStats,
    delta_scope_check,
)
from app.domain.baseline_result import BaselineFlag, BaselineSuggestion
from app.domain.interfaces.document_analyzer import ChangeKind, SourcePayload
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.services.analysis_request_builder import DocumentInput
from app.domain.services.analysis_result_mapper import SuggestionDraft
from app.domain.services.delta_merge import evidence_changed
from app.domain.services.delta_selection import (
    BlockText,
    DeltaLimits,
    SelectedBlocks,
    SourceInput,
    reduce_sources,
    select_blocks,
)
from app.domain.services.source_differ import diff_fingerprints
from app.domain.source_snapshot import SourceSegment

Segments = Sequence[tuple[SourceSegment, int]]


@dataclass(frozen=True, slots=True)
class BaselineFingerprints:
    """Отпечатки прошлого входа: все вместе для differ и по снимкам для оснований."""

    all: frozenset[int]
    by_snapshot: Mapping[str, Sequence[int]]


def summarize_delta(base: frozenset[int], segments: Sequence[Segments]) -> DeltaSummary:
    delta = diff_fingerprints(base, chain.from_iterable(segments))
    return DeltaSummary(
        changed_share=delta.changed_share,
        segments_added=len(delta.added),
        segments_removed=len(delta.removed_fingerprints),
    )


@dataclass(frozen=True, slots=True)
class DeltaRequest:
    """Выборочный запрос и всё, что нужно для сведения ответа с прошлым результатом."""

    baseline: Baseline
    base: frozenset[int]
    selected: SelectedBlocks
    sources: list[SourcePayload]
    stats: RunStats
    summary: DeltaSummary
    suggestions: list[BaselineSuggestion]
    flags: list[BaselineFlag]
    # id прошлых правок, основание которых изменилось или исчезло, и их блоки вместе с
    # блоками таких флагов.
    changed: frozenset[uuid.UUID]
    evidence_blocks: frozenset[str] = frozenset()

    @property
    def calls_model(self) -> bool:
        return bool(self.sources)

    @property
    def rechecked(self) -> frozenset[str]:
        """Блоки, прошлый результат которых заменяется ответом.

        Без вызова модели заменяется только результат с исчезнувшим основанием: флаги
        и правки остальных выбранных блоков остаются, их никто не перепроверил.
        """
        return self.selected.refs if self.calls_model else self.evidence_blocks


def checkable_blocks(document: DocumentInput) -> list[BlockText]:
    """Блоки, которые harness проверяет: редактируемые, не заголовки, с текстом.

    Условие повторяет Block.checkable контракта harness: доля выбранных блоков считается
    от того же числа, что и счётчик проверенных блоков в его ответе.
    """
    blocks: list[BlockText] = []
    for block in document.blocks:
        if not block.editable or block.type is DocumentBlockType.HEADING:
            continue
        text = block.text_in(document.plain_text)
        if text.strip():
            blocks.append(BlockText(ref=block.ref, text=text))
    return blocks


def build_delta_request(
    *,
    baseline: Baseline,
    fingerprints: BaselineFingerprints,
    document: DocumentInput,
    payloads: Sequence[SourcePayload],
    segments: Sequence[Segments],
    suggestions: list[BaselineSuggestion],
    flags: list[BaselineFlag],
    policy: DeltaPolicy,
    limits: DeltaLimits,
) -> DeltaRequest | DeltaFallback:
    """Выбор блоков и сегментов; DeltaFallback, если выборочная проверка не подходит.

    Новый абзац, который не совпал ни с одним блоком, может требовать правки add в любом
    месте документа; такой вход проверяется полностью. Пустой sources означает, что
    источник только потерял текст и для выбранных блоков не нашлось опоры: вызывать
    модель не с чем, а правила переноса отметят правки, потерявшие основание.
    """
    base = fingerprints.all
    summary = summarize_delta(base, segments)
    current = frozenset(fp for source in segments for _, fp in source)
    changed = frozenset(
        row.id
        for row in suggestions
        if evidence_changed(row.evidence, fingerprints.by_snapshot, current)
    )
    evidence_blocks = {row.block_ref for row in suggestions if row.id in changed and row.block_ref}
    evidence_blocks.update(
        flag.block_ref
        for flag in flags
        if evidence_changed(flag.evidence, fingerprints.by_snapshot, current)
    )
    blocks = checkable_blocks(document)
    selected = select_blocks(
        blocks,
        (
            segment.text_in(payload.text)
            for payload, source in zip(payloads, segments, strict=True)
            for segment, fp in source
            if fp not in base
        ),
        evidence_changed=evidence_blocks,
        flagged={flag.block_ref for flag in flags},
        limits=limits,
    )
    fallback = delta_scope_check(
        changed_share=summary.changed_share,
        selected=len(selected.refs),
        checkable=len(blocks),
        policy=policy,
    )
    if fallback is not None:
        return fallback
    if not selected.refs and summary.segments_added:
        return DeltaFallback.NO_BLOCKS_MATCHED
    reduced = reduce_sources(
        [
            SourceInput(text=payload.text, segments=source)
            for payload, source in zip(payloads, segments, strict=True)
        ],
        base,
        (block.text for block in blocks if block.ref in selected.refs),
        limits,
    )
    sources = [
        replace(payload, text=part.text)
        for payload, part in zip(payloads, reduced, strict=True)
        if part.segment_indexes
    ]
    return DeltaRequest(
        baseline=baseline,
        base=base,
        selected=selected,
        sources=sources,
        stats=RunStats(
            segments_total=sum(len(source) for source in segments),
            segments_sent=sum(len(part.segment_indexes) for part in reduced),
            chars_total=sum(len(payload.text) for payload in payloads),
            chars_sent=sum(len(source.text) for source in sources),
            blocks_total=sum(1 for _ in document.blocks),
            blocks_checked=len(selected.refs) if sources else 0,
        ),
        summary=summary,
        suggestions=suggestions,
        flags=flags,
        changed=changed,
        evidence_blocks=frozenset(evidence_blocks),
    )


def add_on_unchanged(
    drafts: Sequence[SuggestionDraft],
    segments_by_position: Mapping[int, Segments],
    base: frozenset[int],
) -> list[bool]:
    """Для каждой правки: add, все основания которой найдены в неизменённых сегментах.

    Правка без найденного основания сюда не попадает: о ней ничего не известно, и
    отбросить её было бы потерей.
    """
    flags: list[bool] = []
    for draft in drafts:
        if draft.change_type is not ChangeKind.ADD or not draft.evidence:
            flags.append(False)
            continue
        unchanged = True
        for item in draft.evidence:
            source = (
                None
                if item.source_position is None
                else segments_by_position.get(item.source_position)
            )
            index = item.segment_index
            if source is None or index is None or not 0 <= index < len(source):
                unchanged = False
                break
            if source[index][1] not in base:
                unchanged = False
                break
        flags.append(unchanged)
    return flags
