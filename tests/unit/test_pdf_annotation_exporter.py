"""Экспорт PDF аннотациями: исходные байты не меняются, каждая принятая правка видна на
странице в своём месте.

Эталон отрисовки: та же страница исходного файла. Сравнение через PDFium проверяет, что
изменились только пиксели внутри прямоугольника правки и что там появился красный цвет.
"""

from __future__ import annotations

import ctypes
import io
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
from pypdf import PdfReader

from app.domain.document_geometry import PageRect, highlight_span, rects_from_json, rects_to_json
from app.domain.interfaces.document_exporter import AppliedChange
from app.infrastructure.exporters.exporter_registry import DocumentExporterRegistry
from app.infrastructure.exporters.pdf_annotation_exporter import PdfAnnotationExporter
from app.infrastructure.parsers.pdf_parser import PdfiumGeometry, PdfParser
from app.domain.value_objects import DocumentFormatVO

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pdf"
_SCALE = 2.0


def _pdf(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _locate(raw: bytes, spans: list[tuple[int, int]]) -> tuple[str, list[tuple[PageRect, ...]]]:
    geometry = PdfiumGeometry(PdfParser())
    layout = geometry.parse_layout(raw)
    assert layout.runs is not None
    return layout.document.plain_text, geometry.locate(raw, layout.runs, spans)


def _span(text: str, fragment: str) -> tuple[int, int]:
    start = text.index(fragment)
    return start, start + len(fragment)


def _annotations(raw: bytes) -> list[dict[str, object]]:
    reader = PdfReader(io.BytesIO(raw))
    found: list[dict[str, object]] = []
    for number, page in enumerate(reader.pages, start=1):
        for ref in page.get("/Annots") or []:
            annotation = ref.get_object()
            found.append({"page": number, **{k: annotation[k] for k in annotation}})
    return found


def _render(raw: bytes, page_number: int) -> tuple[bytes, int, int, int]:
    document = pdfium.PdfDocument(raw)
    try:
        page = document[page_number - 1]
        bitmap = page.render(scale=_SCALE, may_draw_forms=True, rev_byteorder=True)
        return bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.n_channels
    finally:
        document.close()


def _device_box(raw: bytes, rect: PageRect) -> tuple[int, int, int, int]:
    """Прямоугольник страницы в пикселях отрисовки: PDFium учитывает поворот и CropBox."""
    document = pdfium.PdfDocument(raw)
    try:
        page = document[rect.page - 1]
        width, height = (int(v * _SCALE) for v in page.get_size())
        xs, ys = [], []
        for px, py in ((rect.x0, rect.y0), (rect.x1, rect.y1)):
            dx, dy = ctypes.c_int(), ctypes.c_int()
            pdfium_c.FPDF_PageToDevice(page, 0, 0, width, height, 0, px, py, dx, dy)
            xs.append(dx.value)
            ys.append(dy.value)
        return min(xs), min(ys), max(xs), max(ys)
    finally:
        document.close()


def _diff(
    before: bytes, after: bytes, raw: bytes, rect: PageRect, margin: int = 3
) -> tuple[int, int, int]:
    """(красных пикселей внутри, изменённых внутри, изменённых снаружи) для одной страницы."""
    old, width, height, channels = _render(before, rect.page)
    new, _, _, _ = _render(after, rect.page)
    x0, y0, x1, y1 = _device_box(raw, rect)
    red = inside = outside = 0
    stride = width * channels
    for y in range(height):
        row = y * stride
        for x in range(width):
            i = row + x * channels
            if old[i : i + 3] == new[i : i + 3]:
                continue
            within = x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin
            if within:
                inside += 1
                r, g, b = new[i], new[i + 1], new[i + 2]
                red += int(r > 150 and g < 110 and b < 110)
            else:
                outside += 1
    return red, inside, outside


def _bounds(rects: tuple[PageRect, ...]) -> PageRect:
    return PageRect(
        rects[0].page,
        min(r.x0 for r in rects),
        min(r.y0 for r in rects),
        max(r.x1 for r in rects),
        max(r.y1 for r in rects),
    )


def test_export_appends_annotation_per_accepted_change() -> None:
    raw = _pdf("headings_lists.pdf")
    text, _ = _locate(raw, [])
    modify, delete = _span(text, "50 МБ"), _span(text, "с понятным сообщением")
    insert = _span(text, "тридцати дней.")[1]
    _, rects = _locate(raw, [modify, delete, highlight_span(insert, insert)])
    changes = [
        AppliedChange("b", "modify", "50 МБ", "100 МБ", *modify, rects=rects[0]),
        AppliedChange("b", "delete", "с понятным сообщением", None, *delete, rects=rects[1]),
        AppliedChange("b", "add", None, " Срок продлевается.", insert, insert, rects=rects[2]),
    ]

    exported = PdfAnnotationExporter(author="Тест").apply_changes(raw, changes)

    assert exported.startswith(raw)
    annotations = _annotations(exported)
    assert [a["/Subtype"] for a in annotations] == ["/StrikeOut", "/StrikeOut", "/Caret"]
    assert [a["/Contents"] for a in annotations] == ["100 МБ", "Удалить", " Срок продлевается."]
    assert {a["/T"] for a in annotations} == {"Тест"}
    assert all("/AP" in a for a in annotations)
    quads = annotations[0]["/QuadPoints"]
    assert len(quads) == 8 * len(rects[0])


def test_strikeout_is_drawn_over_replaced_text() -> None:
    raw = _pdf("headings_lists.pdf")
    text, _ = _locate(raw, [])
    span = _span(text, "50 МБ")
    _, [rects] = _locate(raw, [span])
    exported = PdfAnnotationExporter().apply_changes(
        raw, [AppliedChange("b", "modify", "50 МБ", "100 МБ", *span, rects=rects)]
    )

    box = _bounds(rects)
    red, inside, outside = _diff(raw, exported, raw, box)
    assert red > 10
    assert inside > 0 and outside == 0


def test_caret_marks_insertion_point() -> None:
    raw = _pdf("headings_lists.pdf")
    text, _ = _locate(raw, [])
    insert = _span(text, "тридцати дней.")[1]
    _, [rects] = _locate(raw, [highlight_span(insert, insert)])
    exported = PdfAnnotationExporter().apply_changes(
        raw, [AppliedChange("b", "add", None, " текст", insert, insert, rects=rects)]
    )

    [caret] = _annotations(exported)
    x0, y0, x1, y1 = (float(v) for v in caret["/Rect"])
    assert x0 < rects[0].x1 < x1
    red, _, outside = _diff(raw, exported, raw, PageRect(1, x0, y0, x1, y1))
    assert red > 5 and outside == 0


def test_rotated_page_keeps_mark_on_text() -> None:
    raw = _pdf("rotated.pdf")
    text, _ = _locate(raw, [])
    word = next(w for w in text.split() if len(w) > 5)
    span = _span(text, word)
    _, [rects] = _locate(raw, [span])
    exported = PdfAnnotationExporter().apply_changes(
        raw, [AppliedChange("b", "modify", word, "X", *span, rects=rects)]
    )

    assert exported.startswith(raw)
    red, _, outside = _diff(raw, exported, raw, rects[0])
    assert red > 5 and outside == 0


def test_change_across_pages_is_grouped_into_one_note() -> None:
    raw = _pdf("table_page_break.pdf")
    rects = (PageRect(1, 72, 100, 200, 110), PageRect(2, 72, 700, 200, 710))
    exported = PdfAnnotationExporter().apply_changes(
        raw, [AppliedChange("b", "modify", "a", "b", 0, 1, rects=rects)]
    )

    first, second = _annotations(exported)
    assert (first["page"], second["page"]) == (1, 2)
    assert second["/RT"] == "/Group"
    assert second["/IRT"].get_object()["/NM"] == first["/NM"]


def test_change_without_position_becomes_note_on_first_page() -> None:
    raw = _pdf("headings_lists.pdf")
    changes = [
        AppliedChange("b", "modify", "старое", "новое"),
        AppliedChange("b", "delete", "лишнее", None, rects=(PageRect(99, 1, 1, 2, 2),)),
    ]

    exported = PdfAnnotationExporter().apply_changes(raw, changes)

    notes = _annotations(exported)
    assert [(n["page"], n["/Subtype"]) for n in notes] == [(1, "/Text"), (1, "/Text")]
    assert "Было: «старое». Стало: «новое»." in notes[0]["/Contents"]
    assert "Удалить: «лишнее»." in notes[1]["/Contents"]
    # Заметки не накладываются друг на друга.
    assert float(notes[0]["/Rect"][1]) > float(notes[1]["/Rect"][3])


def test_no_changes_returns_original_bytes() -> None:
    raw = _pdf("headings_lists.pdf")
    assert PdfAnnotationExporter().apply_changes(raw, []) is raw


def test_registry_returns_pdf_exporter() -> None:
    exporter = DocumentExporterRegistry().get_exporter(DocumentFormatVO.PDF)
    assert isinstance(exporter, PdfAnnotationExporter)


@pytest.mark.parametrize(
    "geometry",
    [
        None,
        [],
        {"rects": "x"},
        {"rects": [[1, 2, 3]]},
        {"rects": [[1, 10, 10, 5, 20]]},
        {"rects": [[0, 1, 1, 2, 2]]},
        {"rects": [[True, 1, 1, 2, 2]]},
        {"rects": [["1", 1, 1, 2, 2]]},
    ],
)
def test_malformed_geometry_is_skipped(geometry: object) -> None:
    assert rects_from_json(geometry) == ()


def test_geometry_roundtrip() -> None:
    rects = (PageRect(1, 1.04, 2.0, 3.0, 4.0), PageRect(2, 5.0, 6.0, 7.0, 8.0))
    assert rects_from_json(rects_to_json(rects)) == (
        PageRect(1, 1.0, 2.0, 3.0, 4.0),
        PageRect(2, 5.0, 6.0, 7.0, 8.0),
    )
