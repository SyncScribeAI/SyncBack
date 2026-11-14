"""Сравнение текущих сегментов источников с отпечатками прошлого анализа.

Differ работает только с отпечатками (app.domain.source_snapshot): прошлый текст для
сравнения не нужен, достаточно .fp-файла снимка. Сегмент считается неизменным, если
его отпечаток встречался в прошлом входе в любом источнике: перенос абзаца между
источниками не меняет факты, которые видела модель.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.source_snapshot import SourceSegment


@dataclass(frozen=True, slots=True)
class SourceDelta:
    unchanged: tuple[SourceSegment, ...]
    added: tuple[SourceSegment, ...]
    # Отпечатки прошлого входа, которых нет в текущем: удалённые или изменённые сегменты.
    removed_fingerprints: frozenset[int]
    # Доля символов текущего входа, пришедшая в новых сегментах; 0 для пустого входа.
    changed_share: float

    @property
    def segments_total(self) -> int:
        return len(self.unchanged) + len(self.added)


def diff_fingerprints(
    base: frozenset[int], current: Iterable[tuple[SourceSegment, int]]
) -> SourceDelta:
    """Один проход по текущим сегментам: O(n) времени, O(число сегментов) памяти.

    Порядок сегментов не учитывается намеренно: перестановка абзацев не меняет фактов
    источника, а повторная проверка таких блоков стоила бы вызова модели.
    """
    unchanged: list[SourceSegment] = []
    added: list[SourceSegment] = []
    seen: set[int] = set()
    chars_total = chars_added = 0
    for segment, fingerprint in current:
        seen.add(fingerprint)
        length = segment.end - segment.start
        chars_total += length
        if fingerprint in base:
            unchanged.append(segment)
        else:
            added.append(segment)
            chars_added += length
    return SourceDelta(
        unchanged=tuple(unchanged),
        added=tuple(added),
        removed_fingerprints=base - seen,
        changed_share=chars_added / chars_total if chars_total else 0.0,
    )
