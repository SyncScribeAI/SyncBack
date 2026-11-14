"""Основание правки: цитата модели и её место в снимке источника.

Модель обосновывает правку цитатой, но не говорит, где цитата стоит в источнике. Место
находит локатор (app.domain.services.evidence_locator) по снимку, то есть по тому тексту,
который модель видела. Результат хранится вместе с правкой: просмотр источника
подсвечивает его без повторного поиска, а differ знает, на какие сегменты опирается правка.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.domain.source_snapshot import JobSourceRecord, JobSourceStatus


class EvidenceMatch(StrEnum):
    # Точный диапазон символов: цитата нашлась дословно или после свёртки.
    EXACT = "exact"
    # Цитата пересказана; подсвечивается сегмент, где больше всего её слов.
    APPROXIMATE = "approximate"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class Quote:
    """Цитата из ответа модели. source_id есть только в структурном поле контракта."""

    source_name: str
    text: str
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    """Цитата, сопоставленная с источником задачи.

    source_position указывает на строку задачи в analysis_job_source_snapshots; None
    значит, что источник цитаты не определён. start и end заданы в кодпойнтах текста
    снимка: у exact это сама цитата, у approximate весь сегмент.
    """

    source_position: int | None
    source_name: str
    quote: str
    match: EvidenceMatch
    start: int | None = None
    end: int | None = None
    segment_index: int | None = None
    locator: Mapping[str, Any] | None = None

    @property
    def found(self) -> bool:
        return self.match is not EvidenceMatch.NOT_FOUND


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """Что о строке задачи нужно, чтобы записать основание: id источника и снимка."""

    position: int
    source_id: uuid.UUID | None
    name: str
    snapshot_id: uuid.UUID | None
    file_storage_key: str | None


def evidence_sources_of(
    records: Iterable[JobSourceRecord], snapshot_ids: Mapping[str, uuid.UUID]
) -> dict[int, EvidenceSource]:
    """Строки задачи по номеру; у неудачной загрузки снимка нет, и основание на неё не ссылается."""
    return {
        record.position: EvidenceSource(
            position=record.position,
            source_id=record.source_id,
            name=record.name,
            snapshot_id=None
            if record.content_hash is None
            else snapshot_ids.get(record.content_hash),
            file_storage_key=record.file_storage_key,
        )
        for record in records
        if record.status is JobSourceStatus.FETCHED
    }


def evidence_to_json(
    items: Iterable[Evidence], sources: Mapping[int, EvidenceSource]
) -> list[dict[str, Any]]:
    """Основания в виде записи JSONB.

    id источника и снимка пишутся сразу: просмотр читает правку без обращения к строкам
    задачи, а file_storage_key нужен, чтобы понять, не заменили ли файл после анализа.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        source = None if item.source_position is None else sources.get(item.source_position)
        rows.append(
            {
                "source_position": item.source_position,
                "source_id": None
                if source is None or source.source_id is None
                else str(source.source_id),
                "source_name": item.source_name if source is None else source.name,
                "snapshot_id": None
                if source is None or source.snapshot_id is None
                else str(source.snapshot_id),
                "file_storage_key": None if source is None else source.file_storage_key,
                "quote": item.quote,
                "match": item.match.value,
                "start_offset": item.start,
                "end_offset": item.end,
                "segment_index": item.segment_index,
                "locator": None if item.locator is None else dict(item.locator),
            }
        )
    return rows


def present_evidence(
    rows: Sequence[Mapping[str, Any]] | None, current_storage_keys: Mapping[str, str | None]
) -> list[dict[str, Any]]:
    """Основания для ответа API: без ключа хранилища, с признаком доступности оригинала.

    Оригинал доступен, когда источник ещё существует и его файл тот же, что разбирался
    при анализе. Иначе показывать можно только текст снимка: подсветка в другом файле
    указала бы не туда.
    """
    if not rows:
        return []
    presented: list[dict[str, Any]] = []
    for row in rows:
        item = {key: value for key, value in row.items() if key != "file_storage_key"}
        source_id = row.get("source_id")
        stored_key = row.get("file_storage_key")
        item["original_file_available"] = bool(
            source_id is not None
            and stored_key is not None
            and source_id in current_storage_keys
            and current_storage_keys[source_id] == stored_key
        )
        presented.append(item)
    return presented


@dataclass(slots=True)
class EvidenceCounters:
    """Счётчики прогона. Рост unparsed означает, что harness поменял формат обоснования."""

    exact: int = 0
    approximate: int = 0
    not_found: int = 0
    unparsed: int = 0

    def count(self, item: Evidence) -> None:
        if item.match is EvidenceMatch.EXACT:
            self.exact += 1
        elif item.match is EvidenceMatch.APPROXIMATE:
            self.approximate += 1
        else:
            self.not_found += 1

    def as_meta(self) -> dict[str, int]:
        return {
            "evidence_exact": self.exact,
            "evidence_approximate": self.approximate,
            "evidence_not_found": self.not_found,
            "evidence_unparsed": self.unparsed,
        }
