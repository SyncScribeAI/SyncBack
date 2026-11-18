"""Первичный контекст с замыканием по явно распознаваемым основаниям.

Эвристика не доказывает полноту смысловых зависимостей. Неизвестная структура,
неразрешённая ссылка и слишком большое замыкание возвращают полный контекст.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, replace

from app.domain.interfaces.document_analyzer import SourcePayload
from app.domain.services.delta_selection import TermIndex, text_terms

_CLAUSE = re.compile(r"^(\d+(?:\.\d+)+)\.?\s+")
_REFERENCE = re.compile(r"(?:пункт[а-я]*|пп?\.?)\s+(\d+(?:\.\d+)+)", re.I)
_QUALIFIER = re.compile(
    r"\b(?:если|кроме|исключени\w*|услови\w*|только|"
    r"однако|определени\w*|под\s+\S+\s+понимается|для\s+режима)\b",
    re.I,
)
_QUOTED = re.compile(r"«([^»]+)»|\"([^\"]+)\"")
_HEADING = re.compile(r"^#{1,6}\s+\S")


@dataclass(frozen=True, slots=True)
class PrimaryContextPolicy:
    top_k: int = 2
    stem_chars: int = 6
    max_units: int = 40
    max_share: float = 0.8
    preserve_dependencies: bool = True

    def __post_init__(self) -> None:
        if self.top_k < 1 or self.stem_chars < 1 or self.max_units < 1:
            raise ValueError("Параметры размера должны быть положительными")
        if not 0 < self.max_share <= 1:
            raise ValueError("max_share должен быть в интервале (0, 1]")


@dataclass(frozen=True, slots=True)
class EvidenceUnit:
    source: int
    index: int
    section: str
    text: str
    clause: str | None
    references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextSelection:
    sources: tuple[SourcePayload, ...]
    seed_units: tuple[int, ...]
    selected_units: tuple[int, ...]
    reasons: tuple[tuple[int, str], ...]
    fallback: str | None
    total_units: int


class PrimaryContextIndex:
    """Индекс строится один раз для всех блоков первичного запроса."""

    def __init__(self, sources: Sequence[SourcePayload], policy: PrimaryContextPolicy) -> None:
        self.sources = tuple(sources)
        self.policy = policy
        self.units, self.structured = self._parse()
        self.terms = TermIndex(
            text_terms(f"{unit.section}\n{unit.text}", policy.stem_chars) for unit in self.units
        )
        self.edges: dict[int, set[int]] = defaultdict(set)
        self.unresolved: set[int] = set()
        self._link()

    def _parse(self) -> tuple[tuple[EvidenceUnit, ...], bool]:
        units: list[EvidenceUnit] = []
        structured = True
        for number, source in enumerate(self.sources):
            section = ""
            source_has_structure = False
            for line in source.text.splitlines():
                text = line.strip()
                if not text:
                    continue
                if _HEADING.match(text):
                    section = text
                    source_has_structure = True
                    continue
                clause = _CLAUSE.match(text)
                source_has_structure |= clause is not None
                units.append(
                    EvidenceUnit(
                        source=number,
                        index=len(units),
                        section=section,
                        text=text,
                        clause=clause.group(1) if clause else None,
                        references=tuple(_REFERENCE.findall(text)),
                    )
                )
            if source.text.strip() and not source_has_structure:
                structured = False
        return tuple(units), structured

    def _link(self) -> None:
        clauses: dict[tuple[int, str], list[int]] = defaultdict(list)
        sections: dict[tuple[int, str], list[int]] = defaultdict(list)
        definitions: dict[frozenset[str], list[int]] = defaultdict(list)
        for unit in self.units:
            if unit.clause:
                clauses[unit.source, unit.clause].append(unit.index)
            sections[unit.source, unit.section].append(unit.index)
            if "определени" in unit.text.lower() or "понимается" in unit.text.lower():
                for match in _QUOTED.finditer(unit.text):
                    term = match.group(1) or match.group(2)
                    words = frozenset(text_terms(term, self.policy.stem_chars).counts)
                    if words:
                        definitions[words].append(unit.index)
        qualifiers: dict[tuple[int, str], list[int]] = {
            key: [i for i in indexes if _QUALIFIER.search(self.units[i].text)]
            for key, indexes in sections.items()
        }
        for unit in self.units:
            for reference in unit.references:
                targets = clauses.get((unit.source, reference), ())
                if len(targets) != 1:
                    self.unresolved.add(unit.index)
                    continue
                target = targets[0]
                self.edges[unit.index].add(target)
                self.edges[target].add(unit.index)
            # Без рубрики связь по соседству не считается надёжной.
            if unit.section:
                related = qualifiers[unit.source, unit.section]
                if related:
                    self.edges[unit.index].add(related[0])
                    if unit.index == related[0]:
                        self.edges[unit.index].update(related)
            unit_words = text_terms(unit.text, self.policy.stem_chars).counts.keys()
            for term, targets in definitions.items():
                if term <= unit_words:
                    self.edges[unit.index].update(targets)
        # Строка таблицы без шапки меняет смысл; сохраняем весь связный фрагмент.
        table: list[int] = []
        for unit in self.units:
            continues = (
                table
                and self.units[table[-1]].source == unit.source
                and self.units[table[-1]].section == unit.section
            )
            if not unit.text.startswith("|") or not continues:
                self._link_table(table)
                table = []
            if unit.text.startswith("|"):
                table.append(unit.index)
        self._link_table(table)

    def _link_table(self, indexes: list[int]) -> None:
        if indexes:
            self.edges[indexes[0]].update(indexes)
            for index in indexes[1:]:
                self.edges[index].add(indexes[0])

    def select(self, query: str) -> ContextSelection:
        if not self.units:
            return self._full("empty_index", ())
        if self.policy.preserve_dependencies and not self.structured:
            return self._full("unknown_structure", ())
        seeds = tuple(
            self.terms.best(text_terms(query, self.policy.stem_chars), self.policy.top_k, None)
        )
        if not seeds:
            return self._full("no_lexical_support", ())
        selected = set(seeds)
        reasons = dict.fromkeys(seeds, "retrieval")
        if self.policy.preserve_dependencies:
            pending = deque(seeds)
            while pending:
                index = pending.popleft()
                if index in self.unresolved:
                    return self._full("unresolved_reference", seeds)
                for target in sorted(self.edges[index]):
                    if target in selected:
                        continue
                    selected.add(target)
                    reasons[target] = "dependency"
                    pending.append(target)
                if len(selected) > self.policy.max_units:
                    return self._full("closure_limit", seeds)
        sources = self._render(selected)
        original_chars = sum(len(source.text) for source in self.sources)
        chosen_chars = sum(len(source.text) for source in sources)
        if original_chars and chosen_chars / original_chars > self.policy.max_share:
            return self._full("low_expected_saving", seeds)
        return ContextSelection(
            sources=sources,
            seed_units=seeds,
            selected_units=tuple(sorted(selected)),
            reasons=tuple(sorted(reasons.items())),
            fallback=None,
            total_units=len(self.units),
        )

    def _render(self, selected: set[int]) -> tuple[SourcePayload, ...]:
        rendered: list[SourcePayload] = []
        for number, source in enumerate(self.sources):
            parts: list[str] = []
            previous_section: str | None = None
            for unit in self.units:
                if unit.source != number or unit.index not in selected:
                    continue
                if unit.section and unit.section != previous_section:
                    parts.append(unit.section)
                parts.append(unit.text)
                previous_section = unit.section
            if parts:
                rendered.append(replace(source, text="\n".join(parts)))
        return tuple(rendered)

    def combine(self, selections: Sequence[ContextSelection]) -> ContextSelection:
        """Объединить контекст блоков без повторения одного и того же основания."""
        seeds = tuple(sorted({i for selection in selections for i in selection.seed_units}))
        if any(selection.fallback for selection in selections):
            return self._full("member_full_fallback", seeds)
        selected = {i for selection in selections for i in selection.selected_units}
        reasons = {i: reason for selection in selections for i, reason in selection.reasons}
        sources = self._render(selected)
        total_chars = sum(len(source.text) for source in self.sources)
        selected_chars = sum(len(source.text) for source in sources)
        if len(selected) > self.policy.max_units or (
            total_chars and selected_chars / total_chars > self.policy.max_share
        ):
            return self._full("combined_context_limit", seeds)
        return ContextSelection(
            sources=sources,
            seed_units=seeds,
            selected_units=tuple(sorted(selected)),
            reasons=tuple(sorted(reasons.items())),
            fallback=None,
            total_units=len(self.units),
        )

    def _full(self, reason: str, seeds: tuple[int, ...]) -> ContextSelection:
        indexes = tuple(range(len(self.units)))
        return ContextSelection(
            sources=self.sources,
            seed_units=seeds,
            selected_units=indexes,
            reasons=tuple((index, reason) for index in indexes),
            fallback=reason,
            total_units=len(self.units),
        )
