"""Кэш разбора PDF: кодек и чтение через хранилище."""

from __future__ import annotations

import base64
import gzip
import json
from array import array
from pathlib import Path

import pytest

from app.domain.document_geometry import DocumentLayout, highlight_span, rects_to_json
from app.domain.document_geometry import PageRect
from app.domain.storage_keys import derived_document_keys, document_layout_key
from app.infrastructure.layout.cached_reader import CachedDocumentLayoutReader
from app.infrastructure.layout.codec import (
    LayoutCacheMismatch,
    LayoutStamp,
    decode_layout,
    encode_layout,
)
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from tests.contract.conftest import InMemoryFileStorage

FIXTURES = Path(__file__).parents[1] / "fixtures" / "pdf"
PDF_KEY = "projects/p/documents/d.pdf"


def _raw(name: str = "table_page_break.pdf") -> bytes:
    return (FIXTURES / name).read_bytes()


def _layout(raw: bytes) -> DocumentLayout:
    geometry = DocumentParserRegistry().geometry_for(PDF_KEY)
    assert geometry is not None
    return geometry.parse_layout(raw)


def test_codec_roundtrip_keeps_document_runs_and_pages() -> None:
    layout = _layout(_raw())
    stamp = LayoutStamp("pdf-1", "abc")

    restored = decode_layout(encode_layout(layout, stamp), stamp)

    assert restored.document == layout.document
    assert restored.pages == layout.pages
    assert restored.runs is not None and layout.runs is not None
    for column in ("starts", "pages", "char_indexes", "counts"):
        assert getattr(restored.runs, column) == getattr(layout.runs, column)


def test_codec_reads_columns_written_with_other_byte_order() -> None:
    layout = _layout(_raw())
    stamp = LayoutStamp("pdf-1", "abc")
    payload = json.loads(gzip.decompress(encode_layout(layout, stamp)))
    runs = payload["runs"]
    for column in ("starts", "pages", "char_indexes", "counts"):
        values = array("i")
        values.frombytes(base64.b64decode(runs[column]))
        values.byteswap()
        runs[column] = base64.b64encode(values.tobytes()).decode()
    runs["byteorder"] = "big" if runs["byteorder"] == "little" else "little"
    blob = gzip.compress(json.dumps(payload).encode())

    restored = decode_layout(blob, stamp)

    assert restored.runs is not None and layout.runs is not None
    assert restored.runs.starts == layout.runs.starts


@pytest.mark.parametrize(
    "stamp", [LayoutStamp("pdf-2", "abc"), LayoutStamp("pdf-1", "other")], ids=["version", "file"]
)
def test_codec_rejects_cache_of_other_parser_or_file(stamp: LayoutStamp) -> None:
    blob = encode_layout(_layout(_raw()), LayoutStamp("pdf-1", "abc"))
    with pytest.raises(LayoutCacheMismatch):
        decode_layout(blob, stamp)


def test_codec_rejects_corrupt_blob() -> None:
    with pytest.raises(LayoutCacheMismatch):
        decode_layout(b"not gzip", LayoutStamp("pdf-1", "abc"))


class _CountingRegistry(DocumentParserRegistry):
    """Считает полные разборы PDF, чтобы проверить попадание в кэш."""

    def __init__(self) -> None:
        super().__init__()
        self.parses = 0
        inner = self._pdf_geometry
        registry = self

        class _Geometry:
            version = inner.version

            def parse_layout(self, raw: bytes) -> DocumentLayout:
                registry.parses += 1
                return inner.parse_layout(raw)

            def locate(self, raw, runs, spans):  # type: ignore[no-untyped-def]
                return inner.locate(raw, runs, spans)

        self._pdf_geometry = _Geometry()  # type: ignore[assignment]


async def test_reader_parses_pdf_once_and_then_reads_cache() -> None:
    storage = InMemoryFileStorage()
    registry = _CountingRegistry()
    reader = CachedDocumentLayoutReader(registry, storage)
    raw = _raw()

    first = await reader.read(PDF_KEY, raw)
    second = await reader.read(PDF_KEY, raw)

    assert registry.parses == 1
    assert document_layout_key(PDF_KEY) in storage.files
    assert second.document == first.document
    assert second.pages == first.pages


async def test_reader_reparses_when_file_changed() -> None:
    storage = InMemoryFileStorage()
    registry = _CountingRegistry()
    reader = CachedDocumentLayoutReader(registry, storage)

    await reader.read(PDF_KEY, _raw())
    changed = await reader.read(PDF_KEY, _raw("headings_lists.pdf"))

    assert registry.parses == 2
    assert changed.document.plain_text.startswith("Руководство администратора")


async def test_reader_returns_layout_when_cache_write_fails() -> None:
    class ReadOnlyStorage(InMemoryFileStorage):
        async def upload(self, key, content):  # type: ignore[no-untyped-def]
            raise ConnectionError("хранилище недоступно")

    reader = CachedDocumentLayoutReader(DocumentParserRegistry(), ReadOnlyStorage())

    layout = await reader.read(PDF_KEY, _raw())

    assert layout.runs is not None and layout.pages


async def test_reader_parses_other_formats_without_storage() -> None:
    storage = InMemoryFileStorage()
    reader = CachedDocumentLayoutReader(DocumentParserRegistry(), storage)

    layout = await reader.read("projects/p/documents/d.md", b"# Title\n\nText\n")

    assert layout.runs is None and layout.pages == ()
    assert storage.files == {}
    assert await reader.locate("projects/p/documents/d.md", b"", layout, [(0, 1)]) is None


async def test_reader_locates_spans_on_pages() -> None:
    reader = CachedDocumentLayoutReader(DocumentParserRegistry(), InMemoryFileStorage())
    raw = _raw()
    layout = await reader.read(PDF_KEY, raw)
    start = layout.document.plain_text.index("timeout 59")

    [rects] = await reader.locate(PDF_KEY, raw, layout, [(start, start + 10)]) or [()]

    assert {r.page for r in rects} == {4}


def test_highlight_span_of_insertion_uses_previous_char() -> None:
    assert highlight_span(5, 9) == (5, 9)
    assert highlight_span(5, 5) == (4, 5)
    assert highlight_span(0, 0) == (0, 1)


def test_rects_to_json_rounds_coordinates() -> None:
    assert rects_to_json(()) is None
    assert rects_to_json((PageRect(2, 10.04, 20.06, 30, 40),)) == {
        "rects": [[2, 10.0, 20.1, 30, 40]]
    }


def test_layout_cache_is_derived_only_from_pdf_documents() -> None:
    assert derived_document_keys(PDF_KEY) == [f"{PDF_KEY}.layout"]
    assert derived_document_keys("projects/p/documents/d.docx") == []
    assert derived_document_keys("projects/p/sources/s.pdf") == []
    assert derived_document_keys(None) == []
