"""Выбор блоков документа и сегментов источников для инкрементального анализа.

Изменённые сегменты источника сопоставляются с блоками документа по словам: блок, у
которого с сегментом общие слова, числа или редкие термины, может зависеть от
изменения и проверяется заново. Остальные блоки уходят в запрос как контекст
(editable=false), а их прошлый результат переносится.

Индекс слов строится один раз на запрос и живёт только пока запрос собирается. Работа
пропорциональна числу слов документа и источников плюс длине списков индекса для слов
изменённых сегментов; слова, которые есть в большей части блоков, пропускаются: они не
различают блоки и сделали бы проход квадратичным.

Пересказ без общих слов этим способом не находится: такой блок останется со старым
результатом до следующего полного анализа. Это известный разрыв режима, его закрывают
лимиты цепочки и выборочный аудит полным прогоном.
"""

from __future__ import annotations

import heapq
import math
import re
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from app.domain.source_snapshot import SegmentKind, SourceSegment

# Слово, число, дата, версия или идентификатор: точки, дефисы, слэши и двоеточия внутри
# токена его не разрывают, иначе «1.2.3» и «01.02.2026» распались бы на общие цифры.
_TOKEN_RE = re.compile(r"\w+(?:[.\-/:]\w+)*")
_PART_RE = re.compile(r"[.\-/:]")
# Стандартные параметры BM25: насыщение частоты слова и поправка на длину блока.
_K1 = 1.2
_B = 0.75
# Слово из более чем половины блоков почти не различает их; на коротком документе
# пропускать нечего, поэтому отсечка включается с этого числа блоков.
_COMMON_SHARE = 0.5
_COMMON_MIN_DOCS = 20
_MIN_WORD = 2


@dataclass(frozen=True, slots=True)
class DeltaLimits:
    """Параметры выбора; значения по умолчанию приходят из настроек ANALYSIS_DELTA_*."""

    blocks_per_segment: int = 8
    score_threshold: float = 0.3
    rare_block_count: int = 2
    context_segments: int = 2
    support_segments: int = 3
    stem_chars: int = 6


@dataclass(frozen=True, slots=True)
class Terms:
    """Слова текста с частотами; strong хранит токены с цифрами: номера, даты, версии."""

    counts: Counter[str]
    strong: frozenset[str]


def text_terms(text: str, stem_chars: int) -> Terms:
    """Токены за один проход. Слова обрезаются до stem_chars символов.

    Обрезка заменяет морфологию: «сервера» и «серверы» дают одну основу, а словарь
    стеммера в закрытом контуре не нужен. Токен с цифрой хранится целиком: «5» и «50»
    должны различаться.
    """
    counts: Counter[str] = Counter()
    strong: set[str] = set()
    for match in _TOKEN_RE.finditer(text.lower()):
        token = match.group()
        if any(char.isdigit() for char in token):
            counts[token] += 1
            strong.add(token)
            continue
        for part in _PART_RE.split(token):
            if len(part) >= _MIN_WORD:
                counts[part[:stem_chars]] += 1
    return Terms(counts=counts, strong=frozenset(strong))


class TermIndex:
    """Обратный индекс слов по текстам с оценкой BM25, нормированной в [0, 1)."""

    __slots__ = ("_avg_length", "_lengths", "_postings")

    def __init__(self, documents: Iterable[Terms]) -> None:
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._lengths: list[int] = []
        for position, terms in enumerate(documents):
            self._lengths.append(sum(terms.counts.values()))
            for term, count in terms.counts.items():
                self._postings.setdefault(term, []).append((position, count))
        self._avg_length = sum(self._lengths) / len(self._lengths) if self._lengths else 0.0

    def __len__(self) -> int:
        return len(self._lengths)

    def holders(self, term: str) -> Sequence[tuple[int, int]]:
        return self._postings.get(term, ())

    def is_common(self, term: str) -> bool:
        total = len(self._lengths)
        return total >= _COMMON_MIN_DOCS and len(self.holders(term)) > total * _COMMON_SHARE

    def scores(self, query: Terms) -> dict[int, float]:
        """Оценка каждого текста, где есть хоть одно слово запроса.

        Нормировка на максимум, который запрос мог бы набрать, делает порог независимым от
        длины сегмента: короткий изменённый абзац и длинный оцениваются одной шкалой.
        """
        total = len(self._lengths)
        scores: dict[int, float] = {}
        ceiling = 0.0
        for term in query.counts:
            holders = self.holders(term)
            if not holders or self.is_common(term):
                continue
            frequency = len(holders)
            idf = math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            ceiling += idf * (_K1 + 1)
            for position, count in holders:
                norm = _K1 * (1 - _B + _B * self._lengths[position] / self._avg_length)
                scores[position] = scores.get(position, 0.0) + idf * count * (_K1 + 1) / (
                    count + norm
                )
        if ceiling:
            for position in scores:
                scores[position] /= ceiling
        return scores

    def best(self, query: Terms, count: int, threshold: float | None) -> list[int]:
        """count лучших текстов и все с оценкой не ниже threshold."""
        scores = self.scores(query)
        chosen = {-negated for _, negated in heapq.nlargest(count, _ranked(scores))}
        if threshold is not None:
            chosen.update(position for position, score in scores.items() if score >= threshold)
        return sorted(chosen)


def _ranked(scores: dict[int, float]) -> Iterable[tuple[float, int]]:
    # При равной оценке выигрывает более ранний текст: выбор воспроизводим.
    return ((score, -position) for position, score in scores.items())


