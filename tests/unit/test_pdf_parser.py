"""PDF-парсер на фикстурах из LibreOffice (набор P0, docs/pdf-spike.md)."""

import io
import threading
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from app.domain.document_geometry import TextRuns
from app.domain.evidence import Quote
from app.domain.exceptions import (
    DocumentParseError,
    PdfEncryptedError,
    PdfHasNoTextLayerError,
    PdfTooLargeError,
)
from app.domain.interfaces.file_storage import UploadContent
from app.domain.interfaces.document_parser import DocumentBlockType
from app.domain.services.evidence_locator import EvidenceLocator, LocatedSource
from app.domain.source_text import FragmentPart, SourceFragment, serialize_source_layout
from app.infrastructure.parsers.parser_registry import DocumentParserRegistry
from app.infrastructure.parsers.pdf_blocks import PDF_RECTS_LOCATOR, build_layout
from app.infrastructure.parsers.pdf_lines import PdfLine, PdfPageLines
from app.infrastructure.parsers.pdf_parser import PdfiumGeometry, PdfParser
from app.infrastructure.parsers.upload_file_inspector import ParserUploadFileInspector

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pdf"


def _raw(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _texts(name: str) -> list[tuple[DocumentBlockType, str]]:
    document = PdfParser().parse(_raw(name))
    return [(b.type, b.text_in(document.plain_text)) for b in document.blocks]


def test_headings_lists_and_paragraphs() -> None:
    document = PdfParser().parse(_raw("headings_lists.pdf"))
    blocks = [(b.type, b.heading_level, b.text_in(document.plain_text)) for b in document.blocks]
    H, P, LI = DocumentBlockType.HEADING, DocumentBlockType.PARAGRAPH, DocumentBlockType.LIST_ITEM
    assert [(t, level) for t, level, _ in blocks] == [
        (H, 1),
        (P, None),
        (H, 2),
        (P, None),
        (LI, None),
        (LI, None),
        (LI, None),
        (H, 2),
        (LI, None),
        (LI, None),
        (LI, None),
        (P, None),
    ]
    assert blocks[0][2] == "Руководство администратора"
    assert blocks[4][2] == "• Скачайте архив"
    assert blocks[8][2] == "1. Откройте конфигурацию"
    # Цифры и знаки препинания в файле идут отдельными объектами, в тексте по местам.
    assert "составляет 50 МБ, превышение" in document.plain_text
    assert [s.ref for s in document.sections] == [
        "Руководство администратора",
        "Установка",
        "Настройка",
    ]
    assert not any(b.editable for b in document.blocks if b.type is H)


def test_table_rows_and_wrapped_cells() -> None:
    blocks = _texts("table.pdf")
    assert [t for t, _ in blocks] == [
        DocumentBlockType.PARAGRAPH,
        DocumentBlockType.TABLE,
        DocumentBlockType.PARAGRAPH,
    ]
    rows = blocks[1][1].split("\n")
    assert rows[0] == "| Параметр | Значение | Комментарий |"
    # Перенос внутри ячейки дал отдельную строку страницы, но ячейка остаётся одна.
    assert rows[1] == "| timeout 1 | 50 | Время ожидания ответа |"
    assert len(rows) == 6
    document = PdfParser().parse(_raw("table.pdf"))
    assert not document.blocks[1].editable


def test_table_across_pages_is_one_block() -> None:
    document = PdfParser().parse(_raw("table_page_break.pdf"))
    tables = [b for b in document.blocks if b.type is DocumentBlockType.TABLE]
    assert len(tables) == 1
    rows = tables[0].text_in(document.plain_text).split("\n")
    assert len(rows) == 60
    # Ячейка, перенесённая на следующую страницу, дописана в свою строку.
    assert all(row.endswith("| Время ожидания ответа |") for row in rows[1:])
    assert rows[-1].startswith("| timeout 59 |")


def test_repeated_headers_and_footers_are_dropped() -> None:
    document = PdfParser().parse(_raw("headers_footers.pdf"))
    assert "Ромашка" not in document.plain_text
    assert "Конфиденциально" not in document.plain_text
    assert len(document.blocks) == 14
    assert all(b.type is DocumentBlockType.PARAGRAPH for b in document.blocks)


def test_rotation_does_not_change_text() -> None:
    assert _texts("rotated.pdf") == _texts("table.pdf")


def test_scan_with_text_layer_is_accepted() -> None:
    text = PdfParser().extract_text(_raw("scan_with_text_layer.pdf"))
    assert "Version 2.4.1" in text


def test_source_layout_uses_common_format_and_rects() -> None:
    layout = PdfParser().extract_source_layout(_raw("headings_lists.pdf"))
    assert layout.text.startswith("# Руководство администратора\n\n")
    assert "\n\n## Установка\n\n" in layout.text
    assert "\n\n- Скачайте архив\n\n" in layout.text
    assert "\n\n- 1. Откройте конфигурацию\n\n" in layout.text
    for span in layout.spans:
        assert span.locator["kind"] == PDF_RECTS_LOCATOR
        for page, x0, y0, x1, y1 in span.locator["rects"]:
            assert page == 1 and x0 < x1 and y0 < y1
    heading = layout.spans[0]
    assert layout.text[heading.start : heading.end] == "# Руководство администратора"
    assert len(heading.locator["rects"]) == 1


def test_source_text_of_table() -> None:
    text = PdfParser().extract_source_text(_raw("table.pdf"))
    assert "\n\n| Параметр | Значение | Комментарий |\n| timeout 1 |" in text


@pytest.mark.parametrize(
    "name", ["headings_lists.pdf", "table.pdf", "table_page_break.pdf", "headers_footers.pdf"]
)
def test_runs_point_to_the_same_characters(name: str) -> None:
    raw = _raw(name)
    document, runs = PdfParser().parse_with_runs(raw)
    pdf = pdfium.PdfDocument(raw)
    textpages = {}
    for i in range(len(runs)):
        page = runs.pages[i]
        if page not in textpages:
            textpages[page] = pdf[page - 1].get_textpage()
        textpage = textpages[page]
        start, first, count = runs.starts[i], runs.char_indexes[i], runs.counts[i]
        ours = document.plain_text[start : start + count].replace(" ", "")
        theirs = "".join(
            chr(pdfium_c.FPDFText_GetUnicode(textpage, first + k)) for k in range(count)
        )
        theirs = "".join(c for c in theirs if not c.isspace()).replace("\uf0b7", "•")
        assert ours == theirs, (start, ours, theirs)
    # Служебная разметка таблицы в карту не входит.
    for i in range(len(runs)):
        start, count = runs.starts[i], runs.counts[i]
        assert "|" not in document.plain_text[start : start + count]


def test_geometry_locates_word_on_its_page() -> None:
    raw = _raw("table_page_break.pdf")
    geometry = PdfiumGeometry(PdfParser())
    layout = geometry.parse_layout(raw)
    document, runs = layout.document, layout.runs
    assert runs is not None
    first = document.plain_text.index("timeout 1 ")
    last = document.plain_text.index("timeout 59")
    rects = geometry.locate(raw, runs, [(first, first + 9), (last, last + 10), (0, 0)])
    assert {r.page for r in rects[0]} == {1}
    assert {r.page for r in rects[1]} == {4}
    assert rects[2] == ()
    for rect in rects[0] + rects[1]:
        assert rect.x0 < rect.x1 and rect.y0 < rect.y1


def test_rejections_name_the_reason() -> None:
    with pytest.raises(PdfHasNoTextLayerError, match="текстового слоя"):
        PdfParser().parse(_raw("image_only.pdf"))
    with pytest.raises(PdfEncryptedError, match="паролем"):
        PdfParser().parse(_raw("encrypted.pdf"))
    with pytest.raises(PdfTooLargeError, match="5 страниц"):
        PdfParser(max_pages=3).parse(_raw("table_page_break.pdf"))
    with pytest.raises(DocumentParseError):
        PdfParser().parse(b"%PDF-1.7 broken")


def test_parallel_parsing_from_two_threads() -> None:
    raw = _raw("table_page_break.pdf")
    expected = PdfParser().parse(raw).plain_text
    results: list[str] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(3):
                results.append(PdfParser().parse(raw).plain_text)
        except BaseException as exc:  # noqa: BLE001 — ошибку потока нужно донести до теста
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert results == [expected] * 6


def test_registry_dispatches_pdf_and_geometry() -> None:
    registry = DocumentParserRegistry()
    assert registry.parse_by_filename("a.PDF", _raw("table.pdf")).blocks
    assert registry.geometry_for("a.pdf") is not None
    assert registry.geometry_for("a.docx") is None


async def test_inspector_reports_pages_without_text_and_rewinds() -> None:
    inspector = ParserUploadFileInspector(pdf_max_pages=10)
    content = UploadContent.from_bytes(_raw("text_then_image.pdf"), "application/pdf")
    report = await inspector.inspect("mixed.pdf", content)
    assert report.pages_without_text == (2,)
    assert content.stream.tell() == 0

    with pytest.raises(PdfHasNoTextLayerError):
        await inspector.inspect(
            "scan.pdf", UploadContent.from_bytes(_raw("image_only.pdf"), "application/pdf")
        )
    plain = UploadContent.from_bytes(b"text", "text/plain")
    assert (await inspector.inspect("a.txt", plain)).pages_without_text == ()


def test_text_runs_pieces() -> None:
    runs = TextRuns()
    runs.append(0, 1, 10, 5)
    runs.append(6, 1, 20, 4)
    runs.append(11, 2, 0, 3)
    assert list(runs.pieces(3, 8)) == [(1, 13, 2), (1, 20, 2)]
    assert list(runs.pieces(5, 6)) == []
    assert list(runs.pieces(0, 14)) == [(1, 10, 5), (1, 20, 4), (2, 0, 3)]


# --------------------------------------------------------------------------- сборка блоков


def _line(
    text: str,
    baseline: float,
    *,
    left: float = 72,
    size: float = 11,
    page: int = 1,
    right: float | None = None,
    hyphenated: bool = False,
) -> PdfLine:
    width = len(text) * size * 0.5
    first_word = text.split(" ", 1)[0]
    return PdfLine(
        page=page,
        text=text,
        left=left,
        bottom=baseline - size * 0.2,
        right=right if right is not None else left + width,
        top=baseline + size * 0.8,
        baseline=baseline,
        size=size,
        cells=[(left, 0, len(text))],
        runs=[(0, 0, len(text))],
        hyphenated=hyphenated,
        first_word_width=len(first_word) * size * 0.5,
    )


def _layout_texts(lines: list[PdfLine]) -> list[str]:
    page = PdfPageLines(1, 612, 792, lines)
    document, _ = build_layout([page]).to_document()
    return [b.text_in(document.plain_text) for b in document.blocks]


def test_soft_hyphen_is_removed_when_lines_join() -> None:
    lines = [
        _line("высокопроизводи\u00ad", 700, right=540, hyphenated=True),
        _line("тельный сервис", 686),
    ]
    assert _layout_texts(lines) == ["высокопроизводительный сервис"]


def test_wide_gap_and_short_line_start_new_paragraphs() -> None:
    lines = [
        _line("Первая строка абзаца без точки", 700, right=540),
        _line("конец абзаца.", 686),
        _line("Второй абзац сразу без отбивки", 672, right=540),
        _line("и его конец", 658, right=540),
        _line("Третий после отбивки", 620),
    ]
    assert _layout_texts(lines) == [
        "Первая строка абзаца без точки конец абзаца.",
        "Второй абзац сразу без отбивки и его конец",
        "Третий после отбивки",
    ]


def test_list_item_continuation_is_indented() -> None:
    lines = [
        _line("• Пункт с длинным текстом", 700, right=540),
        _line("продолжение пункта", 686, left=90),
        _line("Абзац после списка", 672),
    ]
    assert _layout_texts(lines) == [
        "• Пункт с длинным текстом продолжение пункта",
        "Абзац после списка",
    ]


def test_inspector_reads_stream_without_copy() -> None:
    stream = io.BytesIO(_raw("table.pdf"))
    stream.seek(5)
    report = ParserUploadFileInspector(pdf_max_pages=10)._inspect_pdf(stream)
    assert report.pages_without_text == ()


def test_evidence_in_pdf_source_points_to_page_rects() -> None:
    layout = PdfParser().extract_source_layout(_raw("table_page_break.pdf"))
    locator = EvidenceLocator(
        [
            LocatedSource(
                position=0, source_id="s", name="t.pdf", text=layout.text, spans=layout.spans
            )
        ]
    )

    # Строка 58 целиком на странице 4, ячейка «Комментарий» переносится на вторую строку PDF.
    evidence = locator.locate(Quote(source_name="t.pdf", text="timeout 58"))
    assert evidence.match == "exact"
    assert evidence.locator is not None
    assert evidence.locator["kind"] == PDF_RECTS_LOCATOR
    assert [rect[0] for rect in evidence.locator["rects"]] == [4, 4]
    assert layout.text[evidence.start : evidence.end] == "timeout 58"

    # Перенос ячейки строки 59 уходит на страницу 5, подсветка идёт на обе.
    wrapped = locator.locate(Quote(source_name="t.pdf", text="timeout 59"))
    assert wrapped.locator is not None
    assert [rect[0] for rect in wrapped.locator["rects"]] == [4, 5]

    two_rows = locator.locate(Quote(source_name="t.pdf", text="ответа |\n| timeout 59 | 50"))
    assert two_rows.match == "exact"
    assert two_rows.locator is not None
    assert [rect[0] for rect in two_rows.locator["rects"]] == [4, 4, 4, 5]


def test_fragment_parts_follow_formatting_and_merge() -> None:
    fragment = SourceFragment(
        DocumentBlockType.TABLE,
        "  | a |\n| b |",
        parts=(
            FragmentPart(2, 7, {"kind": "k", "rects": [[1]]}),
            FragmentPart(8, 13, {"kind": "k", "rects": [[2]]}),
        ),
    )
    layout = serialize_source_layout(
        [
            (SourceFragment(DocumentBlockType.PARAGRAPH, "Абзац"), {"kind": "p"}),
            (fragment, {"kind": "t"}),
        ]
    )
    span = layout.spans[1]
    assert [layout.text[p.start : p.end] for p in span.parts] == ["| a |", "| b |"]
    first = layout.text.index("| a |")
    assert span.locator_for(first, first + 3) == {"kind": "k", "rects": [[1]]}
    assert span.locator_for(first, len(layout.text)) == {"kind": "k", "rects": [[1], [2]]}
    assert layout.spans[0].locator_for(0, 3) == {"kind": "p"}
