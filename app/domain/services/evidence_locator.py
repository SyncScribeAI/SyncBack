"""Поиск цитат модели в снимках источников.

Модель цитирует источник неточно: меняет кавычки и тире, теряет разметку списка,
сокращает фразу или пересказывает её. Поиск поэтому идёт в три шага от строгого к
мягкому. Сначала дословное вхождение, затем вхождение после свёртки, затем сегмент, где
нашлась наибольшая доля слов цитаты. Свёрнутый текст и индекс слов строятся лениво и один
раз на источник: у большинства цитат хватает первого шага, и тогда они не нужны вовсе.
"""

from __future__ import annotations

import re
from array import array
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace

from app.domain.evidence import Evidence, EvidenceCounters, EvidenceMatch, Quote
from app.domain.services.analysis_result_mapper import FlagDraft, SuggestionDraft
from app.domain.source_snapshot import SourceSegment, iter_segments
from app.domain.source_text import FragmentSpan
from app.domain.value_objects import SuggestionOriginVO

DEFAULT_MIN_CONTAINMENT = 0.6

# Маркеры в начале строки: заголовок, пункт списка, цитата. Модель их почти не цитирует.
_LEADING_MARKUP_RE = re.compile(r"^[ \t]*(?:(?:#{1,6}|[-*+>]|\d{1,9}[.)])[ \t]+)+", re.MULTILINE)
_WORD_RE = re.compile(r"\w+")
# Внутристрочная разметка Markdown исчезает при свёртке, а «|» таблицы становится пробелом.
_DROPPED = frozenset("*`")
_AS_SPACE = frozenset("|")
_FOLDED_CHARS = {
    "«": '"',
    "»": '"',
    "„": '"',
    "“": '"',
    "”": '"',
    "‟": '"',
    "'": '"',
    "‘": '"',
    "’": '"',
    "‚": '"',
    "–": "-",
    "—": "-",
    "‒": "-",
    "―": "-",
    "−": "-",
    "‑": "-",
    "…": "...",
    "ё": "е",
    "Ё": "е",
}
_ONE_TO_ONE = str.maketrans({key: value for key, value in _FOLDED_CHARS.items() if len(value) == 1})
_TOKEN_RE = re.compile(r"[^\s|]+")
_DROPPED_RE = re.compile(r"[*`]")
# Обрамляющие кавычки цитаты: в обосновании их ставит harness, в источнике их нет.
_QUOTE_MARKS = "«»\"“”„'"


def fold(text: str, positions: array[int] | None = None) -> str:
    """Свёртка для поиска: регистр, кавычки, тире, «ё», пробелы и разметка не различаются.

    positions, если передан, получает для каждого символа результата позицию исходного
    символа. По нему вхождение в свёрнутом тексте переводится в диапазон снимка. O(n).
    """
    lowered = text.translate(_ONE_TO_ONE).lower()
    if len(lowered) != len(text) or "…" in text:
        # Символ, который при свёртке меняет длину, ломает соответствие позиций по
        # срезам; такой текст сворачивается посимвольно.
        return _fold_by_char(text, positions)
    # Маркеры ищутся в исходном тексте: после свёртки тире «–» в начале строки выглядело
    # бы как маркер списка.
    parts: list[str] = []
    previous = 0
    for start, end in _skip_ranges(text):
        parts.append(lowered[previous:start])
        parts.append(" " * (end - start))
        previous = end
    parts.append(lowered[previous:])
    blanked = "".join(parts)
    pieces: list[str] = []
    for token in _TOKEN_RE.finditer(blanked):
        word = token.group()
        start = token.start()
        if _DROPPED_RE.search(word) is None:
            indexes: range | list[int] = range(start, token.end())
        else:
            indexes = [start + offset for offset, char in enumerate(word) if char not in _DROPPED]
            if not indexes:
                continue
            word = "".join(char for char in word if char not in _DROPPED)
        if positions is not None:
            # Пробел между словами указывает на первый символ следующего слова.
            if pieces:
                positions.append(indexes[0])
            positions.extend(indexes)
        pieces.append(word)
    return " ".join(pieces)


