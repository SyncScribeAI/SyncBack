"""Строгая привязка фрагмента правки к месту внутри одного блока.

Поиск идёт только в тексте указанного блока. Кандидаты сужаются номером вхождения и
контекстом до и после фрагмента; если после этого остаётся не ровно один кандидат,
правка не привязывается. Отказ лучше выбора «первого подходящего»: неверно привязанная
правка меняет чужое место документа, а отказ виден пользователю и считается пропуском.

Номер вхождения относится ко всем вхождениям old_text в блоке, а не к отфильтрованным
по контексту: модель видит блок целиком и считает по нему. Если номер и контекст
указывают на разные места, это противоречие, и правка тоже не привязывается.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LocateRefusal(StrEnum):
    TEXT_MISSING = "old_text_missing"
    NOT_IN_BLOCK = "old_text_not_in_block"
    INVALID_OCCURRENCE = "invalid_occurrence"
    CONTEXT_MISMATCH = "context_mismatch"
    AMBIGUOUS = "ambiguous_old_text"
    TAKEN = "occurrence_taken"


@dataclass(frozen=True, slots=True)
class TextAnchor:
    exact: str
    prefix: str | None = None
    suffix: str | None = None
    occurrence: int | None = None


@dataclass(frozen=True, slots=True)
class Located:
    start: int

    def end(self, anchor: TextAnchor) -> int:
        return self.start + len(anchor.exact)


def occurrences(text: str, needle: str) -> list[int]:
    """Начала всех вхождений, включая перекрывающиеся; O(len(text)) на вхождение."""
    found: list[int] = []
    position = text.find(needle)
    while position >= 0:
        found.append(position)
        position = text.find(needle, position + 1)
    return found


def locate(
    text: str, anchor: TextAnchor, taken: frozenset[int] | set[int] = frozenset()
) -> Located | LocateRefusal:
    """Начало фрагмента в блоке или причина отказа.

    taken — начала, уже занятые другими правками того же фрагмента в этом блоке: две
    правки на одно место противоречат друг другу, и вторая не привязывается.
    """
    if not anchor.exact.strip():
        return LocateRefusal.TEXT_MISSING
    candidates = occurrences(text, anchor.exact)
    if not candidates:
        return LocateRefusal.NOT_IN_BLOCK
    if anchor.occurrence is not None:
        number = anchor.occurrence
        if isinstance(number, bool) or number < 1 or number > len(candidates):
            return LocateRefusal.INVALID_OCCURRENCE
        candidates = [candidates[number - 1]]
    if anchor.prefix or anchor.suffix:
        candidates = [start for start in candidates if _context_matches(text, anchor, start)]
        if not candidates:
            return LocateRefusal.CONTEXT_MISMATCH
    if len(candidates) != 1:
        return LocateRefusal.AMBIGUOUS
    if candidates[0] in taken:
        return LocateRefusal.TAKEN
    return Located(candidates[0])


def _context_matches(text: str, anchor: TextAnchor, start: int) -> bool:
    # Контекст сверяется только внутри блока: контекст из соседнего блока не отличает
    # одно вхождение от другого и сделал бы проверку мягче, чем она выглядит.
    if anchor.prefix and not text.endswith(anchor.prefix, 0, start):
        return False
    return not anchor.suffix or text.startswith(anchor.suffix, start + len(anchor.exact))