@dataclass(frozen=True, slots=True)
class BlockText:
    ref: str
    text: str


@dataclass(frozen=True, slots=True)
class SelectedBlocks:
    """Блоки на повторную проверку и сколько дала каждая группа отбора (для meta)."""

    refs: frozenset[str]
    by_overlap: int
    by_token: int
    by_evidence: int
    by_flag: int

    def as_meta(self) -> dict[str, int]:
        return {
            "blocks_selected": len(self.refs),
            "selected_by_overlap": self.by_overlap,
            "selected_by_token": self.by_token,
            "selected_by_evidence": self.by_evidence,
            "selected_by_flag": self.by_flag,
        }


def select_blocks(
    blocks: Sequence[BlockText],
    changed_texts: Iterable[str],
    *,
    evidence_changed: Collection[str],
    flagged: Collection[str],
    limits: DeltaLimits,
) -> SelectedBlocks:
    """Четыре группы отбора; blocks содержит только проверяемые блоки документа.

    Перекрытие слов: лучшие blocks_per_segment блоков на каждый новый сегмент и все
    выше порога оценки. Общий токен: число, дата, версия или идентификатор из сегмента,
    либо редкое слово (не больше чем в rare_block_count блоках). Основание: у правки
    или флага прошлой задачи цитата из удалённого или изменённого сегмента. Флаг: блок,
    по которому прошлая задача не смогла решить сама.
    """
    index = TermIndex(text_terms(block.text, limits.stem_chars) for block in blocks)
    by_overlap: set[int] = set()
    by_token: set[int] = set()
    for text in changed_texts:
        terms = text_terms(text, limits.stem_chars)
        by_overlap.update(index.best(terms, limits.blocks_per_segment, limits.score_threshold))
        for term in terms.counts:
            holders = index.holders(term)
            strong = term in terms.strong and not index.is_common(term)
            if strong or 0 < len(holders) <= limits.rare_block_count:
                by_token.update(position for position, _ in holders)
    known = {block.ref for block in blocks}
    overlap_refs = {blocks[position].ref for position in by_overlap}
    token_refs = {blocks[position].ref for position in by_token} - overlap_refs
    evidence_refs = (set(evidence_changed) & known) - overlap_refs - token_refs
    flag_refs = (set(flagged) & known) - overlap_refs - token_refs - evidence_refs
    return SelectedBlocks(
        refs=frozenset(overlap_refs | token_refs | evidence_refs | flag_refs),
        by_overlap=len(overlap_refs),
        by_token=len(token_refs),
        by_evidence=len(evidence_refs),
        by_flag=len(flag_refs),
    )


@dataclass(frozen=True, slots=True)
class SourceInput:
    """Текущий источник: текст снимка и его сегменты с отпечатками."""

    text: str
    segments: Sequence[tuple[SourceSegment, int]]


@dataclass(frozen=True, slots=True)
class ReducedSource:
    """Что из источника уйдёт в запрос: номера сегментов и собранный из них текст."""

    segment_indexes: tuple[int, ...]
    text: str


def reduce_sources(
    sources: Sequence[SourceInput],
    base: Collection[int],
    selected_texts: Iterable[str],
    limits: DeltaLimits,
) -> list[ReducedSource]:
    """Новые и изменённые сегменты с соседями, опорные неизменённые и их заголовки.

    Соседи нужны модели, чтобы понять, к чему относится изменённый абзац. Опорные
    сегменты для каждого выбранного блока ищутся среди неизменённых во всех источниках:
    без них модель не увидит, что блок подтверждён остальным текстом, и предложит лишнюю
    правку. Заголовок раздела каждого отправленного сегмента сохраняет разбиение на
    разделы, по которому harness определяет секцию фрагмента.
    """
    chosen: list[set[int]] = [set() for _ in sources]
    unchanged: list[tuple[int, int]] = []
    unchanged_terms: list[Terms] = []
    headings: list[list[int | None]] = []
    for number, source in enumerate(sources):
        heading: int | None = None
        source_headings: list[int | None] = []
        total = len(source.segments)
        for position, (segment, fingerprint) in enumerate(source.segments):
            if segment.kind is SegmentKind.HEADING:
                heading = position
            source_headings.append(heading)
            if fingerprint in base:
                unchanged.append((number, position))
                unchanged_terms.append(text_terms(segment.text_in(source.text), limits.stem_chars))
                continue
            low = max(0, position - limits.context_segments)
            high = min(total, position + limits.context_segments + 1)
            chosen[number].update(range(low, high))
        headings.append(source_headings)

    if unchanged and limits.support_segments:
        index = TermIndex(unchanged_terms)
        del unchanged_terms
        for text in selected_texts:
            terms = text_terms(text, limits.stem_chars)
            for position in index.best(terms, limits.support_segments, None):
                number, segment_position = unchanged[position]
                chosen[number].add(segment_position)

    reduced: list[ReducedSource] = []
    for number, source in enumerate(sources):
        positions = set(chosen[number])
        for position in chosen[number]:
            heading_position = headings[number][position]
            if heading_position is not None:
                positions.add(heading_position)
        ordered = tuple(sorted(positions))
        reduced.append(
            ReducedSource(
                segment_indexes=ordered,
                text="\n\n".join(
                    source.segments[position][0].text_in(source.text) for position in ordered
                ),
            )
        )
    return reduced
