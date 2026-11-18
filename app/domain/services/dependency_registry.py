"""Реестр зависимостей блоков документа от сегментов прошлого входа и отбор по нему.

Отбор по словам (app.domain.services.delta_selection) смотрит только на новый текст
источников: блок, который опирался на удалённый абзац, этим способом не находится, если
удалённого текста больше нет во входе. Реестр сопоставляет каждый проверяемый блок с
сегментами прошлого входа S0, на которые он, вероятно, опирается, и пересчитывается из
текстов прошлых снимков в момент выборочной задачи: хранить его отдельно не нужно.

Зависимость блока от сегмента S0 устанавливается тремя способами: лучшие по BM25
сегменты (support_segments штук), сегменты с общим сильным токеном (число, дата, версия,
идентификатор), если токен не встречается в большей части сегментов, и заголовки
разделов найденных сегментов. Связь лексическая: пересказ без общих слов реестр не
найдёт, поэтому блок без единой зависимости проверяется всегда.

Правила отбора заданы до прогона на данных и применяются все вместе:
R1 блок зависит от удалённого или изменённого сегмента S0;
R2 отбор по словам по текстам новых и удалённых сегментов;
R3 у блока есть зависимость в разделе, где изменился сегмент;
R4 у блока нет ни одной зависимости;
R5 новый сегмент не связан ни с одним блоком ни сильным токеном, ни разделом: место
правки неизвестно, и проверяются все блоки.

Сравнение входов идёт мультимножествами отпечатков: если в S0 абзац был дважды, а в S1
остался один раз, одна копия считается удалённой.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from app.domain.services.delta_selection import (
    BlockText,
    DeltaLimits,
    TermIndex,
    select_blocks,
    text_terms,
)
from app.domain.source_snapshot import SegmentKind, segment_fingerprints

# Сильный токен из большей доли сегментов не различает их: так ведёт себя номер раздела
# верхнего уровня или единица измерения с цифрой.
_COMMON_TOKEN_SHARE = 0.5


@dataclass(frozen=True, slots=True)
class SegmentText:
    """Сегмент одного входа: отпечаток, текст и отпечаток заголовка его раздела."""

    fingerprint: int
    text: str
    heading: bool
    # Отпечаток ближайшего заголовка выше в том же источнике; None до первого заголовка.
    section: int | None


def source_segments(text: str, *, by_line: bool = False) -> list[SegmentText]:
    """Сегменты текста источника с разделами; O(длина текста).

    by_line=True строит реестр по строкам и пунктам: зависимость блока указывает на
    конкретный пункт источника, а не на весь абзац без пустых строк.
    """
    result: list[SegmentText] = []
    section: int | None = None
    for segment, fingerprint in segment_fingerprints(text, by_line=by_line):
        heading = segment.kind is SegmentKind.HEADING
        if heading:
            section = fingerprint
        result.append(
            SegmentText(
                fingerprint=fingerprint,
                text=segment.text_in(text),
                heading=heading,
                section=section,
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class DependencyRegistry:
    """Зависимости блоков: отпечатки сегментов S0 и разделы, где они лежат."""

    segments: Mapping[str, frozenset[int]]
    sections: Mapping[str, frozenset[int]]

    def dependents_of_sections(self, sections: Collection[int]) -> set[str]:
        wanted = set(sections)
        return {ref for ref, own in self.sections.items() if own & wanted}


def build_registry(
    blocks: Sequence[BlockText], baseline: Sequence[Sequence[SegmentText]], limits: DeltaLimits
) -> DependencyRegistry:
    """O(слова блоков × длина списков индекса) на BM25 и O(сильные токены) на совпадения."""
    flat = [segment for source in baseline for segment in source if not segment.heading]
    index = TermIndex(text_terms(segment.text, limits.stem_chars) for segment in flat)
    strong_holders: dict[str, set[int]] = {}
    for position, segment in enumerate(flat):
        for token in text_terms(segment.text, limits.stem_chars).strong:
            strong_holders.setdefault(token, set()).add(position)
    common = len(flat) * _COMMON_TOKEN_SHARE

    segments: dict[str, frozenset[int]] = {}
    sections: dict[str, frozenset[int]] = {}
    for block in blocks:
        terms = text_terms(block.text, limits.stem_chars)
        chosen = set(index.best(terms, limits.support_segments, None)) if flat else set()
        for token in terms.strong:
            holders = strong_holders.get(token, ())
            if 0 < len(holders) <= common:
                chosen.update(holders)
        segments[block.ref] = frozenset(flat[position].fingerprint for position in chosen)
        sections[block.ref] = frozenset(
            section for position in chosen if (section := flat[position].section) is not None
        )
    return DependencyRegistry(segments=segments, sections=sections)


@dataclass(frozen=True, slots=True)
class RegistrySelection:
    """Блоки на повторную проверку и вклад каждого правила (для meta и протокола)."""

    refs: frozenset[str]
    by_rule: Mapping[str, int]
    # R5: место правки неизвестно, выбраны все проверяемые блоки.
    unknown_scope: bool

    def as_meta(self) -> dict[str, int | bool]:
        return {
            "blocks_selected": len(self.refs),
            **{f"selected_by_{rule}": count for rule, count in self.by_rule.items()},
            "unknown_scope": self.unknown_scope,
        }


def _changed(
    first: Sequence[Sequence[SegmentText]], second: Sequence[Sequence[SegmentText]]
) -> list[SegmentText]:
    """Сегменты first, копий которых в second меньше; порядок как в first."""
    remaining = Counter(segment.fingerprint for source in second for segment in source)
    changed: list[SegmentText] = []
    for source in first:
        for segment in source:
            if remaining[segment.fingerprint] > 0:
                remaining[segment.fingerprint] -= 1
            else:
                changed.append(segment)
    return changed


def select_by_registry(
    blocks: Sequence[BlockText],
    baseline: Sequence[Sequence[SegmentText]],
    current: Sequence[Sequence[SegmentText]],
    *,
    evidence_changed: Collection[str],
    flagged: Collection[str],
    limits: DeltaLimits,
) -> RegistrySelection:
    """Правила R1–R5 над реестром, построенным по baseline; blocks только проверяемые."""
    registry = build_registry(blocks, baseline, limits)
    removed = _changed(baseline, current)
    added = _changed(current, baseline)
    removed_fps = {segment.fingerprint for segment in removed}
    known_sections = {
        segment.section for source in baseline for segment in source if segment.section
    }

    rule_refs: dict[str, set[str]] = {}
    rule_refs["r1_removed_dependency"] = {
        ref for ref, own in registry.segments.items() if own & removed_fps
    }
    rule_refs["r2_overlap"] = set(
        select_blocks(
            blocks,
            [segment.text for segment in (*added, *removed) if not segment.heading],
            evidence_changed=evidence_changed,
            flagged=flagged,
            limits=limits,
        ).refs
    )
    changed_sections = {
        segment.section
        for segment in (*added, *removed)
        if segment.section is not None and segment.section in known_sections
    }
    rule_refs["r3_section"] = registry.dependents_of_sections(changed_sections)
    rule_refs["r4_no_dependency"] = {ref for ref, own in registry.segments.items() if not own}

    block_index = TermIndex(text_terms(block.text, limits.stem_chars) for block in blocks)
    covered_sections = {section for own in registry.sections.values() for section in own}
    unknown_scope = False
    for segment in added:
        if segment.heading:
            continue
        terms = text_terms(segment.text, limits.stem_chars)
        strong_match = any(
            block_index.holders(token) and not block_index.is_common(token)
            for token in terms.strong
        )
        if not strong_match and segment.section not in covered_sections:
            unknown_scope = True
            break

    every = {block.ref for block in blocks}
    selected: set[str] = set(every) if unknown_scope else set()
    by_rule: dict[str, int] = {}
    for rule, refs in rule_refs.items():
        fresh = refs - selected
        by_rule[rule] = len(fresh)
        selected |= fresh
    by_rule["r5_unknown_scope"] = len(every) if unknown_scope else 0
    return RegistrySelection(
        refs=frozenset(selected & every), by_rule=by_rule, unknown_scope=unknown_scope
    )


def document_context_refs(
    blocks: Sequence[tuple[str, bool]], selected: Collection[str], neighbours: int
) -> frozenset[str]:
    """Блоки документа для выборочного запроса: выбранные, соседи и все заголовки.

    blocks — пары (ref, заголовок ли) в порядке документа. Заголовки сохраняют разбиение
    на разделы, по которому harness определяет секцию блока; соседи дают модели связный
    фрагмент, если правка опирается на соседний абзац.
    """
    wanted = set(selected)
    keep: set[str] = set()
    for position, (ref, heading) in enumerate(blocks):
        if heading:
            keep.add(ref)
        if ref not in wanted:
            continue
        low = max(0, position - neighbours)
        high = min(len(blocks), position + neighbours + 1)
        keep.update(blocks[index][0] for index in range(low, high))
    return frozenset(keep)
