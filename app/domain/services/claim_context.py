"""Поиск по предложениям с сохранением условий родительских разделов."""

import re
from collections import defaultdict
from dataclasses import replace

from app.domain.interfaces.document_analyzer import SourcePayload
from app.domain.services.primary_context import (
    ContextSelection,
    EvidenceUnit,
    PrimaryContextIndex,
)

_BOUNDARY = re.compile(r"(?<=[.!?;])\s+(?=[А-ЯЁA-Z«])")
_HEADING = re.compile(r"^(#{1,6})\s+\S")
_QUALIFIER = re.compile(
    r"\b(?:если|кроме|исключени\w*|услови\w*|только|определени\w*|для\s+режима)\b",
    re.I,
)


def split_claims(text: str) -> tuple[str, ...]:
    """Разбить текст на непустые предложения без модельного вызова."""
    return tuple(part.strip() for part in _BOUNDARY.split(text) if part.strip())


class ClaimContextIndex(PrimaryContextIndex):
    """Применить прежний поиск отдельно к каждому утверждению."""

    def full_context(self, reason: str) -> ContextSelection:
        """Вернуть исходные источники без сокращения."""
        return self._full(reason, ())

    def _parse(self) -> tuple[tuple[EvidenceUnit, ...], bool]:
        units, structured = super()._parse()
        sections: dict[tuple[int, int], str] = {}
        for number, source in enumerate(self.sources):
            headings: list[tuple[int, str]] = []
            offset = 0
            for line in source.text.splitlines():
                text = line.strip()
                if not text:
                    continue
                heading = _HEADING.match(text)
                if heading:
                    level = len(heading.group(1))
                    while headings and headings[-1][0] >= level:
                        headings.pop()
                    headings.append((level, text))
                else:
                    sections[number, offset] = "\n".join(value for _, value in headings)
                    offset += 1
        offsets: dict[int, int] = defaultdict(int)
        result = []
        for unit in units:
            result.append(replace(unit, section=sections[unit.source, offsets[unit.source]]))
            offsets[unit.source] += 1
        return tuple(result), structured

    def _link(self) -> None:
        super()._link()
        qualifiers: dict[tuple[int, str], list[int]] = defaultdict(list)
        for unit in self.units:
            if _QUALIFIER.search(unit.text):
                qualifiers[unit.source, unit.section].append(unit.index)
        for unit in self.units:
            path = unit.section.splitlines()
            # Условие старшей рубрики относится к дочерним до выхода из раздела.
            ancestors = ["", *("\n".join(path[:depth]) for depth in range(1, len(path)))]
            for section in ancestors:
                self.edges[unit.index].update(qualifiers.get((unit.source, section), ()))

    def _render(self, selected: set[int]) -> tuple[SourcePayload, ...]:
        rendered = []
        for number, source in enumerate(self.sources):
            parts: list[str] = []
            previous: list[str] = []
            for unit in self.units:
                if unit.source != number or unit.index not in selected:
                    continue
                path = unit.section.splitlines()
                shared = 0
                while shared < min(len(path), len(previous)) and path[shared] == previous[shared]:
                    shared += 1
                if path and shared == len(path) and len(previous) > len(path):
                    parts.append(path[-1])
                else:
                    parts.extend(path[shared:])
                parts.append(unit.text)
                previous = path
            if parts:
                rendered.append(replace(source, text="\n".join(parts)))
        return tuple(rendered)

    def select(self, query: str) -> ContextSelection:
        claims = split_claims(query)
        if not claims:
            return self._full("empty_claims", ())
        selections = tuple(super(ClaimContextIndex, self).select(claim) for claim in claims)
        return self.combine(selections)
