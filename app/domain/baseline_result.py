"""Сводка результата прошлой задачи, по которой строится перенос в инкрементальную.

Репозиторий отдаёт только поля, нужные правилам переноса: блок, ключ правки, принято ли
решение и на какие сегменты снимков опиралось основание. Сами строки копируются в БД
без загрузки в память.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Сегмент снимка, на который опиралось основание: id снимка и номер сегмента."""

    snapshot_id: str
    segment_index: int


@dataclass(frozen=True, slots=True)
class BaselineSuggestion:
    id: uuid.UUID
    block_ref: str | None
    section_ref: str
    change_type: str
    original_text: str | None
    suggested_text: str | None
    decided: bool
    evidence: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True, slots=True)
class BaselineFlag:
    id: uuid.UUID
    block_ref: str
    reason: str
    decided: bool
    evidence: tuple[EvidenceRef, ...] = ()


def evidence_refs(rows: Iterable[Mapping[str, Any]] | None) -> tuple[EvidenceRef, ...]:
    """Места оснований из JSONB правки или цитат флага; без снимка или сегмента пропускаются."""
    refs: list[EvidenceRef] = []
    for row in rows or ():
        snapshot_id = row.get("snapshot_id")
        segment_index = row.get("segment_index")
        if isinstance(snapshot_id, str) and isinstance(segment_index, int):
            refs.append(EvidenceRef(snapshot_id=snapshot_id, segment_index=segment_index))
    return tuple(refs)