def _fold_by_char(text: str, positions: array[int] | None) -> str:
    skips = _skip_ranges(text)
    skip_start, skip_end = next(skips, (-1, -1))
    out: list[str] = []
    pending_space = False
    for index, char in enumerate(text):
        while skip_end != -1 and index >= skip_end:
            skip_start, skip_end = next(skips, (-1, -1))
        if skip_start <= index < skip_end:
            pending_space = bool(out)
            continue
        if char.isspace() or char in _AS_SPACE:
            pending_space = bool(out)
            continue
        if char in _DROPPED:
            continue
        mapped = _FOLDED_CHARS.get(char) or char.lower()
        if pending_space:
            out.append(" ")
            if positions is not None:
                positions.append(index)
            pending_space = False
        for piece in mapped:
            out.append(piece)
            if positions is not None:
                positions.append(index)
    return "".join(out)


def _skip_ranges(text: str) -> Iterator[tuple[int, int]]:
    for match in _LEADING_MARKUP_RE.finditer(text):
        if match.end() > match.start():
            yield match.start(), match.end()


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(fold(text)))


def _normalized_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _stem(name: str) -> str:
    head, dot, _ = name.rpartition(".")
    return head if dot and head else name


@dataclass(frozen=True, slots=True)
class LocatedSource:
    """Источник задачи, в котором ищутся цитаты: текст снимка и положение фрагментов."""

    position: int
    source_id: str
    name: str
    text: str
    spans: Sequence[FragmentSpan] = ()


@dataclass(frozen=True, slots=True)
class _Hit:
    match: EvidenceMatch
    start: int
    end: int
    score: float


class _SourceIndex:
    """Структуры поиска одного источника; каждая строится при первом обращении."""

    def __init__(self, source: LocatedSource) -> None:
        self.source = source
        self._folded: str | None = None
        self._positions: array[int] = array("I")
        self._segments: list[SourceSegment] | None = None
        self._segment_starts: list[int] = []
        self._postings: dict[str, list[int]] | None = None
        self._span_starts = [span.start for span in source.spans]

    def find(self, quote: str, min_containment: float) -> _Hit | None:
        text = self.source.text
        found = text.find(quote)
        if found >= 0:
            return _Hit(EvidenceMatch.EXACT, found, found + len(quote), 1.0)
        folded_quote = fold(quote).strip()
        if not folded_quote:
            return None
        folded = self._folded_text()
        found = folded.find(folded_quote)
        if found >= 0:
            last = found + len(folded_quote) - 1
            return _Hit(EvidenceMatch.EXACT, self._positions[found], self._positions[last] + 1, 1.0)
        return self._approximate(_words(quote), min_containment)

    def segment_index(self, offset: int) -> int | None:
        segments = self._segments_list()
        index = bisect_right(self._segment_starts, offset) - 1
        if index < 0 or not segments:
            return None
        return segments[index].index

    def locator_at(self, offset: int) -> FragmentSpan | None:
        index = bisect_right(self._span_starts, offset) - 1
        if index < 0:
            return None
        span = self.source.spans[index]
        return span if offset < span.end else None

    def _folded_text(self) -> str:
        if self._folded is None:
            self._folded = fold(self.source.text, self._positions)
        return self._folded

    def _segments_list(self) -> list[SourceSegment]:
        if self._segments is None:
            self._segments = list(iter_segments(self.source.text))
            self._segment_starts = [segment.start for segment in self._segments]
        return self._segments

    def _approximate(self, quote_words: set[str], min_containment: float) -> _Hit | None:
        """Сегмент с наибольшей долей слов цитаты. O(слов цитаты + затронутые списки)."""
        if not quote_words:
            return None
        postings = self._postings_index()
        hits: dict[int, int] = {}
        for word in quote_words:
            for position in postings.get(word, ()):
                hits[position] = hits.get(position, 0) + 1
        if not hits:
            return None
        # При равенстве берётся более ранний сегмент: так результат не зависит от порядка
        # обхода множества слов.
        best = min(hits, key=lambda position: (-hits[position], position))
        score = hits[best] / len(quote_words)
        if score < min_containment:
            return None
        segment = self._segments_list()[best]
        return _Hit(EvidenceMatch.APPROXIMATE, segment.start, segment.end, score)

    def _postings_index(self) -> dict[str, list[int]]:
        if self._postings is None:
            postings: dict[str, list[int]] = {}
            text = self.source.text
            for position, segment in enumerate(self._segments_list()):
                for word in _words(segment.text_in(text)):
                    postings.setdefault(word, []).append(position)
            self._postings = postings
        return self._postings


