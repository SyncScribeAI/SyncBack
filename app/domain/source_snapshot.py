"""Снимок источника: текст ровно в том виде, в каком он ушёл в ML-конвейер.

Просмотр источника и подсветка оснований читают снимок, а не пересобирают текст заново:
файл источника могут заменить, страница по ссылке может обновиться, а цитаты модели
относятся к тому тексту, который она видела. Differ сравнивает снимки по отпечаткам
сегментов, не скачивая сами тексты.

Сегмент совпадает с блоком единого формата источника (app.domain.source_text): абзац,
пункт списка, строка таблицы, заголовок или код в ограждении. Границы сегмента заданы
в кодпойнтах текста снимка.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from array import array
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

SNAPSHOT_PREFIX = "source_snapshots"
_FINGERPRINT_BYTES = 8
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+\S")
# Маркеры разметки в начале строки не меняют смысл сегмента: «- Лимит 50 МБ» и
# «Лимит 50 МБ» — один и тот же факт, перенесённый из абзаца в список.
_LEADING_MARKUP_RE = re.compile(r"^(?:\s*(?:#{1,6}|[-*+>]|\d{1,9}[.)])\s+)+", re.MULTILINE)
_TABLE_PIPES_RE = re.compile(r"\s*\|\s*")
_TYPOGRAPHY = str.maketrans(
    {
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
    }
)


class SegmentKind(StrEnum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    LIST_ITEM = "list_item"
    TABLE_ROW = "table_row"
    CODE = "code"


@dataclass(frozen=True, slots=True)
class SourceSegment:
    index: int
    start: int
    end: int
    kind: SegmentKind

    def text_in(self, text: str) -> str:
        return text[self.start : self.end]


def iter_segments(text: str) -> Iterator[SourceSegment]:
    """Сегменты текста снимка за один проход по строкам; O(n) времени, O(1) памяти сверх
    текущего сегмента.

    Пустая строка закрывает сегмент. Заголовок и строка таблицы всегда отдельные сегменты;
    пункт списка открывает новый сегмент, а строки его продолжения входят в него. Код в
    ограждении — один сегмент вместе с ограждением, как в разборе Markdown.
    """
    index = 0
    kind: SegmentKind | None = None
    start = end = 0
    fence: str | None = None
    position = 0
    length = len(text)
    while position <= length:
        newline = text.find("\n", position)
        line_end = length if newline == -1 else newline
        line = text[position:line_end]
        stripped = line.strip()
        content_start = position + (len(line) - len(line.lstrip()))
        content_end = position + len(line.rstrip())

        if fence is not None:
            if stripped:
                end = content_end
            if stripped.startswith(fence):
                fence = None
                yield SourceSegment(index, start, end, SegmentKind.CODE)
                index += 1
                kind = None
        elif not stripped:
            if kind is not None:
                yield SourceSegment(index, start, end, kind)
                index += 1
                kind = None
        else:
            fence_match = _FENCE_RE.match(line)
            line_kind = _line_kind(line, stripped)
            opens_new = (
                fence_match is not None
                or line_kind in (SegmentKind.HEADING, SegmentKind.TABLE_ROW, SegmentKind.LIST_ITEM)
                or kind in (SegmentKind.HEADING, SegmentKind.TABLE_ROW)
            )
            if kind is not None and opens_new:
                yield SourceSegment(index, start, end, kind)
                index += 1
                kind = None
            if fence_match is not None:
                fence = fence_match.group(1)
                kind = SegmentKind.CODE
                start, end = content_start, content_end
            elif kind is None:
                kind = line_kind
                start, end = content_start, content_end
            else:
                end = content_end

        if newline == -1:
            break
        position = newline + 1

    if kind is not None:
        yield SourceSegment(index, start, end, kind)


def _line_kind(line: str, stripped: str) -> SegmentKind:
    if _HEADING_RE.match(line):
        return SegmentKind.HEADING
    if len(stripped) > 1 and stripped.startswith("|") and stripped.endswith("|"):
        return SegmentKind.TABLE_ROW
    if _LIST_ITEM_RE.match(line):
        return SegmentKind.LIST_ITEM
    return SegmentKind.PARAGRAPH


def fold_for_fingerprint(segment_text: str) -> str:
    """Свёртка, к которой устойчив отпечаток: пробелы, типографика и маркеры разметки.

    Регистр, цифры и единицы измерения сохраняются: замена «50 МБ» на «100 МБ» — именно
    то изменение источника, которое продукт обязан заметить.
    """
    text = _LEADING_MARKUP_RE.sub("", segment_text)
    text = _TABLE_PIPES_RE.sub(" | ", text.translate(_TYPOGRAPHY))
    return " ".join(text.split()).strip(" |")


def fingerprint(segment_text: str) -> int:
    """64-битный отпечаток свёрнутого текста сегмента."""
    digest = hashlib.blake2b(
        fold_for_fingerprint(segment_text).encode("utf-8"), digest_size=_FINGERPRINT_BYTES
    ).digest()
    return int.from_bytes(digest, "big")


def segment_fingerprints(text: str) -> list[tuple[SourceSegment, int]]:
    return [(segment, fingerprint(segment.text_in(text))) for segment in iter_segments(text)]


def pack_fingerprints(fingerprints: Iterable[int]) -> bytes:
    """Упаковка по 8 байт на сегмент: differ читает её, не скачивая текст снимка."""
    packed = array("Q", fingerprints)
    if packed.itemsize != _FINGERPRINT_BYTES:
        raise RuntimeError("Платформа без 64-битного array('Q')")
    return packed.tobytes()


def unpack_fingerprints(raw: bytes) -> array[int]:
    packed = array("Q")
    packed.frombytes(raw)
    return packed


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_storage_key(project_id: uuid.UUID, text_hash: str) -> str:
    """Адрес от содержимого: одинаковый текст хранится один раз на проект."""
    return f"{SNAPSHOT_PREFIX}/{project_id}/{text_hash}.txt"


def fingerprints_storage_key(text_storage_key: str) -> str:
    return text_storage_key.removesuffix(".txt") + ".fp"


@dataclass(frozen=True, slots=True)
class SnapshotDraft:
    """Снимок, посчитанный воркером до записи: текст уже в памяти, хэш и отпечатки готовы."""

    project_id: uuid.UUID
    content_hash: str
    text: str
    fingerprints: tuple[int, ...]

    @classmethod
    def of(cls, project_id: uuid.UUID, text: str) -> SnapshotDraft:
        return cls.from_segments(project_id, text, segment_fingerprints(text))

    @classmethod
    def from_segments(
        cls, project_id: uuid.UUID, text: str, segments: Sequence[tuple[SourceSegment, int]]
    ) -> SnapshotDraft:
        """Снимок из уже посчитанных сегментов: differ использует их же, без второго прохода."""
        return cls(
            project_id=project_id,
            content_hash=content_hash(text),
            text=text,
            fingerprints=tuple(fp for _, fp in segments),
        )

    @property
    def storage_key(self) -> str:
        return snapshot_storage_key(self.project_id, self.content_hash)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def segments(self) -> int:
        return len(self.fingerprints)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    id: uuid.UUID
    project_id: uuid.UUID
    content_hash: str
    storage_key: str
    chars: int
    segments: int
    created_at: datetime | None = None


class JobSourceStatus(StrEnum):
    FETCHED = "fetched"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JobSourceRecord:
    """Какой источник и в каком виде участвовал в задаче анализа.

    content_hash указывает на снимок; у неудачной загрузки его нет. file_storage_key
    фиксирует, какие байты файла были разобраны: если файл источника потом заменят,
    подсветку в оригинале показывать нельзя, а в тексте снимка — можно.
    """

    position: int
    source_id: uuid.UUID | None
    name: str
    kind: str
    status: JobSourceStatus
    content_hash: str | None = None
    file_storage_key: str | None = None


@dataclass(frozen=True, slots=True)
class JobSourceSnapshot:
    """Строка задачи анализа вместе с её снимком; так её читают просмотр и differ."""

    position: int
    source_id: uuid.UUID | None
    name: str
    kind: str
    status: JobSourceStatus
    snapshot: SourceSnapshot | None
    file_storage_key: str | None


def distinct_drafts(drafts: Sequence[SnapshotDraft]) -> list[SnapshotDraft]:
    """Один снимок на одинаковый текст: два источника с одним содержимым — одна запись."""
    seen: dict[str, SnapshotDraft] = {}
    for draft in drafts:
        seen.setdefault(draft.content_hash, draft)
    return list(seen.values())
