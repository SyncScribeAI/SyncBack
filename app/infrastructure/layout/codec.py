"""Сериализация DocumentLayout для кэша в объектном хранилище.

gzip JSON без pickle: кэш читается при каждом открытии документа, и разбор объекта
из хранилища не должен исполнять код. Колонки TextRuns пишутся байтами массивов в
base64 вместе с порядком байт, чтобы кэш переносился между машинами.
"""

import base64
import gzip
import json
import sys
from array import array
from dataclasses import dataclass
from typing import Any

from app.domain.document_geometry import DocumentLayout, PageSize, TextRuns
from app.domain.interfaces.document_parser import (
    DocumentBlockType,
    DocumentSection,
    ParsedBlock,
    ParsedDocument,
)

_FORMAT = 1
_RUN_COLUMNS = ("starts", "pages", "char_indexes", "counts")


class LayoutCacheMismatch(Exception):
    """Кэш собран другой версией парсера, из другого файла или повреждён."""


@dataclass(frozen=True, slots=True)
class LayoutStamp:
    """Чем собран кэш: версия эвристик разбора и sha256 файла документа."""

    version: str
    sha256: str


def encode_layout(layout: DocumentLayout, stamp: LayoutStamp) -> bytes:
    doc = layout.document
    payload: dict[str, Any] = {
        "format": _FORMAT,
        "version": stamp.version,
        "sha256": stamp.sha256,
        "text": doc.plain_text,
        "sections": [[s.ref, s.start_offset, s.end_offset] for s in doc.sections],
        "blocks": [
            [
                b.ref,
                b.type.value,
                b.position,
                b.start_offset,
                b.end_offset,
                b.editable,
                b.heading_level,
            ]
            for b in doc.blocks
        ],
        "pages": [[p.number, p.width, p.height] for p in layout.pages],
        "runs": _encode_runs(layout.runs),
    }
    return gzip.compress(json.dumps(payload, ensure_ascii=False).encode(), compresslevel=6)


def decode_layout(blob: bytes, expected: LayoutStamp) -> DocumentLayout:
    try:
        payload = json.loads(gzip.decompress(blob))
    except (OSError, EOFError, ValueError) as exc:
        raise LayoutCacheMismatch("кэш разбора повреждён") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != _FORMAT
        or payload.get("version") != expected.version
        or payload.get("sha256") != expected.sha256
    ):
        raise LayoutCacheMismatch("кэш собран другой версией парсера или из другого файла")
    try:
        document = ParsedDocument(
            plain_text=payload["text"],
            sections=[DocumentSection(r, s, e) for r, s, e in payload["sections"]],
            blocks=[
                ParsedBlock(r, DocumentBlockType(t), pos, s, e, ed, lvl)
                for r, t, pos, s, e, ed, lvl in payload["blocks"]
            ],
        )
        pages = tuple(PageSize(n, w, h) for n, w, h in payload["pages"])
        runs = _decode_runs(payload["runs"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LayoutCacheMismatch("кэш разбора повреждён") from exc
    return DocumentLayout(document, runs, pages)


def _encode_runs(runs: TextRuns | None) -> dict[str, Any] | None:
    if runs is None:
        return None
    columns = {
        name: base64.b64encode(getattr(runs, name).tobytes()).decode() for name in _RUN_COLUMNS
    }
    return {"byteorder": sys.byteorder, "itemsize": array("i").itemsize, **columns}


def _decode_runs(data: dict[str, Any] | None) -> TextRuns | None:
    if data is None:
        return None
    if data["itemsize"] != array("i").itemsize:
        raise ValueError("размер int в кэше отличается от текущей платформы")
    swap = data["byteorder"] != sys.byteorder
    columns: dict[str, array[int]] = {}
    for name in _RUN_COLUMNS:
        column = array("i")
        column.frombytes(base64.b64decode(data[name]))
        if swap:
            column.byteswap()
        columns[name] = column
    if len({len(c) for c in columns.values()}) != 1:
        raise ValueError("колонки отрезков разной длины")
    return TextRuns(**columns)
