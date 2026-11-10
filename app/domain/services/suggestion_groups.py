"""Группы правок одной фразы и догоняющие правки для её точных повторов.

group_key объединяет правки, которые меняют одну и ту же фразу одинаково: тип правки,
old_text и new_text после нормализации (normalize_text, casefold, схлопывание пробелов,
«ё» → «е»). Варианты с другими словами или падежом в группу не попадают: объединять их
по смыслу без признака от конвейера ненадёжно. Группа из одной правки ключа не получает —
фронтенду нечего показывать как «ещё N таких».

Группа — только подсказка для массового решения: каждая правка остаётся отдельной
и решается пользователем сама по себе.

Догоняющие правки (propagate_exact) создаются, если модель исправила фразу не во всех
местах: остальные дословные вхождения old_text в editable-блоках получают правку с тем же
текстом и origin = propagated. Добавления не размножаются — повтор якоря не означает,
что после него нужен тот же абзац. Вхождение, пересекающееся с любой уже существующей
правкой в блоке, пропускается.

Сложность: group_key — O(S·L); догоняющие — O(G·T) для G групп и текста T, поэтому
число групп и правок на группу ограничено. Если лимиты окажутся тесными, поиск всех фраз
за один проход даёт алгоритм Ахо — Корасик: O(T + Σ|фраз| + совпадений).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from app.domain.interfaces.document_analyzer import BlockPayload, ChangeKind
from app.domain.services.analysis_result_mapper import BlockContext, SuggestionDraft, index_blocks
from app.domain.text_normalization import normalize_text
from app.domain.value_objects import SuggestionOriginVO

# 16 hex-символов sha1 (64 бита): коллизия внутри одного анализа практически невозможна,
# а ключ короткий для индекса и URL.
GROUP_KEY_LENGTH = 16
_SEPARATOR = "\x00"
_PROPAGATED_NOTE = "Найдено автоматически: та же фраза, что в блоке {ref}"


@dataclass(frozen=True, slots=True)
class PropagationLimits:
    max_groups: int
    max_per_group: int

    def __post_init__(self) -> None:
        if self.max_groups < 1 or self.max_per_group < 1:
            raise ValueError("Лимиты догоняющих правок должны быть положительными")


def finalize_drafts(
    drafts: Sequence[SuggestionDraft],
    blocks: Iterable[BlockPayload],
    propagation: PropagationLimits | None,
) -> list[SuggestionDraft]:
    """Правки модели, догоняющие правки (если включены) и group_key для всех вместе.

    Ключи считаются после догоняющих: правка модели в одном месте становится группой,
    как только у неё появилась догоняющая пара.
    """
    combined = list(drafts)
    if propagation is not None:
        combined.extend(propagate_exact(drafts, blocks, propagation))
    return assign_group_keys(combined)


def normalize_phrase(text: str) -> str:
    """Форма фразы для сравнения: регистр, пробелы и «ё/е» не различаются."""
    return " ".join(normalize_text(text).casefold().replace("ё", "е").split())


def group_key(change_type: ChangeKind, old_text: str, new_text: str | None) -> str:
    raw = _SEPARATOR.join(
        (change_type.value, normalize_phrase(old_text), normalize_phrase(new_text or ""))
    )
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:GROUP_KEY_LENGTH]


def assign_group_keys(drafts: Sequence[SuggestionDraft]) -> list[SuggestionDraft]:
    """Проставить group_key правкам, у которых есть хотя бы одна пара. O(S·L)."""
    keys = [group_key(d.change_type, d.original_text, d.suggested_text) for d in drafts]
    sizes: dict[str, int] = {}
    for key in keys:
        sizes[key] = sizes.get(key, 0) + 1
    return [
        replace(draft, group_key=key) if sizes[key] > 1 else draft
        for draft, key in zip(drafts, keys, strict=True)
    ]


def propagate_exact(
    drafts: Sequence[SuggestionDraft],
    blocks: Iterable[BlockPayload],
    limits: PropagationLimits,
) -> list[SuggestionDraft]:
    """Догоняющие правки для точных повторов исправленных фраз; исходные не меняются."""
    editable = [context for context in index_blocks(blocks).values() if context.block.editable]
    occupied: dict[str, list[tuple[int, int]]] = {}
    for draft in drafts:
        occupied.setdefault(draft.block_ref, []).append((draft.start_offset, draft.end_offset))

    propagated: list[SuggestionDraft] = []
    position = len(drafts)
    for seed in _seeds(drafts, limits.max_groups):
        added = 0
        for context in editable:
            block = context.block
            spans = occupied.setdefault(block.ref, [])
            cursor = block.text.find(seed.original_text)
            while cursor >= 0 and added < limits.max_per_group:
                start = block.start_offset + cursor
                end = start + len(seed.original_text)
                if not _overlaps(spans, start, end):
                    spans.append((start, end))
                    propagated.append(_copy(seed, context, start, end, position))
                    position += 1
                    added += 1
                cursor = block.text.find(seed.original_text, cursor + len(seed.original_text))
            if added >= limits.max_per_group:
                break
    return propagated


def _seeds(drafts: Sequence[SuggestionDraft], max_groups: int) -> list[SuggestionDraft]:
    """Первая правка каждой группы modify/delete в порядке ответа, не больше max_groups."""
    seen: set[str] = set()
    seeds: list[SuggestionDraft] = []
    for draft in drafts:
        if draft.change_type is ChangeKind.ADD:
            continue
        key = group_key(draft.change_type, draft.original_text, draft.suggested_text)
        if key in seen:
            continue
        seen.add(key)
        seeds.append(draft)
        if len(seeds) == max_groups:
            break
    return seeds


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    # Правок в одном блоке единицы, поэтому линейный проход дешевле дерева интервалов.
    return any(start < span_end and span_start < end for span_start, span_end in spans)


def _copy(
    seed: SuggestionDraft, context: BlockContext, start: int, end: int, position: int
) -> SuggestionDraft:
    note = _PROPAGATED_NOTE.format(ref=seed.block_ref)
    rationale = f"{seed.rationale}\n\n{note}" if seed.rationale else note
    return replace(
        seed,
        block_ref=context.block.ref,
        section_ref=context.section_ref,
        rationale=rationale,
        position=position,
        start_offset=start,
        end_offset=end,
        group_key=None,
        origin=SuggestionOriginVO.PROPAGATED,
    )