class EvidenceLocator:
    """Сопоставляет цитату с источником задачи и находит её место в снимке.

    Источник выбирается по source_id цитаты, затем по имени (без учёта регистра, затем без
    расширения). При одинаковых именах берётся тот, в чьём снимке цитата нашлась. Если имя
    не совпало ни с одним источником, цитата ищется во всех: harness берёт имя из своего
    чанка, и оно может отличаться от имени источника в SyncBack.
    """

    def __init__(
        self, sources: Sequence[LocatedSource], min_containment: float = DEFAULT_MIN_CONTAINMENT
    ) -> None:
        self._indexes = [_SourceIndex(source) for source in sources]
        self._min_containment = min_containment
        self._memo: dict[tuple[str | None, str, str], Evidence] = {}

    def locate(self, quote: Quote) -> Evidence:
        key = (quote.source_id, quote.source_name, quote.text)
        cached = self._memo.get(key)
        if cached is None:
            cached = self._memo[key] = self._locate(quote)
        return cached

    def _locate(self, quote: Quote) -> Evidence:
        text = quote.text.strip().strip(_QUOTE_MARKS).strip()
        named = self._candidates(quote)
        pool = named or self._indexes
        best: tuple[_SourceIndex, _Hit] | None = None
        if text:
            for index in pool:
                hit = index.find(text, self._min_containment)
                if hit is None:
                    continue
                if hit.match is EvidenceMatch.EXACT:
                    return self._evidence(quote, text, index, hit)
                if best is None or hit.score > best[1].score:
                    best = (index, hit)
        if best is not None:
            return self._evidence(quote, text, *best)
        # Источник назван однозначно, но цитаты в нём нет: позиция сохраняется, чтобы
        # просмотр открыл именно его.
        position = named[0].source.position if len(named) == 1 else None
        return Evidence(
            source_position=position,
            source_name=quote.source_name,
            quote=text or quote.text,
            match=EvidenceMatch.NOT_FOUND,
        )

    def _candidates(self, quote: Quote) -> list[_SourceIndex]:
        if quote.source_id:
            by_id = [i for i in self._indexes if i.source.source_id == quote.source_id]
            if by_id:
                return by_id
        name = _normalized_name(quote.source_name)
        if not name:
            return []
        by_name = [i for i in self._indexes if _normalized_name(i.source.name) == name]
        if by_name:
            return by_name
        stem = _stem(name)
        return [i for i in self._indexes if _stem(_normalized_name(i.source.name)) == stem]

    @staticmethod
    def _evidence(quote: Quote, text: str, index: _SourceIndex, hit: _Hit) -> Evidence:
        span = index.locator_at(hit.start)
        return Evidence(
            source_position=index.source.position,
            source_name=index.source.name,
            quote=text,
            match=hit.match,
            start=hit.start,
            end=hit.end,
            segment_index=index.segment_index(hit.start),
            locator=None if span is None else span.locator,
        )


def attach_evidence(
    drafts: Sequence[SuggestionDraft], flags: Sequence[FlagDraft], locator: EvidenceLocator
) -> tuple[list[SuggestionDraft], list[FlagDraft], EvidenceCounters]:
    """Основания правок и флагов. O(Q) поисков, повторы цитат берутся из кэша локатора.

    Счётчики считают цитаты правок модели и флагов: догоняющая правка повторяет цитаты
    исходной и исказила бы долю найденных. unparsed — правки модели с обоснованием, из
    которого не извлеклось ни одной цитаты.
    """
    counters = EvidenceCounters()
    located_drafts: list[SuggestionDraft] = []
    for draft in drafts:
        evidence = tuple(locator.locate(quote) for quote in draft.quotes)
        if draft.origin is SuggestionOriginVO.MODEL:
            for item in evidence:
                counters.count(item)
            if not draft.quotes and draft.rationale:
                counters.unparsed += 1
        located_drafts.append(replace(draft, evidence=evidence) if evidence else draft)
    located_flags: list[FlagDraft] = []
    for flag in flags:
        evidence = tuple(locator.locate(quote) for quote in flag.quotes)
        for item in evidence:
            counters.count(item)
        located_flags.append(replace(flag, evidence=evidence) if evidence else flag)
    return located_drafts, located_flags, counters
